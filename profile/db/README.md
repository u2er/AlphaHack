# Индексация приложений в PostgreSQL

Данные приложения 1 и фрагменты документов приложения 2 хранятся в схеме `profile_data` базы EventGraph — один Postgres с `pgvector` на весь проект. Векторы считает тот же эмбеддер, что граф и сюжеты (`EMB_MODEL` / `EMB_BASE_URL`, `eventgraph/embed.py`), и сохраняются в `profile_data.chunks.embedding`; размерность берётся у модели, после загрузки создаётся индекс HNSW. Модель записывается в `profile_data.metadata`: поиск откажется работать, если текущий эмбеддер другой. Импорт выполняется отдельно от профиля и GUI.

## Подключение

В compose всё уже настроено:

```bash
docker compose run --rm profile-prepare            # первый раз
docker compose run --rm profile-prepare --replace  # пересборка
```

С хоста — к базе из compose (порт `DB_PORT`, по умолчанию 5433, только на 127.0.0.1):

```dotenv
PROFILE_DATABASE_URL=postgresql://eventgraph:eventgraph@127.0.0.1:5433/eventgraph
```

Без `PROFILE_DATABASE_URL` используется `DATABASE_URL` EventGraph (суффикс драйвера `+asyncpg` отбрасывается). Команде можно передать DSN отдельно через `--dsn`.

## Парсер документов

По умолчанию — `parser.pdf:parse_document` на движке docling: пакет `docling_parser` (из ветки `docling-parser`), тот же, что у PDF-источника EventGraph (`eventgraph/connectors/kinds/pdf.py`). PDF приложения 2 — сканы; docling распознаёт их Tesseract (`rus+eng`) с разметкой таблиц, страница отдаётся markdown. Кэш общий с PDF-источником — `$DATA_DIR/pdf_cache/<sha256>`: документ разбирается один раз для обоих. Первый запуск качает модели docling (~1 ГБ) в `HF_HOME`; полная индексация 8 PDF на CPU — около 7 минут, повторная из кэша — около минуты.

Режим — `PDF_PARSER`:

| значение | что делает |
|---|---|
| `auto` | docling, если установлен; иначе текстовый слой pypdfium2 + OCR бинарником tesseract (по умолчанию) |
| `docling` | только docling |
| `text` | только текстовый слой |
| `ocr` | только tesseract |

В образе всё уже есть: docling, CPU-torch, tesseract с `rus`, `TESSDATA_PREFIX` для tesserocr (без него docling падает с «No language models have been detected»). На хосте docling ставится из `requirements.txt` и требует системный tesseract (`brew install tesseract tesseract-lang`). Запасные движки кэшируют результат в `PARSER_CACHE_DIR` (иначе `$DATA_DIR/parser-cache`). Дата документа — первая дата на первой странице (если OCR потерял день — месяц и год; даты лицензий из дисклеймера пропускаются), иначе из имени файла (`22072026`, `15Apr2021`, `August26`, `3Q26`, `7M26`, `1H26`), иначе `unknown`.

Другой парсер подключается как `parser.module:function`. Функция принимает путь к одному PDF и возвращает:

```python
{
    'date_hint': '2026-09-11',  # дата документа либо 'unknown'
    'pages': [
        {'page': 1, 'text': '', 'extraction': 'tesseract-ocr'},          # обложка без текста
        {'page': 2, 'text': 'Полный текст второй страницы', 'extraction': 'tesseract-ocr'},
    ],
}
```

`pages` — непустой список страниц в исходном порядке, пронумерованных подряд с 1. `text` — строка; пустая страница сохраняется, но в поиск не попадает, а документ совсем без текста останавливает импорт. `extraction` — непустое название способа извлечения; оно сохраняется как происхождение текста. `date_hint` — непустая строка с датой или `unknown`. Неверный результат останавливает импорт, а транзакция PostgreSQL откатывается. Хэш документа и имя файла индексатор получает самостоятельно.

Импорт приложения 1 использует `parser/csv.py`: он читает три исходные таблицы CSV.

## Запуск индексации

```bash
.venv/bin/python -m profile.db.prepare                       # parser.pdf:parse_document
.venv/bin/python -m profile.db.prepare --parser parser.custom:parse_document
```

Команда загружает CSV из `формулировка и данные хакатона/приложение 1` и все PDF из `формулировка и данные хакатона/приложение 2`. Для другого набора файлов используйте `--source /path/to/folder`; внутри должны быть папки `приложение 1` и `приложение 2` с теми же CSV-именами и PDF соответственно.

Если схема `profile_data` уже заполнена, команда остановится. Для явной пересборки добавьте `--replace`: данные схемы заменяются в одной транзакции, а при ошибке старые данные сохраняются. Таблицы EventGraph (схема `public`) не затрагиваются.

После импорта проверьте базу:

```bash
docker compose exec db psql -U eventgraph -d eventgraph \
  -c 'SELECT count(*) FROM profile_data.companies; SELECT count(*) FROM profile_data.chunks;'
```

Справочники полей можно обновить без переиндексации: `.venv/bin/python -m profile.db.field_labels`. Смена эмбеддера или парсера требует новой индексации с `--replace`. Профиль и GUI только читают подготовленную базу.
