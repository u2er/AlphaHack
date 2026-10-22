"""The exported company profile (schema_version 1): the one contract between the
profile wizard and its consumers — the EventGraph analyst, the chat agent, the UI.

`ProfileExport` validates an export; `render_for_prompt` turns it into the text
the analyst reads instead of {{company_profile}}. Only confirmed exports exist:
export_profile refuses until both the profile and the topics are confirmed."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import Field
from utils.models import StrictModel
from .models import Assessment, Fact, Overview
from .db.field_labels import FIELDS, VALUE_LABELS

SCHEMA_VERSION = '1'

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


def render_for_prompt(export: ProfileExport | dict) -> str:
    """Profile text for agent prompts: user answers first (the only source of the
    actual business), then dataset fields with their caveats, relationship signals
    with their limits, and the topics the user confirmed."""
    if not isinstance(export, ProfileExport):
        export = ProfileExport.model_validate(export)
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

    topics = export.monitoring_context.topics
    if topics:
        grounded = any(s in export.evidence for topic in topics for s in topic.source_ids)
        lines += ['', 'Темы наблюдения, подтверждённые пользователем'
                      + (' (по материалам аналитики банка):' if grounded else ':')]
        for topic in topics:
            sources = ', '.join(dict.fromkeys(
                f'{export.evidence[s].get("filename", s)} ({export.evidence[s].get("date_hint", "дата не указана")})'
                for s in topic.source_ids if s in export.evidence))
            lines.append(f'- {topic.title}: {topic.rationale}' + (f' [источники: {sources}]' if sources else ''))
    limitations = list(export.monitoring_context.limitations) + list(signals.get('limitations') or [])
    if limitations:
        lines += ['', 'Ограничения профиля:'] + [f'- {item}' for item in dict.fromkeys(limitations)]
    return '\n'.join(lines)
