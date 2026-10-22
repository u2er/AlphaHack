# EventGraph

**Граф событий из новостей — сбор, агенты и граф в одном проекте.**

Копия связки Irminsule + Loom + Casket, собранная в один сервис: коннекторы
копят записи из Telegram-каналов и сайтов, агент новостей разбирает каждую
запись в граф событий сразу, как она собрана, садовник обслуживает граф. Источники заданы в коде
(`eventgraph/sources.py`). Интерфейса нет — только HTTP API. Фильтров до
агента нет — в разбор идёт всё собранное. Обзоров и чата нет — вырезаны.

```
 Telegram · сайты ─▶ коннекторы ─▶ items (Postgres)
                                      │ сюжеты (эмбеддер)
                                      ▼
                   ┌────────── воркер ──────────────────────┐
                   │  запись ─▶ analyst ─▶ news ─┐           │
                   │  задачи ─▶ gardener ───┘ инструменты ─▶ граф (Postgres + pgvector)
                   └─────────────────────────────────────────┘
                              ▲
                     API (FastAPI, X-Api-Key)
```

## Откуда что

| часть | взято из | где теперь |
|---|---|---|
| сборщики Telegram-канала и сайта (были шаблонами коннекторов), лента, разбор записей, обслуживание | Irminsule (`core/irminsule`) | `eventgraph/connectors/kinds`, `sources.py`, `news.py`, `stories.py`, `gardening.py`, `worker.py`, `api/` |
| ReAct-цикл агента: шаги, повторы вызова модели, `RepeatGuard`, картинки, учёт токенов | Loom (`worker/runner.py`, `images.py`) | `eventgraph/agents/runner.py`, `agents/images.py` |
| агенты News и Gardener: шаги, температура, права | агенты Loom id 4, 5 и поля модуля `casket_search` | `eventgraph/agents/specs.py` |
| промпты агентов | копии из Echo (`news`, `news-gardener`) | `prompts/*.md` |
| инструменты графа: поиск, узел, окрестность, период, запись, обслуживание | модуль Loom `casket_search` (id 14) | `eventgraph/agents/tools.py`, `graph/upkeep.py` |
| графы: узлы по имени, рёбра по четвёрке, поиск по смыслу, обход, слияние дублей и журнал решений | Casket (`db/graph.py`, `db/graph_merge.py`, `proc/embed.py`) | `eventgraph/graph/store.py`, `graph/merge.py`, `embed.py` |

## Чем отличается от оригинала

- **Без обзоров и чата.** Вырезаны Летописец, таблица и API обзоров,
  инструмент `digest_save`, узлы `digest` в графе и чат с собеседником.
- **Коннекторы — в коде.** Список источников — `eventgraph/sources.py`,
  сборщики — модули `eventgraph/connectors/kinds/`, их библиотеки — в
  `requirements.txt`. Нет кода коннекторов в базе, правки через API,
  шаблонов и установщика библиотек. В базе у коннектора — только курсор,
  расписание и история запусков.
- **Записи разбираются по одной, как только собраны.** Нет прогонов по
  расписанию и пачек: воркер берёт самую старую неразобранную запись и отдаёт
  её агенту отдельным раном — новая запись уходит в разбор через пару секунд
  после сбора. Раны идут строго по одному: две записи об одном событии,
  разобранные параллельно, дали бы два узла-дубля. Ран без ответа (модель
  недоступна) ставит разбор на паузу — 30 с, дальше вдвое, до 15 минут;
  первый удачный ран её снимает. Садовник работает между записями, когда
  подошёл срок.
- **Без UI.** Нет веба и входа по паролю — только API с ключом `X-Api-Key`.
- **Без фильтров.** Убраны фильтр коннектора (LLM по критериям, вердикт
  `filtered`) и суточная квота по важности (`below_cut`) вместе с оценкой
  важности. Сюжеты остались — это подсказка, а не отбор: агент видит, в какие
  события графа уже разобраны другие записи сюжета. Почти дословный повтор уже разобранного
  по-прежнему закрывается как `duplicate`; `DUPLICATE_THRESHOLD=0` — не
  закрывать и его.
- **Один процесс пишет в граф.** Агенты исполняются в воркере и зовут граф
  напрямую, без HTTP. Ушли очередь записи модуля в памяти, ответ `status:
  queued` и предел Loom в 60 с на вызов; `graph_upsert` пишет всё или ничего
  одной транзакцией. Промпты поправлены под это.
- **Граф — в Postgres с pgvector**, а не в Qdrant: одна база на всё. Модель та
  же, что в Casket; длинные метаданные (сводка события, цитата публикации)
  получают свой вектор, как meta-узлы `graph_attr`.
- **Элементы узлов не идут через LLM.** Casket суммаризировал каждый текст
  моделью, и запись пачки шла час; здесь текст эмбеддится как есть.
- **Инструменты переименованы:** `casket_graph_*` → `graph_*`. Атомы Casket
  (`casket_save_atom` и т. п.) не перенесены — эти агенты ими не пользуются.
- **Одна модель на всех агентов** (OpenAI-совместимый API), один эмбеддер на
  граф, сюжеты и библиотеку профиля: OpenAI-совместимый или локальный
  fastembed. Профиль компании читает те же `LLM_*` и ту же базу.

## Запуск

```bash
cp .env.example .env    # API_KEY, LLM_API_KEY, при желании EMB_BASE_URL
docker compose up -d --build
curl -s localhost:8000/health | jq
```

Поднимаются `eventgraph` (API), `eventgraph-worker`, `eventgraph-profile-gui`
(интерфейс «Альфа-Бизнес»: бизнес-аналитика и мастер профиля,
http://127.0.0.1:8765, см. `gui/README.md`) и `eventgraph-db` (Postgres с
pgvector). Без `EMB_BASE_URL` эмбеддер локальный: модель (~1 ГБ) качается в
том `data` при первой записи.

Библиотеку для мастера профиля — CSV приложения 1 и OCR + векторы PDF
приложения 2 в схему `profile_data` той же базы — готовят один раз:

```bash
docker compose run --rm profile-prepare            # --replace — пересобрать
```

## Источники

Источник — строка в `SOURCES` в `eventgraph/sources.py`:

```python
Source(
    name="tsunews",                      # имя: латиница, цифры, _; по нему хранится курсор
    collect=telegram_channel.collect,    # сборщик из connectors/kinds/
    config={"channel": "tsunews", "backfill": 10},
    interval_seconds=3600,               # None — только вручную
    images=True,                         # брать ли картинки записей
),
```

Сборщики: `telegram_channel` — публичный канал через `t.me/s/<канал>`,
`website` — раздел новостей сайта (RSS, затем sitemap, затем листинг);
настройки каждого описаны в начале его модуля. Новый вид источника — модуль
в `connectors/kinds/` с `async def collect(ctx)`, его библиотеки — в
`requirements.txt`. Источники применяются при перезапуске; убранный из списка
источник больше не запускается, а его записи остаются.

```bash
H='X-Api-Key: …'
curl -s -H "$H" localhost:8000/connectors | jq '.[] | {name, last_status, items_count}'
curl -s -X POST -H "$H" localhost:8000/connectors/tsunews/test       # проба: что вернул бы, без записи
curl -s -X POST -H "$H" localhost:8000/connectors/tsunews/run        # собрать сейчас
curl -s -H "$H" localhost:8000/graph/status | jq '{queue, working, paused_until}'  # как идёт разбор
curl -s -H "$H" localhost:8000/graph/processed | jq '.[0]'                         # последняя разобранная запись
```

## Агенты

| агент | что делает | права в графе | шагов |
|---|---|---|---|
| `news` | разбирает каждую новую запись отдельным раном: пишет события, людей, организации, издания; вердикт по записи | чтение, запись | 100 |
| `gardener` | раз в `maintenance_every_days` дней, между записями: дубли, островки, обрезка | чтение, запись, удаление, обслуживание | 60 |
| `analyst` | перед `news`, на каждую запись: насколько она важна для компании и как может повлиять на её бизнес, с оглядкой на прошлые события графа | чтение | 30 |

Аналитик оценивает каждую запись для каждого клиента — см. «Профили
клиентов» ниже. Оценка лежит в `items.analysis.profiles[profile_id]`:
обязательны `importance` (0–5, для клиента, а не для мира) и `impact`,
остальные поля — `relevance`, `direction`, `horizon`, `trend`, `areas`,
`entities`, `related_events`, `actions`, `confidence`, `headline` — описаны в
конце промпта. Верхние поля `items.analysis` — самая важная из оценок с её
`profile_id`. Сбой аналитика разбор не останавливает: запись уходит в `news`
без оценки (`error`), а повторная попытка разбора оценивает её снова — только
для тех клиентов, у кого оценки ещё нет.

Порог важности — `min_importance` в `PUT /graph/settings` (по умолчанию из
`ANALYST_MIN_IMPORTANCE`, 0 — без порога). Запись ниже порога **для всех
клиентов** закрывается вердиктом `below_threshold`: агент `news` её не видит,
в граф она не попадает, оценки остаются. Важная хоть одному клиенту идёт в
общий граф. Запись без оценки порог не задерживает. Снизили порог или добавили
клиента — `POST /graph/requeue-below-threshold` вернёт такие записи в очередь:
сделанные оценки не повторяются, новый клиент получает свою.

## Профили клиентов

Профиль — подтверждённый экспорт мастера профиля (`profile/`, `gui/`):
JSON `schema_version=1`, контракт — `profile/contract.py` (`ProfileExport`).
Пример — `tests/fixtures/profiles/vectorfood.json`. Попадает в EventGraph
двумя путями:

- GUI и CLI мастера кладут экспорт в `PROFILES_DIR` (в compose — общий том
  `/profiles`); воркер загружает новые и изменённые файлы перед разбором;
- `PUT /profiles` с тем же JSON.

Аналитик видит профиль текстом `render_for_prompt` (ответы пользователя,
поля карточки с оговорками, связи без имён контрагентов, подтверждённые темы
и ограничения) вместо `{{company_profile}}` в `prompts/analyst.md`; текст
хранится в `company_profiles.prompt` и подставляется в ран
(`agent_runs.variables`). Пока не загружено ни одного профиля, работает один
профиль `default` из `COMPANY_PROFILE` (пусто — заготовка). Лента клиента —
`GET /profiles/{id}/feed`: записи с его оценкой не ниже `min_importance`,
важные первыми.

```bash
curl -s -X PUT -H "$H" -H 'Content-Type: application/json' \
  --data @tests/fixtures/profiles/vectorfood.json localhost:8000/profiles | jq
curl -s -H "$H" 'localhost:8000/profiles/demo-vectorfood/feed?min_importance=3' | jq '.[] | {title, analysis}'
```

Каждый ран пишется в `agent_runs`, каждый шаг — в `agent_logs`: рассуждения,
вызовы с аргументами и ответами, ошибки (`GET /agent-runs/{id}`). Запустить
только часть агентов — `AGENTS=news`.

## API

| метод | путь | |
|---|---|---|
| `GET` | `/health` | база, модель, эмбеддер, включённые агенты (без ключа) |
| `GET` | `/connectors[/{name}]` | источники из `sources.py` с курсором и последним запуском |
| `POST` | `/connectors/{name}/run` · `/connectors/{name}/test` · `/connectors/{name}/state/reset` | запуск, проба без записи, сброс курсора |
| `GET` | `/connectors/{name}/runs` · `/connector-runs/{id}` | запуски |
| `GET` `DELETE` | `/items[/{uid}]` · `/items/count` · `/items/{uid}/images/{n}` | лента |
| `GET` `PUT` | `/graph/status` · `/graph/settings` · `/graph/stats` | разбор: очередь, запись в работе, пауза; настройки (`enabled: false` — пауза); динамика по дням |
| `GET` | `/graph/processed` | последние разобранные записи: вердикт, событие или причина, ран, оценка для компании (`?min_importance=4`) |
| `POST` | `/graph/resume` · `/graph/requeue-failed` · `/graph/requeue-below-threshold` | снять паузу после сбоя; вернуть в очередь failed / закрытые порогом важности |
| `POST` | `/graph/maintain` · `/graph/maintain/stop` | обслуживание сейчас / остановить после задачи |
| `GET` | `/graph/maintenance[/{id}]` | прогоны обслуживания и их задачи |
| `GET` | `/graphs` · `/graphs/{graph}` · `/graphs/{graph}/nodes?name=` · `/graphs/{graph}/{nodes\|edges}/{id}` | граф: выгрузка, узел, элемент |
| `POST` | `/graphs/{graph}/search` · `/graphs/{graph}/neighbors` | поиск по смыслу, окрестность |
| `GET` | `/graphs/{graph}/period?since=&until=` | события периода и кто был в центре |
| `GET` `POST` | `/agents` · `/agent-runs[/{id}]` · `/agent-runs/{id}/stop` | агенты и их раны |
| `GET` `PUT` | `/profiles` | профили клиентов; PUT — экспорт мастера профиля (новая версия по `profile_id`) |
| `GET` `DELETE` | `/profiles/{id}` | профиль с текстом для аналитика; DELETE — снять с оценки (лента остаётся) |
| `GET` | `/profiles/{id}/feed?min_importance=` | лента клиента по его оценкам; `default` — профиль из `COMPANY_PROFILE` |

В граф через API не пишут: пишут только агенты, в воркере, одна запись за
другой.

## Разработка

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest      # EventGraph, профиль, GUI, парсер
```

Профиль и GUI с хоста ходят в базу из compose через
`PROFILE_DATABASE_URL=postgresql://eventgraph:eventgraph@127.0.0.1:5433/eventgraph`
(см. `profile/README.md`).

Интеграционные тесты (`-m integration`) идут на живом Postgres с pgvector и
подставном эмбеддере:

```bash
docker run -d --rm --name eventgraph-test-db -p 55432:5432 \
  -e POSTGRES_USER=eventgraph -e POSTGRES_PASSWORD=eventgraph -e POSTGRES_DB=eventgraph \
  pgvector/pgvector:pg16
DATABASE_URL=postgresql+asyncpg://eventgraph:eventgraph@localhost:55432/eventgraph \
  .venv/bin/python -m pytest -m integration tests/integration
```

Тесты сборщиков (`tests/unit/test_connector_kinds.py`) гоняют их на
подставном HTTP, без сети.
