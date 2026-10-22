"""Автозагружается интерпретатором (site.py) до запуска кода приложения.

В воркер-процессе заранее импортирует tesserocr в главном потоке: библиотека
через cysignals устанавливает signal-обработчики при импорте, а это возможно
только в главном потоке. PDF-парсер (движок docling) коннектор запускает в
потоке, и без преимпорта OCR-движок падает с ошибкой
"signal only works in main thread of the main interpreter".

Включается переменной EVENTGRAPH_ROLE=worker (ставится в docker-compose.yml).
В остальных процессах файл ни на что не влияет.
"""
import os

if os.environ.get("EVENTGRAPH_ROLE") == "worker":
    try:
        import tesserocr  # noqa: F401
    except Exception:
        # tesserocr необязателен (OCR docling может работать и без него,
        # если PDF не требует распознавания) — не ломаем запуск.
        pass
