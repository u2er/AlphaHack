"""Профили-примеры: компании набора банка (приложение 1), собранные агентом онбординга.

Для каждой компании из EXAMPLES агент получает анкету из набора (ID, ОКВЭД, роль
пользователя) и поля карточки, вопросов не задаёт (пользователя нет) и пишет
страничный профиль. Подтверждённые профили ложатся в eventgraph/seed/profiles/ —
оттуда воркер загружает их при старте (PROFILE_EXAMPLES), так что примеры
приезжают вместе с образом. Нужны библиотека профиля (profile-prepare) и LLM:

    PROFILE_DATABASE_URL=postgresql://eventgraph:eventgraph@127.0.0.1:5433/eventgraph \\
    LLM_API_KEY=… python -m profile.examples [U_26030 …] [--force]
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

from profile.db.store import Store
from profile.onboarding import Onboarding
from utils.config import settings
from utils.llm import make_client

OUT = Path(__file__).resolve().parent.parent / 'eventgraph' / 'seed' / 'profiles'
ROLE = 'Финансовый директор'
ATTEMPTS = 3

# Крупный бизнес набора (выручка > 15 млрд ₽) из разных отраслей: ID → короткое имя.
EXAMPLES = {
    'U_26030': 'Топливный трейдер, Уфа',
    'U_9250': 'Инфраструктурное строительство, Иркутск',
    'U_200890': 'Металлотрейдер, Челябинск',
    'U_34911': 'Зерновой экспортёр, Ростов-на-Дону',
    'U_102920': 'Импортёр рыбы и продуктов, Санкт-Петербург',
    'U_5658': 'Девелопер жилья, Москва',
    'U_4909': 'Тепловая генерация, Москва',
    'U_3263': 'Грузоперевозки, Набережные Челны',
}


def profile_id(company_id: str) -> str:
    return 'example-' + company_id.lower().replace('_', '-')


def build(store, company_id: str, label: str) -> dict:
    company = store.company(company_id)
    with tempfile.TemporaryDirectory() as tmp:
        session = Onboarding(store, lambda: make_client(settings()), Path(tmp) / company_id, max_questions=0)
        session.run_dir.mkdir()
        session.profile_id = profile_id(company_id)
        session.submit_form({
            'company_id': company_id,
            'company_name': f'{company_id} · {label}',
            'activity': f'{company.get("MAINOKVED_NAME", "")} (основной ОКВЭД из набора банка; анкету заполнил не клиент)',
            'role': ROLE,
        })
        session._thread.join()
        if session.status != 'review':
            raise RuntimeError(session.error or session.status)
        payload = session.confirm()['profile']
    return payload


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    force = '--force' in sys.argv
    store = Store()
    OUT.mkdir(parents=True, exist_ok=True)
    failed = []

    def one(company_id):
        path = OUT / f'{profile_id(company_id)}.json'
        if path.exists() and not force:
            print(f'{company_id}: уже есть {path.name}')
            return
        for attempt in range(ATTEMPTS):  # ответ модели иногда не проходит проверку и после ремонта
            try:
                payload = build(store, company_id, EXAMPLES.get(company_id, company_id))
                break
            except Exception as exc:  # noqa: BLE001
                print(f'{company_id}: попытка {attempt + 1} — {str(exc)[:300]}')
        else:
            failed.append(company_id)
            return
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'{company_id}: {path.name}, страниц {len(payload["pages"])}')

    threads = [threading.Thread(target=one, args=(c,)) for c in (args or EXAMPLES)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if failed:
        sys.exit(f'не собраны: {", ".join(failed)}')


if __name__ == '__main__':
    main()
