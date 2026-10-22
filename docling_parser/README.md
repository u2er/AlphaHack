# docling_parser

Самостоятельный PDF-парсер, вынесенный из проекта `agentic-rag`. Движок — [docling](https://github.com/docling-project/docling). Парсер извлекает текст, таблицы и изображения из PDF-документов и возвращает их в виде объектов `ParsedDocument` (собственный dataclass — без зависимости от langchain).

В этом репозитории пакет лежит в корне (импорт — `from docling_parser import ...`) и
используется коннектором PDF (`eventgraph/connectors/kinds/pdf.py`): кэш парсера
ведёт в `<каталог данных>/pdf_cache`, а движок и системные зависимости ставятся
общим `requirements.txt` и Dockerfile.

## Возможности

- Текст извлекается как markdown с маркерами элементов (`[TABLE]`, `[IMAGE]`, `[START_LIST]`/`[END_LIST]` и др.)
- Таблицы: `pandas.DataFrame` + markdown-представление + кроп-изображение области таблицы
- Изображения: `PIL.Image` + подпись (caption) + произвольное текстовое описание
  (`image_describer`, например VLM-вызов): в тексте маркер `[IMAGE]` получает
  описание рядом — `[IMAGE] <описание>`; для таблиц то же через `table_describer`
  (`[TABLE] <описание>`)
- Дисковый кэш результатов с ключом SHA-256 файла — повторный запуск на тот же файл не перепарсит документ
- OCR «нечитаемых» страниц — встроенным OCR docling (Tesseract, языки `rus`/`eng`)
- `docling` импортируется лениво: пакет остаётся импортируемым без установленной
  docling (например, в API-процессе); создание `PdfFileManager` и сам парсинг
  требуют docling — движок импортируется в его `__init__`

## Структура пакета

```
docling_parser/                 # пакет в корне репозитория
├── __init__.py                     # экспорт: PdfFileManager, ParsedDocument, PDFDoclingParser
├── document.py                     # ParsedDocument — тип результата (замена langchain_core.Document)
├── file_manager.py                 # PdfFileManager — кэш, обработка, ParsedDocument-объекты
├── docling_parser.py               # PDFDoclingParser — движок docling (ленивый импорт)
├── docling_parser_utils.py         # кастомные markdown-сериализаторы для docling
├── utils/
│   ├── hash_utils.py               # SHA-256 файла
│   └── markdown_utils.py           # маркер [IMAGE] и вставка описаний (лёгкий модуль)
├── tests/                          # собственные тесты (отдельный запуск, см. «Тесты»)
├── examples/                       # скрипт ручного smoke-теста (run_example.py)
└── README.md
```

## Установка

Системные зависимости (в Docker образ ставит `Dockerfile`; вручную):

- Python 3.10+
- Tesseract 5+ с языковыми пакетами `rus` и `eng` — OCR docling:
  - Ubuntu: `sudo apt-get install tesseract-ocr tesseract-ocr-rus`
  - путь к traineddata при необходимости задаётся через `TESSDATA_PREFIX` (например, `/usr/share/tesseract-ocr/5/tessdata/`); проверить: `tesseract --list-langs`

Python-зависимости уже перечислены в `requirements.txt` корневого репозитория
(раздел «PDF-парсер»):

```bash
pip install -r requirements.txt
```

Примечание: docling тянет `torch` (по умолчанию — CUDA-сборку). Для CPU-only установите сначала CPU-сборку, чтобы pip не скачивал лишнее (в Dockerfile это сделано отдельным слоем):

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
```

Важно: при первом запуске docling загружает layout/table-модели (~1 ГБ) с HuggingFace. Если кэш моделей уже есть, укажите переменную окружения `HF_HOME=/path/to/hf_cache` (в docker-compose — `HF_HOME=/data/hf`).

## Быстрый старт

```python
from docling_parser import PdfFileManager

manager = PdfFileManager(file_path="doc.pdf", cache_dir="./cache")
# manager = PdfFileManager(file_path="doc.pdf", cache_dir="./cache", images_scale=2)
manager.extract_content()

texts  = manager.texts    # list[ParsedDocument], по одному на страницу
tables = manager.tables   # list[ParsedDocument], метаданные: df, image, page, n
images = manager.images   # list[ParsedDocument], метаданные: image, page, n
full   = manager.text     # ParsedDocument со всем текстом (None на пути через кэш —
                          # в этом случае полный текст собирается из manager.texts)
```

## API

### PdfFileManager

`PdfFileManager(file_path, cache_dir, cache_key=None, **parser_kwargs)`

- `extract_content()` — парсит или загружает из кэша
- `cache_key` — версия рецепта парсинга (текст промпта описаний, модель,
  масштаб, прочие параметры движка). Кэш считается годным, только если
  записанный в нём ключ совпадает: иначе каталог `<sha256>` удаляется и
  документ парсится заново. Нужен, чтобы после смены промпта в тексте не
  остались описания от старых настроек. `None` — кэш принимается любым
- `texts` — `list[ParsedDocument]` (по одному на страницу; метаданные: `page`, `file`, при наличии `images`/`tables` — пути через `|`)
- `text` — `ParsedDocument` со всем текстом документа
- `tables`, `images` — `ParsedDocument` с медиа в метаданных
- `data_exists()` / `save_contents()` / `load_contents()` — управление кэшем

### PDFDoclingParser

`PDFDoclingParser(**kwargs)` — низкоуровневый движок, `extract_content(file_path) -> dict`.

Поддерживаемые kwargs:

- `generate_picture_images` (по умолчанию `True`)
- `image_describer` (по умолчанию `None`) — функция
  `PIL.Image -> str | None` (или `(image, context) -> str | None`),
  генерирующая текстовое описание изображения (например, VLM-вызов). Второй
  аргумент — контекст `{"kind", "page", "n", "caption", "page_text"}`: подпись
  и окно текста страницы вокруг неё. Без подписи (и для мелких
  иконок/логотипов) текст страницы не передаётся: фон только сбивает VLM.
  При заданном описателе
  маркеры `[IMAGE]` в постраничном и полном markdown получают описание рядом
  (`[IMAGE] <описание>`), а описания дублируются в поле `description` записей
  `images` и сохраняются в кэш. Переводы строк в ответе схлопываются: маркер
  живёт в строке markdown. Ошибка описателя не ломает парсинг — изображение
  остаётся с голым маркером `[IMAGE]`
- `table_describer` (по умолчанию `None`) — то же для таблиц: функция
  `PIL.Image -> str | None` (или `(image, context) -> str | None`) получает
  кроп области таблицы; в контексте — подпись, markdown таблицы
  (`table_markdown`, точные значения) и окно текста вокруг подписи. Маркеры `[TABLE]`
  становятся `[TABLE] <описание>`, описания дублируются в `description` записей
  `tables`. Бюджеты и ошибки независимы от `image_describer`
- `images_scale` (по умолчанию `2`) — масштаб рендеринга страниц (коннектор
  PDF просит 3: при 2 подписи на осях графика уже не читаются)

Бюджеты описаний задаёт вызывающий код: описатель вызывается на каждый
элемент, поэтому счётчик и лимит (`max_images`, `max_tables`) живут в самом
описателе — так картинки и таблицы не отбирают бюджет друг у друга. Коннектор
PDF отбрасывает иконки и логотипы мельче 200 px: описание по ним ничего не
даёт, а место в бюджете занимает. Движок по той же причине не передаёт мелким
картинкам текст страницы в контексте.

## Возвращаемые данные `extract_content()`

`PDFDoclingParser.extract_content` возвращает:

| ключ | значение |
|---|---|
| `texts` | `list[str]` — markdown постранично (индекс = page_no − 1) |
| `full_text` | `str` — markdown всего документа |
| `tables` | `list[dict]` — `df` (DataFrame), `text` (markdown), `image` (PIL), `page`, `n`, `caption`, `description` (`None` без `table_describer`) |
| `images` | `list[dict]` — `image` (PIL), `text`/`caption`, `page`, `n`, `description` (`None` без `image_describer`) |

### Маркеры в тексте

- `[TABLE]` + `Подпись к таблице: …` + `Столбцы таблицы: …` + `Строки таблицы: …` — вместо markdown-представления сложной таблицы; сама таблица передаётся как изображение (путь к файлу — в метаданных страницы и в `tables`). При заданном `table_describer` описание стоит на строке маркера: `[TABLE] <описание>`
- `[IMAGE]` — плейсхолдер изображения (путь — в метаданных); при заданном `image_describer` рядом с ним стоит текстовое описание: `[IMAGE] <описание>`
- `[START_LIST]…[END_LIST]`, `[START_INLINE]…[END_INLINE]`, `[START_KV]…[END_KV]` — структурные элементы (формы сериализуются штатным `MarkdownFormSerializer`, без маркеров)
- `(cid:NN)` — признак нечитаемой страницы (сломанная таблица глифов шрифта); такие страницы docling распознаёт своим OCR (Tesseract)

## Кэш

После первого парсинга результаты сохраняются в `<cache_dir>/<sha256>/`:

```
<hash>/
├── texts/1.txt, 2.txt, …    # постраничный markdown (описания изображений/таблиц — внутри)
├── cache_key                # версия рецепта (промпт/модель/масштаб): чужой кэш перепарсивается
├── tables/1__3.png          # вырез области таблицы
│   1__3.txt                 # описание таблицы (если table_describer его дал)
└── images/1__2.png          # изображение
    1__2.txt                 # описание изображения (если image_describer его дал)
```

При повторном запуске на тот же файл данные загружаются из кэша без повторного парсинга (полный текст документа при этом собирается из постраничных). Чтобы принудительно перепарсить — удалите каталог `<hash>` (или передайте другой `cache_key`).

## Подключение к EventGraph

Коннектор `pdf` (`eventgraph/connectors/kinds/pdf.py`) — единственный потребитель
пакета в этом репозитории. Он держит кэш в `<DATA_DIR>/pdf_cache`, просит
`images_scale=3` (при 2 подписи на осях графиков не читаются), передаёт
`image_describer`/`table_describer` с промптами описаний и подставляет
`cache_key` из хэша рецепта. Импорт парсера — только внутри `collect`, поэтому
API-процесс остаётся без torch и docling.

В контейнере системная часть ставится `Dockerfile`: CPU-сборка torch отдельным
слоем, `tesseract-ocr` + `tesseract-ocr-rus`, `TESSDATA_PREFIX=/usr/share/tessdata`.
`sitecustomize.py` в образе преимпортирует `tesserocr` при `EVENTGRAPH_ROLE=worker`:
cysignals ставит signal-обработчики при импорте, а коннектор парсит в потоке
(`asyncio.to_thread`), где это невозможно.

Как это выглядит в графе — корневой `README.md`, раздел «PDF-документы».

## Отличия от исходной реализации (`agentic_rag`)

- импорт внутри пакета, нет зависимости от `agentic_rag`
- `docling` импортируется лениво (пакет импортируется без установленной docling; pandas — только при обработке таблиц)
- движок pdfplumber удалён — единственный движок `docling`
- `langchain_core.Document` заменён собственным `ParsedDocument` (зависимость langchain не нужна)
- `logging.basicConfig` в модулях пакета заменён на модульные логгеры
- easyocr-OCR-fallback удалён (модули `ocr_utils`/`text_utils`): «нечитаемые» страницы распознает встроенный OCR docling
- убран мультifайловый `FileParser` — API пакета: `PdfFileManager` (по файлу)
- убраны несущественные совместимые параметры: `min_img_size`, `min_picture_page_surface_ratio`, `min_image_size`
- `do_picture_description` (VLM внутри docling) заменён внешним `image_describer`:
  описание изображения может генерировать любая функция, а не только встроенная модель
  (`table_describer` устроен так же и работает по кропу таблицы)
- кэш хранит только то, что восстанавливается при повторном запуске: `texts/*.txt` и PNG (весь markdown документа и `tables/*.xlsx`/`*.txt` не пишутся)
- `custom_export_to_markdown` урезан до реально используемых параметров; неиспользуемый `CustomMarkdownFormSerializer` удалён

## Ограничения

- поддерживаются только `.pdf` (остальные расширения игнорируются с warning)
- «нечитаемые» страницы распознаются только встроенным OCR docling (Tesseract, `rus`/`eng`) — нужно системное tesseract с языковыми пакетами
- настройки пайплайна docling зафиксированы: OCR через Tesseract (`rus`, `eng`), `TableFormerMode.ACCURATE`; для других настроек правьте `docling_parser.py`
- `custom_export_to_markdown` обходит всё дерево документа для каждой страницы (особенность docling) — на очень больших документах это замедляет работу

## Тесты

```bash
pip install pytest fpdf2
python -m pytest docling_parser/tests -v
```

- `test_api_compat.py` — быстрая проверка совместимости API docling/docling_core (без скачивания моделей)
- `test_utils.py` — лёгкие тесты утилит
- `test_docling_parser.py` — полный парсинг сгенерированных PDF через docling (медленно: при первом запуске скачиваются модели docling)

Тестовые PDF генерируются на лету (`tests/make_test_pdfs.py`, fpdf2 + Pillow), сеть не требуется; поэтому `tests/fixtures/` в репозиторий не входит и лежит в `.gitignore` — на каждом прогоне fpdf2 пишет новый `CreationDate`, и хэши файлов были бы разными. Пропустить тяжёлые docling-тесты: `SKIP_DOC_PDF_PARSER_HEAVY=1 python -m pytest docling_parser/tests -v`.

### Ручной smoke-тест

```bash
python docling_parser/examples/run_example.py /path/to/doc.pdf
```

Скрипт печатает статистику (страницы, таблицы, изображения) и сохраняет полный
markdown в `<cache-dir>/<stem>_full.md`. Результат глазами — в тексте записи и
на картинках кэша (`<cache_dir>/<sha256>/{images,tables}/`).
