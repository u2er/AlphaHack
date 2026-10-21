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
                   │  запись ─▶ агент news ─┐                │
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
  граф и сюжеты: OpenAI-совместимый или локальный fastembed.

## Запуск

```bash
cp .env.example .env    # API_KEY, LLM_API_KEY, при желании EMB_BASE_URL
docker compose up -d --build
curl -s localhost:8000/health | jq
```

Поднимаются `eventgraph` (API), `eventgraph-worker` и `eventgraph-db`
(Postgres с pgvector). Без `EMB_BASE_URL` эмбеддер локальный: модель
(~1 ГБ) качается в том `data` при первой записи.

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
`website` — раздел новостей сайта (RSS, затем sitemap, затем листинг),
`pdf` — PDF-документы; настройки каждого описаны в начале его модуля. Новый
вид источника — модуль в `connectors/kinds/` с `async def collect(ctx)`, его
библиотеки — в `requirements.txt`. Источники применяются при перезапуске;
убранный из списка источник больше не запускается, а его записи остаются.

### PDF-документы

Сборщик `pdf` (модуль `connectors/kinds/pdf.py`, парсер — пакет
`docling_parser` в корне репозитория, движок — docling): берёт PDF из
каталога (`mode: "dir"`) или из списка ссылок (`mode: "url"`). Каждый файл —
одна запись: `text` — весь документ (markdown от docling: текст, таблицы,
списки), `image` — вычитанные картинки и кропы таблиц. Курсор — SHA-256
увиденных файлов, кэш парсера — `<DATA_DIR>/pdf_cache`; файл, чьё содержимое
не менялось, повторно не парсится.

В докере источник `pdf_docs` читает каталог `./pdfs` на хосте (в контейнере
`/data/pdfs`) — PDF просто кладётся туда. Первый запуск скачивает модели
docling (~1 ГБ) в `HF_HOME` и может занять много времени — поэтому у
источника свой потолок `timeout`; модели остаются в томе данных, без интернета
работает дальше.

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
| `GET` | `/graph/processed` | последние разобранные записи: вердикт, событие или причина, ран |
| `POST` | `/graph/resume` · `/graph/requeue-failed` | снять паузу после сбоя; вернуть failed в очередь |
| `POST` | `/graph/maintain` · `/graph/maintain/stop` | обслуживание сейчас / остановить после задачи |
| `GET` | `/graph/maintenance[/{id}]` | прогоны обслуживания и их задачи |
| `GET` | `/graphs` · `/graphs/{graph}` · `/graphs/{graph}/nodes?name=` · `/graphs/{graph}/{nodes\|edges}/{id}` | граф: выгрузка, узел, элемент |
| `POST` | `/graphs/{graph}/search` · `/graphs/{graph}/neighbors` | поиск по смыслу, окрестность |
| `GET` | `/graphs/{graph}/period?since=&until=` | события периода и кто был в центре |
| `GET` `POST` | `/agents` · `/agent-runs[/{id}]` · `/agent-runs/{id}/stop` | агенты и их раны |

В граф через API не пишут: пишут только агенты, в воркере, одна запись за
другой.

## Разработка

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
```

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
