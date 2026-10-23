"""Fetch public pages with bounded size, then extract HTML/text/PDF content."""
import hashlib
import importlib.util
import io
import ipaddress
import json
import logging
import re
import socket
import ssl
from datetime import datetime
from time import perf_counter
from urllib.parse import urlsplit, urljoin
import httpx
from bs4 import BeautifulSoup
from .models import ParsedDocument
from .search import canonical_url

log = logging.getLogger("alfa.parser")


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
        body = ""
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
                        body = clean(BeautifulSoup(obj["articleBody"], "html.parser").get_text(" "))
            except (ValueError, TypeError):
                continue
        for node in soup.select("script,style,nav,header,footer,aside,form,.breadcrumbs"):
            node.decompose()
        containers = soup.select('[itemprop="articleBody"],article')
        if not containers:
            # Many pages put their content in a div with a main role/id instead of <main>.
            candidates = soup.select('main,[role="main"],[id="main"],[id="content"]')
            containers = [max(candidates, key=lambda n: len(clean(n.get_text(" "))))] if candidates else []
        if not containers:
            containers = soup.select(".lead-text,.landing-text")
        if not containers and soup.body:
            # Last resort for pages with no semantic container; boilerplate was removed above.
            containers = [soup.body]
        text = body or "\n".join(clean(n.get_text(" ")) for n in containers)
        if not text:
            if challenge_page:
                raise ParseError("JavaScript challenge page; article text unavailable")
            raise ParseError("No extractable HTML text")
    elif mime == "text/plain":
        title, text = urlsplit(url).path, raw.decode("utf-8", errors="replace")
    else:
        raise ParseError("Unsupported content type")
    if len(clean(text)) < 160:
        raise ParseError("Insufficient extracted text")
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
                        target = canonical_url(urljoin(target, response.headers["location"]))
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
