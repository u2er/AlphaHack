FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# tesseract — OCR сканов: docling (через tesserocr) и запасной движок parser/pdf.py.
# libtesseract-dev и g++ — сборка tesserocr, если под платформу нет готового колеса.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
        libtesseract-dev libleptonica-dev pkg-config g++ \
    && rm -rf /var/lib/apt/lists/*

# Зависимости — строго по poetry.lock, в системный Python образа (без venv).
# CPU-сборка torch зафиксирована там же (источник pytorch-cpu в pyproject.toml):
# CUDA-версия с PyPI весит несколько гигабайт.
ENV POETRY_VERSION=2.5.1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1 \
    POETRY_CACHE_DIR=/tmp/poetry-cache
COPY pyproject.toml poetry.lock ./
# Сам Poetry — в отдельном venv, чтобы его зависимости не спорили с lock-файлом.
RUN python -m venv /opt/poetry \
    && /opt/poetry/bin/pip install --no-cache-dir "poetry==${POETRY_VERSION}" \
    && /opt/poetry/bin/poetry install --no-root --with dev \
    && rm -rf "${POETRY_CACHE_DIR}"

COPY . .
RUN chmod +x entrypoint.sh \
    # Каталог данных. Именованный том при первом монтировании наследует
    # владельца отсюда — иначе сервис (uid 1000) не смог бы туда писать.
    # /data/pdfs — PDF, которые кладёт оператор (том ./pdfs из compose).
    && mkdir -p /data /data/pdfs /profiles \
    && chown -R 1000:1000 /data /profiles

# У uid 1000 в образе нет домашнего каталога, а кешам библиотек он нужен.
# tesserocr (OCR docling) сам языки Debian не находит: без этого — «No language models».
ENV HOME=/tmp \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

EXPOSE 8000

CMD ["./entrypoint.sh", "api"]
