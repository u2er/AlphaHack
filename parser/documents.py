"""Files a user attaches to a company profile → pages of text.

PDF goes through parser.pdf (docling with Tesseract OCR, the same engine as the
bank's library); Word, PowerPoint, Excel, HTML, RTF, OpenDocument and images
through docling's converter; plain text, Markdown and CSV are read as they are.
Documents without pages (Word, Excel) are cut into page-sized parts so that the
profile tools can read them in windows."""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger('alfa.parser.documents')

PAGE_CHARS = 4000
TEXT = {'.txt', '.md', '.csv'}
DOCLING = {'.docx', '.doc', '.rtf', '.pptx', '.ppt', '.xlsx', '.xls', '.html', '.htm', '.odt', '.ods', '.odp',
           '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp'}
SUPPORTED = {'.pdf'} | TEXT | DOCLING
KINDS = {'.pdf': 'PDF', '.docx': 'Word', '.doc': 'Word', '.rtf': 'RTF', '.odt': 'Документ', '.pptx': 'Презентация',
         '.ppt': 'Презентация', '.odp': 'Презентация', '.xlsx': 'Таблица', '.xls': 'Таблица', '.ods': 'Таблица',
         '.csv': 'Таблица', '.html': 'Веб-страница', '.htm': 'Веб-страница', '.txt': 'Текст', '.md': 'Текст'}


def kind(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return KINDS.get(suffix, 'Изображение' if suffix in DOCLING else 'Файл')


def split_pages(text: str, size: int = PAGE_CHARS) -> list[str]:
    """Page-sized parts of a long text, cut at paragraph or line boundaries."""
    text, pages = text.strip(), []
    while len(text) > size:
        cut = max(text.rfind('\n\n', 0, size), text.rfind('\n', 0, size))
        cut = cut if cut > size // 2 else size
        pages.append(text[:cut].strip())
        text = text[cut:].strip()
    return pages + ([text] if text else [])


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ('utf-8-sig', 'cp1251'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', 'replace')


def _csv_markdown(text: str) -> str:
    """CSV as a Markdown table, so rows stay rows on the page and in search fragments."""
    import csv
    from io import StringIO
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=';,\t|')
    except csv.Error:
        dialect = csv.excel
    rows = [[cell.strip().replace('|', '/') for cell in row] for row in csv.reader(StringIO(text), dialect) if any(row)]
    if not rows:
        return text
    width = max(len(row) for row in rows)
    rows = [row + [''] * (width - len(row)) for row in rows]
    lines = ['| ' + ' | '.join(rows[0]) + ' |', '|' + ' --- |' * width]
    return '\n'.join(lines + ['| ' + ' | '.join(row) + ' |' for row in rows[1:]])


def _docling_markdown(path: Path) -> list[str]:
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TesseractOcrOptions
        from docling.document_converter import DocumentConverter, ImageFormatOption
    except ImportError as exc:
        raise RuntimeError(f'Для файлов {path.suffix} нужен docling (он есть в Docker-образе)') from exc
    image = ImageFormatOption(pipeline_options=PdfPipelineOptions(ocr_options=TesseractOcrOptions(lang=['rus', 'eng'])))
    document = DocumentConverter(format_options={InputFormat.IMAGE: image}).convert(str(path)).document
    if len(document.pages) > 1:  # presentations keep their slides as pages
        return [document.export_to_markdown(page_no=number) for number in sorted(document.pages)]
    return split_pages(document.export_to_markdown())


def parse_upload(path: Path) -> dict:
    """{'kind', 'pages': [{'page', 'text', 'extraction'}]}; ValueError for an unsupported type."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f'Формат {suffix or "без расширения"} не поддерживается')
    if suffix == '.pdf':
        from parser.pdf import parse_document
        pages = [p['text'] for p in parse_document(path)['pages']]
        extraction = 'docling-pdf'
    elif suffix == '.csv':
        pages, extraction = split_pages(_csv_markdown(_read_text(path))), 'csv'
    elif suffix in TEXT:
        pages, extraction = split_pages(_read_text(path)), 'text'
    else:
        pages, extraction = _docling_markdown(path), 'docling'
    pages = [(text or '').replace('<!-- image -->', '').strip() for text in pages]
    if not any(pages):
        raise ValueError('В файле не найден текст')
    log.info('Файл %s: %d стр. (%s)', path.name, len(pages), extraction)
    return {'kind': kind(path.name), 'pages': [{'page': n, 'text': text, 'extraction': extraction}
                                               for n, text in enumerate(pages, 1)]}
