FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
        # PDF-источник: tesseract — OCR docling (языки rus/eng).
        tesseract-ocr tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# CPU-сборка torch ставится до docling: иначе pip заберёт CUDA-версию (несколько гигабайт) с PyPI.
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt

# traineddata Tesseract (для OCR docling через tesserocr). manylinux-колесо
# tesserocr ищет их по TESSDATA_PREFIX, а Debian хранит в версионированном
# каталоге — подвешиваем стабильный путь.
RUN d=$(ls -d /usr/share/tesseract-ocr/*/tessdata | head -n1) \
    && ln -sfn "$d" /usr/share/tessdata

ENV TESSDATA_PREFIX=/usr/share/tessdata

# sitecustomize.py загружается интерпретатором до кода приложения: в воркере
# (EVENTGRAPH_ROLE=worker) он заранее импортирует tesserocr в главном потоке —
# cysignals ставит signal-обработчики при импорте, а это невозможно из потока
# коннектора, в котором работает PDF-парсер.
COPY sitecustomize.py /tmp/sitecustomize.py
RUN cp /tmp/sitecustomize.py "$(python -c 'import site; print(site.getsitepackages()[0])')/sitecustomize.py" \
    && rm /tmp/sitecustomize.py

COPY . .
RUN chmod +x entrypoint.sh \
    # Каталог данных. Именованный том при первом монтировании наследует
    # владельца отсюда — иначе сервис (uid 1000) не смог бы туда писать.
    # /data/pdfs — PDF, которые кладёт оператор (том ./pdfs из compose).
    && mkdir -p /data /data/pdfs \
    && chown 1000:1000 /data

# У uid 1000 в образе нет домашнего каталога, а кешам библиотек он нужен.
ENV HOME=/tmp

EXPOSE 8000

CMD ["./entrypoint.sh", "api"]
