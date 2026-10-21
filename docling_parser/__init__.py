"""docling_parser — извлечение текста, таблиц и изображений из PDF-документов.

Движок: docling. Парсер извлечён из проекта `agentic-rag` и собран в
самодостаточный модуль.

Быстрый старт:
    from docling_parser import PdfFileManager

    manager = PdfFileManager(file_path="doc.pdf", cache_dir="./cache")
    manager.extract_content()

    texts  = manager.texts    # list[ParsedDocument], по одному на страницу
    tables = manager.tables   # list[ParsedDocument], метаданные: df, image, page, n
    images = manager.images   # list[ParsedDocument], метаданные: image, page, n
"""

from docling_parser.document import ParsedDocument
from docling_parser.file_manager import PdfFileManager

__all__ = [
    "ParsedDocument",
    "PdfFileManager",
    "PDFDoclingParser",
]


def __getattr__(name):
    # Ленивый импорт PDFDoclingParser: пакет остаётся импортируемым без установленной docling
    if name == "PDFDoclingParser":
        from docling_parser.docling_parser import PDFDoclingParser
        return PDFDoclingParser
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
