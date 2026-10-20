FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x entrypoint.sh \
    # Каталог данных. Именованный том при первом монтировании наследует
    # владельца отсюда — иначе сервис (uid 1000) не смог бы туда писать.
    && mkdir -p /data \
    && chown 1000:1000 /data

# У uid 1000 в образе нет домашнего каталога, а кешам библиотек он нужен.
ENV HOME=/tmp

EXPOSE 8000

CMD ["./entrypoint.sh", "api"]
