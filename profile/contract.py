"""The company profile contract between the profile builders (the chat onboarding,
the CLI wizard) and its consumers (the EventGraph analyst, the chat agent, the UI).

schema_version 2 — `ProfileDocument`, a paged profile. Page `main` is short and
agents always see it, together with the table of contents of the other pages;
the other pages (company files, dataset fields, analysis, dialog) are read on
demand with the profile tools (EventGraph: profile_page, profile_search).

schema_version 1 — `ProfileExport` of the CLI wizard; `load_profile` converts it
to pages, so consumers handle one format. `render_for_prompt` is the text the
analyst reads instead of {{company_profile}}."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import Field, model_validator
from utils.models import StrictModel
from .models import Assessment, Fact, Overview
from .db.field_labels import FIELDS, VALUE_LABELS

SCHEMA_VERSION = '1'
MAIN_PAGE = 'main'
MAIN_PAGE_LIMIT = 4000
CHUNK_CHARS = 900

ANSWER_LABELS = {
    'activity': 'Фактическая деятельность',
    'scale': 'Масштаб и положение на рынке',
    'geography': 'География работы, закупок и продаж',
    'counterparties': 'Клиенты, поставщики, партнёры, конкуренты и зависимости',
    'sensitivity': 'Существенные внешние факторы',
    'user_role': 'Роль пользователя и его решения',
}
FIELD_LABELS = {key: label for key, label, _description, _section in FIELDS}
CODED_VALUES = {(key, raw): label for key, raw, label in VALUE_LABELS}
UNKNOWN = {'не знаю', 'неизвестно', 'не применимо', 'нет данных', '-'}


class SourcePreferences(StrictModel):
    sites: list[str] = Field(default_factory=list)
    mode: Literal['include', 'only'] = 'include'


class ProfileExport(StrictModel):
    schema_version: Literal['1'] = SCHEMA_VERSION
    profile_id: str
    company_id: str | None = None
    company_facts: dict[str, Fact] = Field(default_factory=dict)
    source_facts: dict[str, Fact] = Field(default_factory=dict)
    draft_overview: Overview | None = None
    user_context: dict[str, Fact] = Field(default_factory=dict)
    relationship_signals: dict[str, Any] = Field(default_factory=dict)
    monitoring_context: Assessment
    evidence: dict[str, dict] = Field(default_factory=dict)
    source_preferences: SourcePreferences = Field(default_factory=SourcePreferences)
    updated_at: str


def display_name(export: ProfileExport) -> str:
    """Name for lists: the user-given name, else the synthetic ID, else the profile ID."""
    name = export.company_facts.get('name')
    if name and str(name.value).strip():
        return str(name.value).strip()
    return export.company_id or export.profile_id


def _known(value) -> bool:
    return str(value).strip().lower() not in UNKNOWN


def _fact_line(key: str, fact: Fact) -> str:
    value = CODED_VALUES.get((key, str(fact.value)), fact.value)
    origin = 'указано пользователем' if fact.source == 'user' else 'данные набора банка'
    return f'- {FIELD_LABELS.get(key, key)}: {value} ({origin})'


def _v1_main(export: ProfileExport) -> str:
    """Main page of a v1 export: user answers first (the only source of the actual
    business), then dataset fields with their caveats, relationship signals with
    their limits and the profile's limitations."""
    lines = [f'Клиент: {display_name(export)} (профиль {export.profile_id}, обновлён {export.updated_at[:10]}).',
             'Профиль подтверждён пользователем.'
             + (' ID обезличены: не приписывай компании реальное название.' if export.company_id else '')]
    if export.draft_overview and export.draft_overview.summary.strip():
        lines += ['', 'Сводка:', export.draft_overview.summary.strip()]

    answers = [(key, fact) for key, fact in export.user_context.items() if key in ANSWER_LABELS]
    if answers:
        lines += ['', 'Ответы пользователя (главный источник о фактическом бизнесе):']
        for key, fact in answers:
            value = str(fact.value).strip()
            lines.append(f'- {ANSWER_LABELS[key]}: {value if _known(value) else "пользователь не знает / не указал"}')
    extra = [fact.value for key, fact in export.user_context.items()
             if key.startswith('agent_question_') and isinstance(fact.value, dict)]
    if extra:
        lines += ['', 'Уточнения пользователя:']
        lines += [f'- {item.get("question", "")} — {item.get("answer", "")}' for item in extra]

    facts = [(key, fact) for key, fact in export.company_facts.items()
             if key not in {'name', 'SYNTHETIC_U', 'GROUP_ID'} and _known(fact.value)]
    if facts:
        lines += ['', 'Поля карточки (регистрационные и банковские признаки, не описание фактического бизнеса):']
        lines += [_fact_line(key, fact) for key, fact in facts]
        if any(key == 'REVENUE_COMPANY' and fact.source != 'user' for key, fact in facts):
            lines.append('У выручки из набора не указаны валюта, единица и год — не сравнивай её численно.')

    signals = export.relationship_signals or {}
    payments, linked = signals.get('payments') or [], signals.get('shared_person_companies') or []
    if payments or linked:
        lines += ['', f'Наблюдаемые связи: {len(payments)} направлений платежей, {len(linked)} компаний через общих '
                      'физлиц. Тип отношений, период и назначение неизвестны — не называй их поставщиками или '
                      'покупателями и не выводи зависимость из сумм.']

    limitations = list(export.monitoring_context.limitations) + list(signals.get('limitations') or [])
    if limitations:
        lines += ['', 'Ограничения профиля:'] + [f'- {item}' for item in dict.fromkeys(limitations)]
    return '\n'.join(lines)


# ── schema_version 2: paged profile ─────────────────────────────────────────


class PageSource(StrictModel):
    """Where a page's statements come from: form.<field>, answer.<n>, file:<id>:<page>,
    dataset.<FIELD>, signals.<name>, bank:<chunk id>."""
    ref: str
    note: str = ''


class ProfilePage(StrictModel):
    id: str = Field(pattern=r'^[a-z0-9][a-z0-9_.-]{0,63}$')
    title: str = Field(min_length=1, max_length=200)
    # One line for the table of contents: what an agent will find on the page.
    summary: str = Field(default='', max_length=400)
    content: str
    sources: list[PageSource] = Field(default_factory=list)
    # agent — written by the onboarding agent; file / dataset / dialog — collected as is.
    origin: Literal['agent', 'user', 'file', 'dataset', 'dialog', 'v1'] = 'agent'


class FileRef(StrictModel):
    id: str
    filename: str
    kind: str = ''
    pages: int = 0
    sha256: str = ''
    # Скрыто защитой при загрузке: вид данных → число (profile.sensitive).
    redactions: dict[str, int] = Field(default_factory=dict)


class ProfileDocument(StrictModel):
    schema_version: Literal['2'] = '2'
    profile_id: str
    company_id: str | None = None
    name: str
    user_role: str = ''
    pages: list[ProfilePage]
    files: list[FileRef] = Field(default_factory=list)
    # Коннекторы EventGraph, которые смотрит клиент; пусто — все.
    news_sources: list[str] = Field(default_factory=list)
    updated_at: str

    @model_validator(mode='before')
    @classmethod
    def _drop_watch_topics(cls, data):
        # Темы наблюдения убраны из профиля; сохранённые раньше профили с ними загружаются.
        if isinstance(data, dict) and 'watch_topics' in data:
            data = {k: v for k, v in data.items() if k != 'watch_topics'}
        return data

    @model_validator(mode='after')
    def _main_first(self):
        if not self.pages or self.pages[0].id != MAIN_PAGE:
            raise ValueError('The first page must be the main page (id "main")')
        if len(self.pages[0].content) > MAIN_PAGE_LIMIT:
            raise ValueError(f'The main page is limited to {MAIN_PAGE_LIMIT} characters')
        ids = [page.id for page in self.pages]
        if len(ids) != len(set(ids)):
            raise ValueError('Page ids must be unique')
        return self

    def page(self, page_id: str) -> ProfilePage | None:
        return next((page for page in self.pages if page.id == page_id), None)


def from_v1(export: ProfileExport) -> ProfileDocument:
    """A CLI-wizard export as pages: the old prompt text is the main page, the
    dataset fields, signals and bank evidence become pages."""
    pages = [ProfilePage(id=MAIN_PAGE, title='Основное', summary='Ответы пользователя и ключевые поля',
                         content=_v1_main(export)[:MAIN_PAGE_LIMIT], origin='v1')]
    facts = [(k, f) for k, f in export.source_facts.items() if _known(f.value)]
    if facts:
        pages.append(ProfilePage(
            id='dataset', title='Данные набора банка', origin='dataset',
            summary='Исходные поля приложения 1: регистрация, ОКВЭД, банковские продукты',
            content='\n'.join(_fact_line(k, f) for k, f in facts),
            sources=[PageSource(ref=f'dataset.{k}') for k, _ in facts]))
    signals = export.relationship_signals or {}
    if signals.get('payments') or signals.get('shared_person_companies'):
        rows = [f"- платежи {p.get('payer')} → {p.get('beneficiary')}: {p.get('count')} операций"
                for p in signals.get('payments') or []]
        rows += [f"- общие физлица с {c.get('company_id')}" for c in signals.get('shared_person_companies') or []]
        pages.append(ProfilePage(
            id='signals', title='Наблюдаемые связи', origin='dataset',
            summary='Направления платежей и связи через физлиц; тип отношений неизвестен',
            content='\n'.join(rows + [f'Ограничение: {x}' for x in signals.get('limitations') or []]),
            sources=[PageSource(ref='signals.payments'), PageSource(ref='signals.shared_person_companies')]))
    if export.evidence:
        blocks = [f"### {e.get('filename', key)}, стр. {e.get('page', '?')} ({e.get('date_hint', 'дата не указана')})\n"
                  f"{e.get('text', '')}" for key, e in export.evidence.items()]
        pages.append(ProfilePage(
            id='bank-evidence', title='Фрагменты аналитики банка', origin='file',
            summary='Фрагменты отчётов банка, подобранные мастером',
            content='\n\n'.join(blocks), sources=[PageSource(ref=f'bank:{k}') for k in export.evidence]))
    role = export.user_context.get('user_role')
    return ProfileDocument(
        profile_id=export.profile_id, company_id=export.company_id, name=display_name(export),
        user_role=str(role.value) if role else '', pages=pages, updated_at=export.updated_at)


def load_profile(payload: dict | ProfileDocument | ProfileExport) -> ProfileDocument:
    """Any supported export as a paged profile; ValueError if it is neither."""
    if isinstance(payload, ProfileDocument):
        return payload
    if isinstance(payload, ProfileExport):
        return from_v1(payload)
    if isinstance(payload, dict) and str(payload.get('schema_version')) == '1':
        return from_v1(ProfileExport.model_validate(payload))
    return ProfileDocument.model_validate(payload)


def display_title(doc: ProfileDocument) -> str:
    return doc.name or doc.company_id or doc.profile_id


def table_of_contents(doc: ProfileDocument) -> list[dict]:
    return [{'id': page.id, 'title': page.title, 'summary': page.summary, 'chars': len(page.content),
             'origin': page.origin}
            for page in doc.pages[1:]]


def render_for_prompt(profile) -> str:
    """What agents always see: the main page and the table of contents of the rest."""
    doc = load_profile(profile)
    lines = [f'Клиент: {display_title(doc)} (профиль {doc.profile_id}, обновлён {doc.updated_at[:10]}). '
             'Профиль подтверждён пользователем.'
             + (' ID обезличены: не приписывай компании реальное название.' if doc.company_id else ''), '',
             doc.pages[0].content.strip()]
    if doc.user_role and doc.user_role not in doc.pages[0].content:
        lines += ['', f'Роль пользователя: {doc.user_role}']
    toc = table_of_contents(doc)
    if toc:
        lines += ['', 'Другие страницы профиля (читать — profile_page, искать — profile_search):']
        lines += [f'- [{page["id"]}] {page["title"]}' + (f' — {page["summary"]}' if page['summary'] else '')
                  for page in toc]
    return '\n'.join(lines)


def chunk_pages(doc: ProfileDocument, size: int = CHUNK_CHARS) -> list[dict]:
    """Search fragments of the non-main pages: paragraphs packed up to `size`
    characters, a long paragraph cut at whitespace. Each keeps its page."""
    out = []
    for page in doc.pages[1:]:
        parts, current = [], ''
        for paragraph in [p.strip() for p in page.content.split('\n\n') if p.strip()]:
            while len(paragraph) > size:
                cut = paragraph.rfind(' ', size // 2, size)
                cut = cut if cut > 0 else size
                if current:
                    parts.append(current)
                    current = ''
                parts.append(paragraph[:cut].strip())
                paragraph = paragraph[cut:].strip()
            if current and len(current) + len(paragraph) + 2 > size:
                parts.append(current)
                current = ''
            current = f'{current}\n\n{paragraph}' if current else paragraph
        if current:
            parts.append(current)
        out += [{'page_id': page.id, 'page_title': page.title, 'n': n, 'text': text} for n, text in enumerate(parts)]
    return out
