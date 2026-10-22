# Парсеры входных данных

`csv.py` читает CSV приложения 1 с разделителем `;` и кодировкой UTF-8. `loader.py` подключает переданный индексатору модуль из пакета `parser`.

`pdf.py` — парсер PDF приложения 2 по умолчанию (`parser.pdf:parse_document`): движок docling (`docling_parser`, общий с PDF-источником EventGraph и его кэшем), без docling — текстовый слой pypdfium2 и OCR Tesseract; дата с первой страницы или из имени файла. Контракт результата, режимы и команда запуска описаны в [profile/db/README.md](../profile/db/README.md).
