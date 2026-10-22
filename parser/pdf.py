"""PDF of appendix 2 → the indexer contract of profile.db.prepare.

The main engine is docling (the docling_parser package, shared with the PDF
source of EventGraph, connectors/kinds/pdf.py): layout, tables, OCR of scans by
Tesseract. Its cache is the same <DATA_DIR>/pdf_cache/<sha256>, so a document is
parsed once for both. Without docling installed, pages come from the text
layer (pypdfium2) and scans are OCR'd by the tesseract binary.

PDF_PARSER: auto (docling if installed, else text layer + OCR), docling, text
(text layer only), ocr (tesseract only). The fallback engines cache their
result by SHA-256 too.
"""
from __future__ import annotations

from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess

log = logging.getLogger('alfa.parser.pdf')

ENGINES = ('auto', 'text', 'ocr', 'docling')
OCR_SCALE = 300 / 72  # render at 300 dpi
OCR_TIMEOUT = 300
CACHE_VERSION = 1

MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6, 'jul': 7, 'aug': 8,
    'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
    'янв': 1, 'фев': 2, 'мар': 3, 'апр': 4, 'мая': 5, 'май': 5, 'июн': 6, 'июл': 7,
    'авг': 8, 'сен': 9, 'окт': 10, 'ноя': 11, 'дек': 12,
}
_MONTH = '|'.join(sorted(MONTHS, key=len, reverse=True))
_NUMERIC = re.compile(r'\b(\d{1,2})[./](\d{1,2})[./](20\d{2})\b')
_WORDY = re.compile(rf'\b(\d{{1,2}})\s+({_MONTH})[а-яa-z]*\.?,?\s+(20\d{{2}})', re.I)
_WORDY_US = re.compile(rf'\b({_MONTH})[a-z]*\.?\s+(\d{{1,2}}),?\s+(20\d{{2}})', re.I)
# OCR often garbles the day of the header date ("Г сентября 2026"): month and year still count.
_MONTH_YEAR = re.compile(rf'\b({_MONTH})[а-яa-z]*\.?\s+(20\d{{2}})', re.I)
# Bank disclaimers carry the licence date ("лицензия ЦБ РФ № 1326 от 16.01.2015"), not the report's.
_LICENCE = re.compile(r'лиценз', re.I)


def _date(year, month, day=None):
    try:
        year, month = int(year), int(month)
        if not (2000 <= year <= 2100 and 1 <= month <= 12):
            return None
        if day is None:
            return f'{year:04d}-{month:02d}'
        day = int(day)
        return f'{year:04d}-{month:02d}-{day:02d}' if 1 <= day <= 31 else None
    except (TypeError, ValueError):
        return None


def date_from_text(text):
    """First date on the first page: 11.09.2026, 11 сентября 2026, September 11, 2026,
    or just «сентября 2026» when OCR lost the day. Licence dates of disclaimers are skipped."""
    text, found = text or '', []
    for pattern, order in ((_NUMERIC, 'dmy'), (_WORDY, 'dMy'), (_WORDY_US, 'Mdy'), (_MONTH_YEAR, 'My')):
        for match in pattern.finditer(text):
            if _LICENCE.search(text[max(0, match.start() - 60):match.start()]):
                continue
            groups = dict(zip(order, match.groups()))
            month = groups.get('m') or MONTHS.get(groups['M'][:3].lower())
            if value := _date(groups['y'], month, groups.get('d')):
                found.append((match.start(), -len(value), value))  # same start: the full date wins
    return min(found)[2] if found else None


def date_from_name(name):
    """Approximate date from file names like FI_Strategy_3Q26_22072026, ..._15Apr2021, Gold_digest_August26."""
    stem = Path(name).stem
    if m := re.search(r'(?<!\d)(\d{2})(\d{2})(20\d{2})(?!\d)', stem):
        if value := _date(m[3], m[2], m[1]):
            return value
    if m := re.search(rf'(\d{{1,2}})({_MONTH})[a-z]*(20\d{{2}})', stem, re.I):
        if value := _date(m[3], MONTHS[m[2][:3].lower()], m[1]):
            return value
    if m := re.search(rf'({_MONTH})[a-z]*[-_ ]?(\d{{2}})(?!\d)', stem, re.I):
        if value := _date(2000 + int(m[2]), MONTHS[m[1][:3].lower()]):
            return value
    if m := re.search(r'(\d)Q(\d{2})(?!\d)', stem, re.I):
        return _date(2000 + int(m[2]), 3 * int(m[1]))
    if m := re.search(r'(\d{1,2})M(\d{2})(?!\d)', stem, re.I):
        return _date(2000 + int(m[2]), int(m[1]))
    if m := re.search(r'([12])H(\d{2})(?!\d)', stem, re.I):
        return _date(2000 + int(m[2]), 6 * int(m[1]))
    return None


def date_hint(name, first_page):
    return date_from_text(first_page) or date_from_name(name) or 'unknown'


def _cache_dir():
    configured = os.getenv('PARSER_CACHE_DIR', '').strip()
    candidates = [Path(configured)] if configured else []
    candidates += [Path(os.getenv('DATA_DIR', '/data')) / 'parser-cache',
                   Path.home() / '.cache' / 'alfahack' / 'parser-cache']
    for folder in candidates:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            return folder
        except OSError:
            continue
    return None


def docling_available():
    from importlib.util import find_spec
    return find_spec('docling') is not None and find_spec('docling_parser') is not None


def _engine():
    engine = os.getenv('PDF_PARSER', 'auto').strip().lower() or 'auto'
    if engine not in ENGINES:
        raise ValueError(f'PDF_PARSER must be one of {", ".join(ENGINES)}')
    if engine == 'auto' and docling_available():
        return 'docling'
    return engine


def ocr_image(image):
    """Tesseract on one PIL image; the binary is called directly, no Python wrapper needed."""
    binary = shutil.which('tesseract')
    if not binary:
        raise RuntimeError('Tesseract is not installed: apt install tesseract-ocr tesseract-ocr-rus '
                           '(brew install tesseract tesseract-lang), or set PDF_PARSER=docling')
    from io import BytesIO
    buffer = BytesIO()
    image.save(buffer, format='PNG')
    languages = os.getenv('OCR_LANGUAGES', 'rus+eng')
    result = subprocess.run([binary, 'stdin', 'stdout', '-l', languages, '--psm', '3'],
                            input=buffer.getvalue(), capture_output=True, timeout=OCR_TIMEOUT, check=False)
    if result.returncode:
        raise RuntimeError(f'Tesseract failed: {result.stderr.decode("utf-8", "replace")[-300:]}')
    return result.stdout.decode('utf-8', 'replace')


def _pdfium_pages(path, engine):
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        pages = []
        for number in range(len(document)):
            page = document[number]
            text, extraction = '', 'pdfium-text'
            if engine in ('auto', 'text'):
                textpage = page.get_textpage()
                text = textpage.get_text_bounded()
                textpage.close()
            if not text.strip() and engine in ('auto', 'ocr'):
                image = page.render(scale=OCR_SCALE).to_pil()
                text, extraction = ocr_image(image), 'tesseract-ocr'
            page.close()
            pages.append({'page': number + 1, 'text': text.strip(), 'extraction': extraction})
        return pages
    finally:
        document.close()


def _docling_cache():
    """The PDF source's cache (connectors/kinds/pdf.py): <DATA_DIR>/pdf_cache."""
    for folder in (Path(os.getenv('DATA_DIR', '/data')) / 'pdf_cache',
                   Path.home() / '.cache' / 'alfahack' / 'pdf_cache'):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            return folder
        except OSError:
            continue
    raise RuntimeError('No writable cache directory for docling')


def _docling_pages(path):
    if not docling_available():
        raise RuntimeError('PDF_PARSER=docling needs docling and the docling_parser package installed')
    from docling_parser.file_manager import PdfFileManager

    manager = PdfFileManager(file_path=str(path), cache_dir=str(_docling_cache()))
    manager.extract_content()
    pages = sorted(manager.texts, key=lambda doc: int(doc.metadata['page']))
    return [{'page': number, 'text': (doc.page_content or '').strip(), 'extraction': 'docling'}
            for number, doc in enumerate(pages, 1)]


def parse_document(path: Path):
    """{'date_hint', 'pages': [{'page', 'text', 'extraction'}]} — see profile/db/README.md."""
    path = Path(path)
    engine = _engine()
    digest = sha256(path.read_bytes()).hexdigest()
    cache = _cache_dir()
    cached = cache / f'{digest}.{engine}.json' if cache else None
    if cached and cached.is_file():
        data = json.loads(cached.read_text(encoding='utf-8'))
        if data.get('version') == CACHE_VERSION:
            log.info('PDF %s: из кэша %s', path.name, cached)
            return data['document']
    log.info('PDF %s: разбор (%s)', path.name, engine)
    if engine == 'docling':
        # docling keeps its own cache, shared with the EventGraph PDF source.
        pages = _docling_pages(path)
        return {'date_hint': date_hint(path.name, pages[0]['text'] if pages else ''), 'pages': pages}
    pages = _pdfium_pages(path, engine)
    document = {'date_hint': date_hint(path.name, pages[0]['text'] if pages else ''), 'pages': pages}
    if cached:
        tmp = cached.with_suffix('.tmp')
        tmp.write_text(json.dumps({'version': CACHE_VERSION, 'document': document}, ensure_ascii=False),
                       encoding='utf-8')
        tmp.replace(cached)
    return document
