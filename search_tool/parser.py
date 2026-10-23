"""Fetch public pages with bounded size, then extract HTML/text/PDF content."""
import hashlib
import importlib.util
import io
import ipaddress
import json
import logging
import re
import socket
from datetime import datetime
from time import perf_counter
from urllib.parse import urlsplit, urljoin
import httpx
from bs4 import BeautifulSoup
from .models import ParsedDocument, Chunk
from .search import canonical_url

log = logging.getLogger("alfa.search_tool.parser")


class ParseError(RuntimeError):
    pass


def public_url(url: str):
    p = urlsplit(canonical_url(url))
    if p.port not in {None, 80, 443}:
        raise ParseError("Nonstandard article port blocked")
    try:
        addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ParseError("Non-public article address blocked")
    except OSError:
        raise ParseError("Article DNS resolution failed") from None


def clean(value):
    return re.sub(r"\s+", " ", value).strip()


def remove_image_captions(soup):
    """Use image markup, never delete prose merely because it mentions a photo."""
    for node in soup.select(
        'figcaption,[itemprop="caption"],.wp-caption-text,.photo-caption,'
        '.image-caption,.image__caption,.photo__caption,.photo-credit,'
        '.image-credit,.block-image .description,.block-image .caption'
    ):
        if node.attrs is not None:
            node.decompose()


def parse_content(raw: bytes, url: str, content_type: str) -> ParsedDocument:
    mime = content_type.split(";")[0].strip().lower()
    if raw.startswith(b"%PDF-"):
        mime = "application/pdf"
    title, date, author = "", None, None
    if mime == "application/pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ParseError("Install pypdf for PDF parsing") from None
        try:
            reader = PdfReader(io.BytesIO(raw))
            if reader.is_encrypted or len(reader.pages) > 200:
                raise ParseError("Encrypted or oversized PDF")
            title = str((reader.metadata or {}).get("/Title", "")) or urlsplit(url).path
            text = "\n".join(f"[Page {i+1}]\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages))
        except ParseError:
            raise
        except Exception:
            raise ParseError("PDF extraction failed; scanned PDFs require OCR") from None
    elif mime in {"text/html", "application/xhtml+xml", ""}:
        soup = BeautifulSoup(raw, "html.parser")
        challenge_page = bool(soup.select_one("js-challenge-loader, [id*='captcha'], [class*='captcha']"))
        node = soup.select_one('meta[name="citation_title"],meta[name="DC.title"]')
        if node and node.get("content"):
            title = clean(node["content"])
        else:
            node = soup.select_one('meta[property="og:title"]')
            title = clean(node["content"]) if node and node.get("content") else ""
            if not title:
                node = soup.find("h1") or soup.find("title")
                title = clean(node.get_text(" ")) if node else urlsplit(url).path
        for node in soup.select('meta[property="article:published_time"], meta[itemprop="datePublished"], time[datetime]'):
            value = node.get("content") or node.get("datetime")
            try:
                date = datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
                break
            except (ValueError, AttributeError):
                pass
        node = soup.select_one('meta[name="author"]')
        author = node.get("content") if node else None
        bodies = []
        for node in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(node.get_text())
                pending = data if isinstance(data, list) else [data]
                while pending:
                    obj = pending.pop()
                    if not isinstance(obj, dict):
                        continue
                    pending.extend(obj.get("@graph", []))
                    if isinstance(obj.get("articleBody"), str):
                        body_soup = BeautifulSoup(obj["articleBody"], "html.parser")
                        remove_image_captions(body_soup)
                        bodies.append(clean(body_soup.get_text(" ")))
            except (ValueError, TypeError):
                continue
        remove_image_captions(soup)
        # Remove complete UI blocks before choosing ONE article container.
        noise = re.compile(
            r"(?:^|[-_\s])(?:menu|navigation|breadcrumb[s]?|paywall|subscribe|subscription|"
            r"recommendation[s]?|related|similar|social|share|comments?|cookie[s]?|"
            r"advertisement|adfox|tooltip)(?:$|[-_\s])", re.I)
        for node in list(soup.find_all(True)):
            if node.attrs is None:
                continue  # An ancestor was already removed.
            label = " ".join(node.get("class", [])) + " " + str(node.get("id", ""))
            if (node.name in {"script", "style", "nav", "footer", "aside", "form", "button"}
                    or node.get("role") in {"navigation", "banner", "contentinfo"}
                    or noise.search(label)):
                node.decompose()
        # Prefer dedicated text bodies over broad page/main wrappers. Never join
        # every <article>: recommendation feeds often use the same tag.
        containers = []
        for selector in (
            '[itemprop="articleBody"],.article-boxes-list,.news-detail__text',
            '.article-body,.article__text,.article-content,.entry-content,.post-content',
            'article',
            'main,[role="main"],[id="main"],[id="content"]',
            '.lead-text,.landing-text',
            'body',
        ):
            containers = soup.select(selector)
            if containers:
                break
        container = max(containers, key=lambda n: len(clean(n.get_text(" ")))) if containers else None
        text = max(bodies, key=len) if bodies else (container.get_text("\n", strip=True) if container else "")
        # Bibliography is a terminal section, matched only as a standalone heading.
        text = re.split(r"(?im)^\s*(?:Библиографическая ссылка|Список литературы|"
                        r"Библиография|References|Bibliography)\s*[:.]?\s*$", text, maxsplit=1)[0].rstrip()
        if not text:
            if challenge_page:
                raise ParseError("JavaScript challenge page; article text unavailable")
            raise ParseError("No extractable HTML text")
    elif mime == "text/plain":
        title, text = urlsplit(url).path, raw.decode("utf-8", errors="replace")
    else:
        raise ParseError("Unsupported content type")
    if not clean(text):
        raise ParseError("No extracted text")
    return ParsedDocument(url=url, title=title, text=text, published_at=date,
                          author=author, content_type=mime, raw_sha256=hashlib.sha256(raw).hexdigest())


class DocumentParser:
    def __init__(self, client=None, url_guard=public_url, max_bytes=5_000_000):
        if client is None:
            http2_available = importlib.util.find_spec("h2") is not None
            if not http2_available:
                log.warning("HTTP/2 недоступен: установите зависимости через pip install -r requirements.txt")
            client = httpx.Client(timeout=25, verify=False, http2=http2_available,
                follow_redirects=False, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/120.0.0.0 Safari/537.36",
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                })
        self.client = client
        self.guard, self.max_bytes = url_guard, max_bytes

    def close(self):
        self.client.close()

    def fetch(self, url: str) -> ParsedDocument:
        target = canonical_url(url)
        started = perf_counter()
        try:
            for _ in range(5):
                self.guard(target)
                with self.client.stream("GET", target) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ParseError("Redirect without Location")
                        target = canonical_url(urljoin(target, location))
                        log.debug("Переадресация на %s", urlsplit(target).hostname)
                        continue
                    if response.status_code != 200:
                        raise ParseError(f"Article HTTP {response.status_code}")
                    parts, size = [], 0
                    for part in response.iter_bytes():
                        size += len(part)
                        if size > self.max_bytes:
                            raise ParseError("Article exceeds byte limit")
                        parts.append(part)
                    document = parse_content(b"".join(parts), target, response.headers.get("content-type", ""))
                    log.debug("Разбор %s: %s, ответ=%d байт, текст=%d символов, %.2f с",
                              urlsplit(target).hostname, document.content_type, size,
                              len(document.text), perf_counter() - started)
                    return document
            raise ParseError("Too many redirects")
        except httpx.HTTPError as exc:
            log.debug("Сетевая ошибка %s: %s: %s", urlsplit(target).hostname,
                      type(exc).__name__, exc)
            raise ParseError(f"Article network error: {type(exc).__name__}") from None


def sentence_ends(text):
    """Conservative punctuation boundaries; preserve abbreviations and decimals."""
    abbreviations = {"г", "гг", "млн", "млрд", "тыс", "куб", "руб", "т", "др", "им", "ул", "стр"}
    ends = []
    for match in re.finditer(r'[.!?…]+[»”"\)\]]*(?:\s+|$)', text):
        punctuation = match.group().strip()
        before = text[:match.start()]
        word = re.search(r"([\w]+)$", before)
        tail = text[match.end():]
        if punctuation == ".":
            if word and (word.group().lower() in abbreviations or
                         (len(word.group()) == 1 and word.group().isupper())):
                continue
            if tail and tail[0].islower():
                continue
        ends.append(match.end())
    if not ends or ends[-1] != len(text):
        ends.append(len(text))
    return ends


def split_text(text, tokenizer, size=448, overlap=64):
    """Pack whole sentences; preserve every character and original offsets.

    size is a soft token budget: an oversized sentence stays intact. The ranker
    embeds long passages in bounded windows without truncating their tail.
    """
    from bisect import bisect_left
    if size < 1 or not 0 <= overlap < size:
        raise ValueError("Require size > overlap >= 0")
    if not text:
        return []
    offsets = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True,
                        truncation=False)["offset_mapping"]
    token_starts = [start for start, _ in offsets]
    boundaries = [0, *sentence_ends(text)]
    counts = [bisect_left(token_starts, boundary) for boundary in boundaries]
    chunks, i = [], 0
    while i < len(boundaries) - 1:
        j = i + 1
        while j + 1 < len(boundaries) and counts[j + 1] - counts[i] <= size:
            j += 1
        start, end = boundaries[i], boundaries[j]
        chunks.append(Chunk(id=f"C{len(chunks)+1}", ordinal=len(chunks),
                            start=start, end=end, text=text[start:end]))
        if j == len(boundaries) - 1:
            break
        next_i = j
        while next_i - 1 > i and counts[j] - counts[next_i - 1] <= overlap:
            next_i -= 1
        i = next_i
    return chunks
