# THIRD_PARTY_NOTICES

Сторонние компоненты, модели, данные, сервисы и смоделированные интеграции,
которые использует EventGraph. Версии — как в `requirements.txt`, `Dockerfile`
и `docker-compose.yml` на момент сдачи; лицензии — по данным авторов
компонентов, полный текст — в дистрибутиве каждого пакета.

## 1. Языковые модели (LLM)

Все агенты (`news`, `analyst`, `gardener`, чаты, онбординг профиля) ходят в
одну модель через OpenAI-совместимый API (`LLM_BASE_URL`, `LLM_MODEL`).
В решении допускаются только модели из приложения 3 кейса, через общий ключ
организаторов:

| модель | назначение по кейсу |
|---|---|
| Gemma 4 26B A4B | классификация, извлечение, простые преобразования |
| GLM-5.3-Flash | задачи повышенной сложности, рассуждения |
| Qwen3.6 35B A3B | универсальная: анализ и генерация текста |
| DeepSeek-V4.1-Flash | поиск взаимосвязей, большие объёмы информации |
| Qwen3-VL-30B-A3B-Instruct | изображения и мультимодальные данные |
| Kimi-K2.6 | обработка и анализ текста |

Внешние AI-сервисы, автономные агенты и готовые мультиагентные фреймворки не
используются: цикл агента (`eventgraph/agents/runner.py`), инструменты и
оркестрация написаны в проекте.

## 2. Прочие модели

| модель | где | лицензия |
|---|---|---|
| `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` | эмбеддер графа, сюжетов, профиля и ранжирования веб-поиска (`EMB_MODEL`, через fastembed/ONNX) | Apache-2.0 |
| модели разметки страниц и таблиц docling (Docling Models, Hugging Face) | разбор PDF (`docling_parser/`, коннектор `pdf`) | см. карточку модели на Hugging Face |
| языковые данные Tesseract `rus`, `eng` (пакеты Debian `tesseract-ocr-*`) | OCR сканов | Apache-2.0 |

## 3. Данные

| данные | источник | как используется |
|---|---|---|
| `df_Uid_data_hack.csv`, `df_Fid_Uid_link_hack.csv`, `df_Uid_payment_hack.csv` | приложение 1 кейса, АО «Альфа-Банк»; обезличенные | библиотека компаний `profile_data`: поиск компании, поля профиля, связи по платежам и физлицам |
| PDF Центра макроэкономических исследований Альфа-Банка | приложение 2 кейса | коннектор `alfa_analytics` (запись — страница отчёта) и библиотека профиля |
| `eventgraph/seed/news-graph.json.gz`, `embeddings.npz` | собраны командой из новостей открытых источников (раздел 4) этим же решением | базовый граф на 21.10.2026 и готовые векторы |
| `eventgraph/seed/profiles/*.json`, `tests/fixtures/profiles/*.json` | синтетические: собраны агентом онбординга из полей приложения 1; ВекторФуд и СибМеталл вымышлены | профили-примеры и тесты |

Данные кейса используются только в рамках хакатона и не позволяют
идентифицировать реальные организации или людей.

## 4. Внешние источники и сервисы

| сервис | как используется | условия |
|---|---|---|
| Lenta.ru — RSS рубрики «Экономика» | коннектор `lenta_economics`, раз в 30 минут | публичная RSS-лента; robots.txt соблюдается |
| Telegram, публичный канал @alfa_investments (`t.me/s/…`) | коннектор `alfa_investments` | публичное веб-превью канала |
| alfabank.ru/alfa-investor | коннектор `alfa_investor_site` — **выключен** (сайт закрыт защитой от ботов; не обходится) | — |
| сайты и Telegram-каналы, которые добавляет пользователь | пользовательские источники (`website`, `telegram_channel`) | robots.txt соблюдается |
| SearXNG (метапоисковик, свой экземпляр в compose) и поисковые системы, к которым он обращается по умолчанию | инструмент агентов `web_search` / `web_page` | SearXNG — AGPL-3.0, используется без изменений как отдельный сервис |
| PyTorch CPU index (`download.pytorch.org`), PyPI, Docker Hub, Hugging Face | загрузка зависимостей и моделей при сборке и первом запуске | — |

## 5. Смоделированные интеграции

| что | как смоделировано |
|---|---|
| Интернет-банк «Альфа-Бизнес» | `gui/` — прототип интерфейса (главная с точкой входа, «Бизнес-аналитика», профиль, граф). Не связан с реальным интернет-банком, авторизации нет: пользователь выбирает компанию переключателем. |
| Данные о клиенте из систем банка | CSV приложения 1, загруженные в схему `profile_data`. |
| Поток аналитики банка | PDF приложения 2 и каталог `./pdfs` вместо внутренней системы публикаций. |
| Шлюз LLM банка | OpenAI-совместимый эндпоинт организаторов (раздел 1). |

## 6. Python-пакеты (`requirements.txt`)

| пакет | версия | лицензия |
|---|---|---|
| fastapi | 0.115.6 | MIT |
| uvicorn[standard] | 0.34.0 | BSD-3-Clause |
| pydantic | 2.10.4 | MIT |
| pydantic-settings | >=2.14,<3 | MIT |
| SQLAlchemy[asyncio] | 2.0.36 | MIT |
| asyncpg | 0.30.0 | Apache-2.0 |
| pgvector (Python) | 0.5.1 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| tzdata | 2026.5 | Apache-2.0 |
| python-multipart | 0.0.20 | Apache-2.0 |
| openai (SDK) | 2.39.0 | Apache-2.0 |
| Pillow | 11.1.0 | MIT-CMU (HPND) |
| beautifulsoup4 | 4.15.0 | MIT |
| lxml | 6.1.3 | BSD-3-Clause |
| feedparser | 6.0.14 | BSD-2-Clause |
| trafilatura | 2.3.1 | Apache-2.0 |
| pypdf | 5.1.0 | BSD-3-Clause |
| h2 | 4.1.0 | MIT |
| fastembed | 0.9.0 | Apache-2.0 |
| numpy | 2.5.4 | BSD-3-Clause |
| psycopg[binary] | 3.2.3 | LGPL-3.0 |
| python-dotenv | 1.2.4 | BSD-3-Clause |
| docling | >=2.110,<3 | MIT |
| tesserocr | >=2.8 | MIT |
| transformers | >=4.46,<5 | Apache-2.0 |
| pypdfium2 (с PDFium) | 4.30.0 | Apache-2.0 / BSD-3-Clause |
| pytest | 8.3.4 | MIT |
| pytest-asyncio | 0.25.0 | Apache-2.0 |
| torch, torchvision (CPU-сборка, ставятся в Dockerfile) | последние на момент сборки | BSD-3-Clause |

Транзитивные зависимости (в том числе pandas, tabulate, onnxruntime,
huggingface-hub) приходят с пакетами выше под их собственными лицензиями.

## 7. Образы и системные компоненты

| компонент | версия | лицензия |
|---|---|---|
| `python:3.12-slim` (Debian) | 3.12 | PSF-2.0; пакеты Debian — свои лицензии |
| Tesseract OCR, Leptonica (apt) | из Debian | Apache-2.0 / BSD-2-Clause |
| `pgvector/pgvector:pg16` — PostgreSQL 16 с pgvector | pg16 | PostgreSQL License |
| `searxng/searxng` | 2026.10.19-81371861f | AGPL-3.0 |
