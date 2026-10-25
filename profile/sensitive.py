"""Sensitive data the user may attach by accident: found and masked on input,
before chunks, vectors, the LLM, the profile pages and the trace.

Three categories:
- secret — card numbers, CVV, passwords, API keys and tokens: always removed;
- personal — personal data of individuals (passport, SNILS, personal INN, phones,
  e-mails, birth dates): masked by default, the user may keep them;
- commercial — bank account numbers and confidentiality marks («коммерческая
  тайна», NDA…): a page with such a mark is hidden by default, the user may keep it.

Company identifiers (10-digit INN, OGRN, KPP) are public and stay. Findings keep
only a masked preview: the original value is never stored."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass

CATEGORIES = ('secret', 'personal', 'commercial')
ALWAYS = frozenset({'secret'})
DEFAULT = frozenset(CATEGORIES)

# kind → (category, placeholder, label for the user)
KINDS = {
    'card': ('secret', '[карта]', 'номер карты'),
    'cvv': ('secret', '[CVV]', 'CVV/CVC'),
    'password': ('secret', '[пароль]', 'пароль или PIN'),
    'token': ('secret', '[ключ доступа]', 'ключ или токен доступа'),
    'passport': ('personal', '[паспорт]', 'паспорт'),
    'snils': ('personal', '[СНИЛС]', 'СНИЛС'),
    'inn_person': ('personal', '[ИНН физлица]', 'ИНН физлица'),
    'phone': ('personal', '[телефон]', 'телефон'),
    'email': ('personal', '[e-mail]', 'e-mail'),
    'birthdate': ('personal', '[дата рождения]', 'дата рождения'),
    'account': ('commercial', '[счёт]', 'номер счёта'),
    'marker': ('commercial', '', 'пометка о конфиденциальности'),
}
HIDDEN_PAGE = '[Страница скрыта: на ней пометка о конфиденциальности — «{mark}».]'
WINDOW = 40


@dataclass(frozen=True)
class Finding:
    category: str
    kind: str
    start: int
    end: int
    preview: str  # masked: last characters at most

    def as_dict(self, **extra) -> dict:
        data = asdict(self)
        del data['start'], data['end']
        return data | extra


def _digits(text: str) -> str:
    return re.sub(r'\D', '', text)


def _luhn(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def _snils_ok(number: str) -> bool:
    if len(number) != 11:
        return False
    total = sum(int(number[i]) * (9 - i) for i in range(9))
    check = total if total < 100 else 0 if total in (100, 101) else total % 101
    return (0 if check == 100 else check) == int(number[9:])


def _inn12_ok(number: str) -> bool:
    if len(number) != 12:
        return False
    d = [int(c) for c in number]
    n11 = sum(k * v for k, v in zip((7, 2, 4, 10, 3, 5, 9, 4, 6, 8), d)) % 11 % 10
    n12 = sum(k * v for k, v in zip((3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8), d)) % 11 % 10
    return n11 == d[10] and n12 == d[11]


def _near(text: str, start: int, end: int, pattern: re.Pattern) -> bool:
    return bool(pattern.search(text[max(0, start - WINDOW):end + WINDOW]))


CARD_CONTEXT = re.compile(r'(?i)карт|card|visa|master|мир\b|maestro|unionpay')
PASSPORT_CONTEXT = re.compile(r'(?i)паспорт|серия|выдан|passport')
SNILS_CONTEXT = re.compile(r'(?i)снилс')
INN_CONTEXT = re.compile(r'(?i)инн')
ACCOUNT_CONTEXT = re.compile(r'(?i)р/с|к/с|л/с|сч[её]т|бик|account|iban')

# (kind, pattern, check(text, match) -> bool); the value is group 'v' if present.
DETECTORS = [
    ('token', re.compile(r'\b(?P<v>sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}'
                         r'|AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})'), None),
    ('token', re.compile(r'(?i)\bbearer\s+(?P<v>[A-Za-z0-9._~+/-]{16,}=*)'), None),
    ('token', re.compile(r'(?i)\b(?:api[_ -]?key|access[_ -]?key|secret|token|токен|ключ api)\s*[:=]\s*["\']?'
                         r'(?P<v>[A-Za-z0-9._/+-]{16,})'), None),
    ('password', re.compile(r'(?i)\b(?:пароль|password|passwd|pwd|pin|пин-?код|кодовое слово)\s*[:=]\s*'
                            r'(?P<v>\S{3,})'), None),
    ('cvv', re.compile(r'(?i)\b(?:cvv2?|cvc2?)\s*[:=]?\s*(?P<v>\d{3,4})\b'), None),
    ('card', re.compile(r'(?<!\d)(?<!\d[.,])(?P<v>\d{4}(?:[ -]?\d{4}){2}[ -]?\d{4}(?:\d{3})?)(?!\d)(?![.,]\d)'),
     lambda t, m: _luhn(_digits(m['v'])) and (bool(re.search(r'[ -]', m['v'])) or _near(t, m.start(), m.end(), CARD_CONTEXT))),
    ('email', re.compile(r'(?P<v>\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-zА-Яа-я]{2,}\b)'), None),
    ('birthdate', re.compile(r'(?i)(?:дата рождения|д\.\s?р\.|родил(?:ся|ась))\D{0,15}'
                             r'(?P<v>\d{2}[./]\d{2}[./]\d{4})'), None),
    ('snils', re.compile(r'(?<!\d)(?P<v>\d{3}[- ]?\d{3}[- ]?\d{3}[- ]?\d{2})(?!\d)'),
     lambda t, m: _snils_ok(_digits(m['v'])) and (bool(re.search(r'\d{3}-\d{3}-\d{3}', m['v']))
                                                  or _near(t, m.start(), m.end(), SNILS_CONTEXT))),
    ('passport', re.compile(r'(?<!\d)(?P<v>\d{2}\s?\d{2}\s?(?:№\s?)?\d{6})(?!\d)'),
     lambda t, m: _near(t, m.start(), m.end(), PASSPORT_CONTEXT)),
    ('account', re.compile(r'(?<!\d)(?P<v>\d{5}[ .]?\d{3}[ .]?\d[ .]?\d{4}[ .]?\d{7})(?!\d)'),
     lambda t, m: _near(t, m.start(), m.end(), ACCOUNT_CONTEXT)),
    ('inn_person', re.compile(r'(?<!\d)(?P<v>\d{12})(?!\d)'),
     lambda t, m: _inn12_ok(m['v']) and not _near(t, m.start(), m.end(), ACCOUNT_CONTEXT)),
    ('phone', re.compile(r'(?<![\d+])(?P<v>(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2})(?!\d)'), None),
    ('marker', re.compile(r'(?i)\b(?P<v>коммерческая тайна|строго конфиденциально|конфиденциально|'
                          r'для служебного пользования|не для распространения|NDA|confidential)\b'), None),
]


def _preview(kind: str, value: str) -> str:
    if kind == 'marker':
        return value.lower()
    if kind in ('password', 'token', 'cvv', 'birthdate'):
        return '•••'
    if kind == 'email':
        name, _, domain = value.partition('@')
        return f'{name[:1]}•••@{domain}'
    digits = _digits(value)
    return f'•••• {digits[-2:]}' if len(digits) > 4 else '•••'


def scan(text: str) -> list[Finding]:
    """Findings in `text`, sorted, without overlaps (the first detector wins)."""
    found: list[Finding] = []
    taken: list[tuple[int, int]] = []
    for kind, pattern, check in DETECTORS:
        for match in pattern.finditer(text or ''):
            if check and not check(text, match):
                continue
            start, end = match.span('v') if 'v' in pattern.groupindex else match.span()
            if any(start < e and s < end for s, e in taken):
                continue
            taken.append((start, end))
            found.append(Finding(KINDS[kind][0], kind, start, end, _preview(kind, match['v'])))
    return sorted(found, key=lambda f: f.start)


def redact(text: str, hide=DEFAULT, findings: list[Finding] | None = None) -> str:
    """`text` with findings of the `hide` categories replaced by placeholders.
    Secrets are always replaced; a confidentiality mark hides the whole text."""
    hide = set(hide) | ALWAYS
    findings = scan(text) if findings is None else findings
    if 'commercial' in hide:
        mark = next((f for f in findings if f.kind == 'marker'), None)
        if mark:
            return HIDDEN_PAGE.format(mark=mark.preview)
    for f in sorted(findings, key=lambda f: f.start, reverse=True):
        if f.category in hide and f.kind != 'marker':
            text = text[:f.start] + KINDS[f.kind][1] + text[f.end:]
    return text


def counts(findings) -> dict[str, int]:
    """kind → count; accepts Finding objects or their dicts."""
    return dict(Counter(f.kind if isinstance(f, Finding) else f['kind'] for f in findings))


def describe(kinds: dict[str, int]) -> str:
    """«телефон ×2, паспорт» for the chat."""
    return ', '.join(KINDS[k][2] + (f' ×{n}' if n > 1 else '') for k, n in kinds.items())
