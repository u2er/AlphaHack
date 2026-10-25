"""LLM settings form of the UI on top of utils/llm_overrides.py.

The browser gets every value except the API key itself: only whether it is set,
where from and its last characters."""
from __future__ import annotations
import json
from urllib.parse import urlsplit
from utils import llm_overrides

PLAIN = ('base_url', 'model', 'vision', 'params', 'max_tokens', 'timeout')


def _hint(key: str) -> str:
    return '…' + key[-4:] if len(key) > 8 else ''


def view() -> dict:
    env, found = llm_overrides.env_defaults(), llm_overrides.load()
    values = {**env, **found}
    key = values['api_key']
    return {
        'values': {k: values[k] for k in PLAIN},
        'env': {k: env[k] for k in PLAIN},
        'overridden': sorted(k for k in found if k in PLAIN),
        'api_key': {'set': bool(key), 'hint': _hint(key),
                    'source': 'gui' if 'api_key' in found else ('env' if key else '')},
        'env_api_key': {'set': bool(env['api_key']), 'hint': _hint(env['api_key'])},
    }


def _validate(values: dict[str, str]) -> None:
    url = values.get('base_url', '')
    if url:
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise ValueError('Адрес API — http(s)://…')
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('В адресе API не должно быть логина, параметров и #')
    if values.get('vision') and values['vision'].lower() not in ('true', 'false'):
        raise ValueError('Картинки — true или false')
    if values.get('params'):
        try:
            if not isinstance(json.loads(values['params']), dict):
                raise ValueError
        except ValueError:
            raise ValueError('Доп. параметры — JSON-объект, например {"reasoning_effort": "none"}') from None
    for name, label, top in (('max_tokens', 'Потолок токенов', 1_000_000), ('timeout', 'Таймаут', 3600)):
        if values.get(name):
            if not values[name].isdigit() or not 1 <= int(values[name]) <= top:
                raise ValueError(f'{label} — целое число от 1 до {top}')


def save(body: dict) -> dict:
    """Fields of the form; an empty field is "as in .env". The key changes only
    when a new one is typed or `reset_api_key` asks to return to .env."""
    raw = body.get('values') or {}
    if not isinstance(raw, dict):
        raise ValueError('Ожидается объект values')
    values = {k: str(raw.get(k) if raw.get(k) is not None else '').strip()[:4000] for k in PLAIN}
    _validate(values)
    current = llm_overrides.load()
    new_key = str(body.get('api_key') or '').strip()
    if new_key:
        values['api_key'] = new_key[:500]
    elif not body.get('reset_api_key') and 'api_key' in current:
        values['api_key'] = current['api_key']
    llm_overrides.save(values)
    return view()


def reset() -> dict:
    llm_overrides.clear()
    return view()
