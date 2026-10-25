"""Files a user attaches to a company profile → pages of text.

PDF goes through parser.pdf (docling with Tesseract OCR, the same engine as the
bank's library); Word, PowerPoint, Excel, HTML, RTF, OpenDocument and images
through docling's converter; plain text, Markdown and CSV are read as they are.
Documents without pages (Word, Excel) are cut into page-sized parts so that the
profile tools can read them in windows.

With images=True every page also carries the pictures on it (encoded bytes,
for a vision model): figures of Word, PowerPoint and Excel through docling,
image objects of a PDF through pypdfium2, an image file as itself. The text
marks each one as «[изображение N]» where it stands."""
from __future__ import annotations

import logging
from pathlib import Path
import re

log = logging.getLogger('alfa.parser.documents')

PAGE_CHARS = 4000
PLACEHOLDER = '<!-- image -->'
# Logos and icons below this size say nothing; a file gives at most MAX_IMAGES pictures.
MIN_IMAGE_EDGE = 80
MAX_IMAGES = 30
IMAGES = {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp'}
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


def _png(image) -> bytes | None:
    """A PIL picture as PNG bytes; None for one too small to matter."""
    from io import BytesIO
    if image is None or min(image.size) < MIN_IMAGE_EDGE:
        return None
    buffer = BytesIO()
    image.convert('RGBA' if 'A' in image.getbands() else 'RGB').save(buffer, format='PNG')
    return buffer.getvalue()


class _Numbering:
    """Numbers the pictures of one file in reading order, up to MAX_IMAGES."""
    def __init__(self):
        self.images: list[bytes] = []

    def add(self, data: bytes | None) -> str:
        if data is None or len(self.images) >= MAX_IMAGES:
            return ''
        self.images.append(data)
        return f'[изображение {len(self.images)}]'


def _attach(pages: list[str], numbering: _Numbering) -> list[dict]:
    """Pages with the pictures their «[изображение N]» marks point at."""
    out = []
    for text in pages:
        marks = [int(n) for n in re.findall(r'\[изображение (\d+)\]', text)]
        out.append({'text': text, 'images': [numbering.images[n - 1] for n in marks if 0 < n <= len(numbering.images)]})
    return out


def _docling_pages(path: Path, images: bool) -> list[dict]:
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TesseractOcrOptions
        from docling.document_converter import DocumentConverter, ImageFormatOption
        from docling_core.types.doc import PictureItem
    except ImportError as exc:
        raise RuntimeError(f'Для файлов {path.suffix} нужен docling (он есть в Docker-образе)') from exc
    image = ImageFormatOption(pipeline_options=PdfPipelineOptions(ocr_options=TesseractOcrOptions(lang=['rus', 'eng'])))
    document = DocumentConverter(format_options={InputFormat.IMAGE: image}).convert(str(path)).document
    numbering = _Numbering()

    def marked(page_no=None) -> str:
        # The markdown export puts a placeholder per picture in the same order as iterate_items.
        text = document.export_to_markdown(page_no=page_no, image_placeholder=PLACEHOLDER)
        if not images:
            return text
        pictures = iter([item for item, _ in document.iterate_items(page_no=page_no) if isinstance(item, PictureItem)])
        parts = text.split(PLACEHOLDER)
        out = [parts[0]]
        for part in parts[1:]:
            picture = next(pictures, None)
            try:
                data = _png(picture.get_image(document)) if picture is not None else None
            except Exception:  # noqa: BLE001 — a picture docling cannot render stays out
                data = None
            out += [numbering.add(data), part]
        return ''.join(out)

    if len(document.pages) > 1:  # presentations keep their slides as pages
        return _attach([marked(number) for number in sorted(document.pages)], numbering)
    return _attach(split_pages(marked()), numbering)


def _pdf_images(path: Path) -> dict[int, list[bytes]]:
    """Image objects of each PDF page (a scan is one page-sized picture)."""
    try:
        import pypdfium2 as pdfium
        import pypdfium2.raw as pdfium_c
    except ImportError:
        log.warning('pypdfium2 не установлен: картинки из %s не извлечены', path.name)
        return {}
    found, total = {}, 0
    pdf = pdfium.PdfDocument(str(path))
    try:
        for number, page in enumerate(pdf, 1):
            for obj in page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,), max_depth=3):
                if total >= MAX_IMAGES:
                    return found
                try:
                    data = _png(obj.get_bitmap(render=True).to_pil())
                except Exception:  # noqa: BLE001 — an unreadable image object is skipped
                    data = None
                if data:
                    found.setdefault(number, []).append(data)
                    total += 1
    finally:
        pdf.close()
    return found


def parse_upload(path: Path, images: bool = False) -> dict:
    """{'kind', 'pages': [{'page', 'text', 'extraction', 'images'}]}; ValueError for an unsupported type.
    'images' — encoded pictures of the page, filled only with images=True."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f'Формат {suffix or "без расширения"} не поддерживается')
    if suffix == '.pdf':
        from parser.pdf import parse_document
        pages = [{'text': p['text'], 'images': []} for p in parse_document(path)['pages']]
        extraction = 'docling-pdf'
        if images:
            numbering = _Numbering()
            for number, found in _pdf_images(path).items():
                if number <= len(pages):
                    # docling's PDF text has no placeholders: the marks go to the end of the page.
                    marks = [numbering.add(data) for data in found]
                    pages[number - 1]['text'] = '\n\n'.join([pages[number - 1]['text'], *filter(None, marks)])
            pages = [{'text': p['text'], 'images': page['images']} for p, page in zip(pages, _attach([p['text'] for p in pages], numbering))]
    elif suffix == '.csv':
        pages, extraction = [{'text': t, 'images': []} for t in split_pages(_csv_markdown(_read_text(path)))], 'csv'
    elif suffix in TEXT:
        pages, extraction = [{'text': t, 'images': []} for t in split_pages(_read_text(path))], 'text'
    else:
        pages, extraction = _docling_pages(path, images and suffix not in IMAGES), 'docling'
        if images and suffix in IMAGES:
            # The image file is itself the picture; OCR text stays for search.
            from PIL import Image
            with Image.open(path) as picture:
                data = _png(picture)
            text = '\n\n'.join(p['text'] for p in pages).replace(PLACEHOLDER, '').strip()
            pages = [{'text': f'[изображение 1]\n\n{text}'.strip() if data else text, 'images': [data] if data else []}]
    pages = [{'text': (p['text'] or '').replace(PLACEHOLDER, '').strip(), 'images': p['images']} for p in pages]
    if not any(p['text'] or p['images'] for p in pages):
        raise ValueError('В файле не найден текст')
    log.info('Файл %s: %d стр., %d изобр. (%s)', path.name, len(pages), sum(len(p['images']) for p in pages), extraction)
    return {'kind': kind(path.name), 'pages': [{'page': n, 'text': p['text'], 'extraction': extraction, 'images': p['images']}
                                               for n, p in enumerate(pages, 1)]}
