# Раздел новостей сайта: находит новые материалы и забирает их текст.
#
# Настройки (`config` источника в sources.py, приходят в ctx.config):
#   url              — страница раздела, например https://news.tsu.ru/news/;
#   mode             — как искать новое: auto (по умолчанию) | rss | sitemap | listing;
#   feed_url         — адрес RSS/Atom, если сам он не находится;
#   link_pattern     — регулярное выражение для ссылок на материалы, если
#                      листинг путает их с рубриками и тегами;
#   page_param       — параметр пагинации листинга (?page=2), если нет rel="next";
#   max_pages        — сколько страниц листинга листать за запуск (3);
#   max_new_per_run  — сколько новых материалов забирать за запуск (30);
#   backfill         — сколько последних материалов взять при первом запуске (10);
#   images           — собирать картинки материала (true): обложку og:image и
#                      картинки из тела статьи, без логотипов и иконок;
#   max_images       — не больше стольких картинок на запись (5);
#   respect_robots   — соблюдать robots.txt (true);
#   delay            — пауза между запросами к сайту, секунды (1);
#   user_agent       — свой User-Agent, если сайт не пускает чужих.
#
# Стратегия выбирается при первом запуске (mode: auto — первая сработавшая):
#   1. rss     — лента, объявленная на странице или по обычным адресам; лучший
#                сигнал — стабильные ссылки и точные даты;
#   2. sitemap — sitemap.xml из robots.txt или корня, ссылки под путём раздела;
#   3. listing — сама страница раздела с пагинацией: ссылки того же сайта
#                глубже пути раздела, вне шапки, меню и подвала.
# Она запоминается в ctx.state; сброс курсора (POST /connectors/{name}/state/reset)
# выбирает её заново.
#
# Новый материал — тот, чьей ссылки ещё нет среди виденных (ctx.state["seen"]).
# Всё, что видно при первом запуске, кроме backfill последних, сразу
# считается виденным — история не затягивается.
#
# Запись: text — заголовок и текст статьи, id — каноническая ссылка; в data —
# url, title, published, date_precision (exact | day | none), site.
# Сайты, которые рисуются только JavaScript, не читаются.

import asyncio
import hashlib
import html as htmllib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from datetime import time as daytime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import feedparser
import httpx
import trafilatura
from bs4 import BeautifulSoup

MODES = ("auto", "rss", "sitemap", "listing")
MIN_LISTING_LINKS = 3
SEEN_MIN = 3000
MAX_SITEMAPS = 10
MAX_SITEMAP_LOCS = 5000
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json", "application/xml", "text/xml")
FEED_GUESSES = ("rss", "feed", "rss.xml", "rss.php", "feed.xml", "atom.xml", "rss/")
TRACKING_PREFIXES = ("utm_", "yclid", "gclid", "fbclid", "_openstat")
# Служебные страницы раздела, которые сами материалами не бывают.
SERVICE_SEGMENT = re.compile(
    r"^(tags?|keywords?|archive|archives|rubrics?|category|categories|search|authors?|login|page\d*|page\d+\.html|rss|feed)$",
    re.I,
)
PAGINATION_TEXT = re.compile(r"^[\d\s .,…«»<>←→‹›|-]+$")
SKIP_EXT = re.compile(
    r"(\.(jpe?g|png|gif|webp|svg|pdf|docx?|xlsx?|pptx?|zip|rar|mp4|mp3|xml)$)|(/(rss|feed|atom)(\.php)?$)", re.I
)
DATE_META = (
    ("property", "article:published_time"),
    ("property", "og:published_time"),
    ("name", "pubdate"),
    ("name", "publish-date"),
    ("name", "date"),
    ("itemprop", "datePublished"),
    ("name", "DC.date.issued"),
)
URL_DATE_RE = re.compile(r"/(20\d{2})[/-](0[1-9]|1[0-2])[/-](0[1-9]|[12]\d|3[01])(?:/|$)")
RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


class DiscoveryError(Exception):
    pass


@dataclass
class Candidate:
    url: str  # нормализованная
    published_at: datetime | None = None
    title: str | None = None
    summary: str | None = None
    image: str | None = None  # вложение из RSS, если у статьи своих картинок нет


# ── ссылки ───────────────────────────────────────────────────────────────────


def normalize_url(url, base=None):
    """Абсолютная ссылка без якоря и меток трекинга. Хвостовой / не трогаем:
    многие CMS без него отвечают 404."""
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not k.lower().startswith(TRACKING_PREFIXES)])
    return urlunsplit(((parts.scheme or "https").lower(), parts.netloc.lower(), parts.path or "/", query, ""))


def same_site(a, b):
    ha = urlsplit(a).netloc.lower().removeprefix("www.")
    hb = urlsplit(b).netloc.lower().removeprefix("www.")
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


def path_prefix(url):
    """'/news/' для https://site/news/, '' для корня сайта."""
    path = urlsplit(url).path or "/"
    if path == "/":
        return ""
    return path if path.endswith("/") else path + "/"


def site_root(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc, "/", "", ""))


def url_key(url):
    return hashlib.blake2b(url.encode(), digest_size=8).hexdigest()


def make_matcher(source_url, link_pattern):
    rx = re.compile(link_pattern) if link_pattern else None
    prefix = path_prefix(source_url)
    listing = normalize_url(source_url)
    host = urlsplit(listing).netloc

    def match(url):
        parts = urlsplit(url)
        if SKIP_EXT.search(parts.path) or url == listing:
            return False
        if rx is not None:
            return same_site(url, source_url) and bool(rx.search(url))
        # без шаблона: только этот хост и без query — теги, фильтры и пагинация не материалы
        if parts.netloc != host or parts.query or parts.path.endswith(("index.php", "index.html")):
            return False
        if any(SERVICE_SEGMENT.match(seg) for seg in parts.path.split("/") if seg):
            return False
        if prefix:
            return parts.path.startswith(prefix) and len(parts.path.rstrip("/")) > len(prefix.rstrip("/"))
        # листинг в корне: материал — хотя бы два сегмента пути
        return len([s for s in parts.path.split("/") if s]) >= 2

    return match


# ── HTTP ─────────────────────────────────────────────────────────────────────


class Site:
    """Запросы к сайту с паузой между ними."""

    def __init__(self, http, delay, user_agent):
        self.http = http
        self.delay = delay
        self.headers = {"User-Agent": user_agent} if user_agent else {}
        self.headers["Accept-Language"] = "ru,en;q=0.8"
        self._last = 0.0

    async def get(self, url):
        wait = self._last + self.delay - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last = time.monotonic()
        return await self.http.get(url, headers=self.headers)

    async def text(self, url):
        response = await self.get(url)
        return response.status_code, response.text, str(response.url)


def http_error(exc):
    # у таймаутов httpx пустое сообщение
    if isinstance(exc, httpx.TimeoutException):
        return f"сайт не ответил вовремя ({type(exc).__name__})"
    return str(exc).strip() or type(exc).__name__


# ── robots.txt ───────────────────────────────────────────────────────────────


async def load_robots(site, url):
    robots = RobotFileParser()
    try:
        status, text, _ = await site.text(urljoin(site_root(url), "/robots.txt"))
    except httpx.HTTPError:
        status, text = 0, ""
    lines = text.splitlines() if status == 200 else []
    robots.parse(lines)
    sitemaps = [line.split(":", 1)[1].strip() for line in lines if line.lower().startswith("sitemap:")]
    return robots, sitemaps


# ── RSS ──────────────────────────────────────────────────────────────────────


def clean_title(raw):
    """Ленты часто экранируют заголовки дважды — &nbsp; и &mdash; переживают разбор."""
    if not raw:
        return None
    text = BeautifulSoup(htmllib.unescape(raw), "lxml").get_text(" ", strip=True)
    return " ".join(text.replace(" ", " ").split()) or None


def entry_date(entry):
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
    return None


def entry_image(entry, base):
    """Картинка записи ленты: enclosure или media:content / media:thumbnail."""
    for enclosure in entry.get("enclosures") or []:
        if str(enclosure.get("type", "")).startswith("image/") and enclosure.get("href"):
            return urljoin(base, enclosure["href"])
    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key) or []:
            kind = str(media.get("type") or media.get("medium") or "image")
            if media.get("url") and "image" in kind:
                return urljoin(base, media["url"])
    return None


def parse_feed(text, base):
    out = []
    for entry in feedparser.parse(text).entries:
        link = entry.get("link") or ""
        if link:
            out.append(Candidate(url=normalize_url(link, base), published_at=entry_date(entry),
                                 title=clean_title(entry.get("title")), summary=entry.get("summary") or None,
                                 image=entry_image(entry, base)))
    return out


async def first_feed(site, urls, source_url, stop_on_timeout):
    for url in dict.fromkeys(urls):
        try:
            status, text, final = await site.text(url)
        except httpx.TimeoutException:
            if stop_on_timeout:
                break  # медленный сайт потратил бы минуты на каждый угаданный адрес
            continue
        except httpx.HTTPError:
            continue
        if status == 200 and any(same_site(c.url, source_url) for c in parse_feed(text, final)):
            return final
    return None


async def find_feed(site, source_url, page):
    advertised = []
    if page:
        soup = BeautifulSoup(page, "lxml")
        for link in soup.find_all("link", href=True):
            rel = " ".join(link.get("rel") or []).lower()
            if "alternate" in rel and (link.get("type") or "").lower() in FEED_TYPES:
                advertised.append(urljoin(source_url, link["href"]))
        # многие CMS (Битрикс и др.) показывают только значок <a href=".../rss.php">
        for a in soup.find_all("a", href=True):
            if re.search(r"(^|[/._-])(rss|atom|feed)([/._-]|$)", a["href"], re.I):
                advertised.append(urljoin(source_url, a["href"]))
    feed = await first_feed(site, advertised, source_url, stop_on_timeout=False)
    if feed:
        return feed
    base = source_url if source_url.endswith("/") else source_url + "/"
    guesses = [urljoin(base, g) for g in FEED_GUESSES] + [urljoin(site_root(source_url), "/" + g) for g in FEED_GUESSES]
    return await first_feed(site, guesses, source_url, stop_on_timeout=True)


async def rss_candidates(site, feed_url):
    status, text, final = await site.text(feed_url)
    if status != 200:
        raise DiscoveryError(f"RSS {feed_url}: HTTP {status}")
    if not feedparser.parse(text).version:
        # например, «мягкий 404» CMS с HTTP 200: ленты больше нет, а не пусто
        raise DiscoveryError(f"{feed_url} больше не отдаёт RSS/Atom")
    return parse_feed(text, final)


# ── sitemap ──────────────────────────────────────────────────────────────────


def parse_sitemap(text):
    """(дочерние sitemap, ссылки)."""
    soup = BeautifulSoup(text, "xml")
    children = [loc.get_text(strip=True) for sm in soup.find_all("sitemap") if (loc := sm.find("loc"))]
    urls = []
    for u in soup.find_all("url"):
        loc = u.find("loc")
        if not loc:
            continue
        lastmod = None
        mark = u.find("lastmod") or u.find("publication_date")
        if mark:
            try:
                lastmod = datetime.fromisoformat(mark.get_text(strip=True).replace("Z", "+00:00"))
                if lastmod.tzinfo is None:
                    lastmod = lastmod.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        urls.append(Candidate(url=normalize_url(loc.get_text(strip=True)), published_at=lastmod))
    return children, urls


async def sitemap_candidates(site, roots, match):
    queue, visited, out = list(roots), set(), []
    while queue and len(visited) < MAX_SITEMAPS and len(out) < MAX_SITEMAP_LOCS:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            status, text, _ = await site.text(url)
        except httpx.HTTPError:
            continue
        if status != 200:
            continue
        children, urls = parse_sitemap(text)
        # сначала дочерние sitemap, похожие на новостные
        children.sort(key=lambda c: (0 if re.search(r"news|post|article|press|event", c, re.I) else 1, c))
        queue.extend(children)
        out.extend(c for c in urls if match(c.url))
    out.sort(key=lambda c: c.published_at.timestamp() if c.published_at else 0, reverse=True)
    return out[:MAX_SITEMAP_LOCS]


# ── листинг ──────────────────────────────────────────────────────────────────


def listing_links(page, page_url, match):
    soup = BeautifulSoup(page, "lxml")
    next_url = None
    nxt = soup.find("link", rel="next") or soup.find("a", rel="next")
    if nxt is not None and nxt.get("href"):
        next_url = urljoin(page_url, nxt["href"])
    for tag in soup.find_all(["header", "nav", "footer", "aside"]):
        tag.decompose()
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = normalize_url(href, page_url)
        title = a.get_text(" ", strip=True) or None
        if title and PAGINATION_TEXT.match(title):
            continue  # «2», «1204», «»»: пагинация, а не материал
        if url in seen or not match(url):
            continue
        seen.add(url)
        out.append(Candidate(url=url, title=title))
    return out, next_url


def link_shape(url):
    """Форма пути: сегмент из цифр — N, любой другой — S.

    Материалы раздела ссылаются одинаково (/news/12345.html, /news/some-slug),
    а у пагинации, рубрик и архивов форма своя — так что самая «весомая»
    форма на листинге и есть форма материалов.
    """
    segments = []
    for seg in urlsplit(url).path.split("/"):
        if seg:
            stem, dot, ext = seg.partition(".")
            segments.append(("N" if stem.isdigit() else "S") + (dot + ext if dot else ""))
    return "/" + "/".join(segments) + ("/" if url.endswith("/") else "")


def dominant_shape(cands):
    """Меню рубрик бывает длиннее списка материалов, поэтому форму считаем не по
    числу ссылок, а по длине их текста: у материала — заголовок, у меню — слово."""
    scores = {}
    for c in cands:
        shape = link_shape(c.url)
        scores[shape] = scores.get(shape, 0) + min(len(c.title or ""), 100)
    if not scores:
        return None
    return max(scores, key=lambda shape: (scores[shape], shape.count("/")))


def page_url(listing, page_param, page, next_url):
    if page_param:
        return f"{listing}{'&' if '?' in listing else '?'}{page_param}={page}"
    return next_url


async def listing_candidates(site, opts, match, max_pages, known=None):
    out, url = [], opts["url"]
    for page in range(1, max_pages + 1):
        if url is None:
            break
        status, text, final = await site.text(url)
        if status != 200:
            if page == 1:
                raise DiscoveryError(f"листинг {url}: HTTP {status}")
            break
        cands, next_url = listing_links(text, final, match)
        out.extend(cands)
        # листинг — от новых к старым: знакомая ссылка значит, что дальше всё знакомо
        if known is not None and any(known(c.url) for c in cands):
            break
        url = page_url(opts["url"], opts["page_param"], page + 1, next_url)
    return out


# ── статья ───────────────────────────────────────────────────────────────────


def as_day(day):
    # только дата: полдень UTC оставляет тот же календарный день в любом поясе России
    return datetime.combine(day, daytime(12, 0), tzinfo=timezone.utc)


def parse_date(value):
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", value)
        return datetime(int(m[1]), int(m[2]), int(m[3])) if m else None


def with_precision(dt, raw):
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw.strip()):
        return as_day(dt.date()), "day"
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)), "exact"


def jsonld_dates(soup):
    out = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            stack = [json.loads(script.string or "")]
        except (ValueError, TypeError):
            continue
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if isinstance(node.get("datePublished"), str):
                    out.append(node["datePublished"])
                stack.extend(v for v in node.values() if isinstance(v, (list, dict)))
    return out


def find_date(soup, url, page):
    for attr, name in DATE_META:
        tag = soup.find("meta", attrs={attr: name})
        if tag and tag.get("content") and (dt := parse_date(tag["content"])):
            return with_precision(dt, tag["content"])
    for raw in jsonld_dates(soup):
        if dt := parse_date(raw):
            return with_precision(dt, raw)
    tag = soup.find("time", attrs={"datetime": True})
    if tag and (dt := parse_date(tag["datetime"])):
        return with_precision(dt, tag["datetime"])
    if m := URL_DATE_RE.search(url):
        return as_day(datetime(int(m[1]), int(m[2]), int(m[3])).date()), "day"
    try:
        meta = trafilatura.extract_metadata(page, default_url=url)
        if meta is not None and meta.date and (dt := parse_date(meta.date)):
            return as_day(dt.date()), "day"
    except Exception:  # noqa: BLE001 — эвристика, не обязана срабатывать
        pass
    return None, "none"


def russian_date(text):
    m = re.search(r"\b(\d{1,2})\s+(" + "|".join(RU_MONTHS) + r")\s+(20\d{2})", text.lower())
    if not m:
        return None
    try:
        return as_day(datetime(int(m[3]), RU_MONTHS[m[2]], int(m[1])).date())
    except ValueError:
        return None


def extract_article(page, url):
    soup = BeautifulSoup(page, "lxml")
    link = soup.find("link", rel="canonical")
    canonical = normalize_url(link["href"], url) if link is not None and link.get("href") else normalize_url(url)

    # сначала <h1>: og:title и <title> бывают просто названием сайта
    title = None
    h1 = soup.find("h1")
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if h1 is not None and h1.get_text(strip=True):
        title = h1.get_text(" ", strip=True)
    elif og_title and og_title.get("content"):
        title = og_title["content"].strip()
    elif soup.title:
        title = soup.title.get_text(" ", strip=True)

    text = trafilatura.extract(page, url=url, include_comments=False, include_tables=True,
                               favor_recall=True, deduplicate=True) or ""
    if not text:
        main = soup.find("article") or soup.find("main") or soup.body
        text = main.get_text("\n", strip=True) if main else ""

    published, precision = find_date(soup, url, page)
    if published is None:
        published = russian_date(text[:500])
        precision = "day" if published else "none"

    site_name = soup.find("meta", attrs={"property": "og:site_name"})
    return {
        "url": canonical,
        "title": title,
        "text": text.strip(),
        "published_at": published,
        "date_precision": precision,
        "images": article_images(soup, url, 20),
        "site": site_name["content"].strip() if site_name and site_name.get("content") else None,
    }


IMAGE_NOISE = re.compile(
    r"(logo|icon|avatar|sprite|favicon|banner|counter|pixel|placeholder|blank|spacer|emoji|button|badge|share)", re.I
)
SMALL_IMAGE = 150  # px: меньше — значок, а не иллюстрация


def image_src(tag, base):
    """Адрес картинки с учётом ленивой загрузки и srcset (берём самый крупный)."""
    for attr in ("data-src", "data-original", "data-lazy-src", "data-full", "src"):
        value = (tag.get(attr) or "").strip()
        if value and not value.startswith("data:"):
            return urljoin(base, value)
    srcset = (tag.get("srcset") or tag.get("data-srcset") or "").strip()
    if srcset:
        last = srcset.split(",")[-1].strip().split(" ")[0]
        if last and not last.startswith("data:"):
            return urljoin(base, last)
    return None


def too_small(tag):
    for attr in ("width", "height"):
        try:
            if int(str(tag.get(attr, "")).rstrip("px")) < SMALL_IMAGE:
                return True
        except ValueError:
            pass
    return False


def article_images(soup, url, limit):
    """Обложка и иллюстрации статьи — без логотипов, иконок и счётчиков."""
    found = []

    def add(src):
        if not src or src in found:
            return
        path = urlsplit(src).path.lower()
        if path.endswith((".svg", ".gif", ".ico")) or IMAGE_NOISE.search(path):
            return
        found.append(src)

    for attrs in ({"property": "og:image"}, {"name": "twitter:image"}, {"property": "twitter:image"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            add(urljoin(url, tag["content"].strip()))
    body = soup.find(attrs={"itemprop": "articleBody"}) or soup.find("article") or soup.find("main")
    if body is not None:
        for img in body.find_all("img"):
            if not too_small(img):
                add(image_src(img, url))
    return found[:limit]


def strip_html(text):
    return BeautifulSoup(text, "lxml").get_text("\n", strip=True) if text else ""


# ── стратегия ────────────────────────────────────────────────────────────────


def options(config):
    url = str(config.get("url") or "").strip()
    if not url:
        raise ValueError("url: укажите страницу раздела новостей, например https://news.tsu.ru/news/")
    if not url.startswith(("http://", "https://")):
        raise ValueError("url — страница раздела, начинается с http:// или https://")
    mode = config.get("mode") or "auto"
    if mode not in MODES:
        raise ValueError(f"mode={mode!r}; допустимо: {', '.join(MODES)}")
    pattern = config.get("link_pattern") or None
    if pattern:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"link_pattern: некорректное регулярное выражение ({exc})") from exc
    return {
        "url": url,
        "mode": mode,
        "feed_url": config.get("feed_url") or None,
        "link_pattern": pattern,
        "page_param": config.get("page_param") or None,
        "max_pages": int(config.get("max_pages", 3)),
        "max_new_per_run": int(config.get("max_new_per_run", 30)),
        "backfill": int(config.get("backfill", 10)),
        "images": bool(config.get("images", True)),
        "max_images": int(config.get("max_images", 5)),
        "respect_robots": bool(config.get("respect_robots", True)),
        "delay": float(config.get("delay", 1.0)),
        "user_agent": config.get("user_agent") or None,
    }


def fingerprint(opts):
    """От чего зависит выбранная стратегия: поменялось — выбираем заново."""
    return [opts["url"], opts["mode"], opts["feed_url"], opts["link_pattern"]]


async def candidates(site, strategy, opts, known=None):
    mode = strategy["mode"]
    pattern = opts["link_pattern"]
    match = make_matcher(opts["url"], pattern)
    if mode == "rss":
        cands = await rss_candidates(site, strategy["feed_url"])
        return [c for c in cands if same_site(c.url, opts["url"]) and (not pattern or re.search(pattern, c.url))]
    if mode == "sitemap":
        cands = await sitemap_candidates(site, strategy["sitemaps"], match)
        if not cands:
            raise DiscoveryError("sitemap не вернул ни одной ссылки")
        return cands
    cands = await listing_candidates(site, opts, match, opts["max_pages"] if known else 1, known)
    if not pattern:
        shape = strategy.setdefault("shape", dominant_shape(cands))
        cands = [c for c in cands if link_shape(c.url) == shape]
    if not cands:
        # раньше ссылки на материалы были: ноль значит, что страница сломалась или переехала
        raise DiscoveryError("на странице-листинге нет ни одной ссылки на материалы")
    return cands


async def discover(site, opts, robots_sitemaps, ctx):
    """Первая стратегия, которая находит ссылки на материалы, и сами ссылки."""
    page = None
    if opts["mode"] != "rss" or not opts["feed_url"]:
        status, page, _ = await site.text(opts["url"])
        if status != 200:
            raise RuntimeError(f"страница раздела отвечает HTTP {status}")

    tried = []
    order = ("rss", "sitemap", "listing") if opts["mode"] == "auto" else (opts["mode"],)
    for mode in order:
        try:
            if mode == "rss":
                feed = opts["feed_url"] or await find_feed(site, opts["url"], page)
                if not feed:
                    tried.append("RSS не найден")
                    continue
                strategy = {"mode": "rss", "feed_url": feed}
            elif mode == "sitemap":
                strategy = {"mode": "sitemap",
                            "sitemaps": robots_sitemaps or [urljoin(site_root(opts["url"]), "/sitemap.xml")]}
            else:
                strategy = {"mode": "listing"}
            cands = await candidates(site, strategy, opts)
        except (DiscoveryError, httpx.HTTPError) as exc:
            tried.append(f"{mode}: {http_error(exc)}")
            continue
        minimum = 1 if mode != "listing" or opts["link_pattern"] else MIN_LISTING_LINKS
        if len(cands) < minimum:
            tried.append(f"{mode}: ссылок на материалы — {len(cands)}")
            continue
        ctx.log(f"стратегия {mode}, сейчас видно материалов: {len(cands)}")
        prefix = path_prefix(opts["url"])
        if mode == "rss" and prefix and not opts["link_pattern"] and \
                sum(urlsplit(c.url).path.startswith(prefix) for c in cands) < len(cands) / 2:
            ctx.log(f"внимание: это лента всего сайта, а не раздела {prefix} — придут все новости сайта; "
                    "чтобы сузить, укажите feed_url ленты раздела или link_pattern")
        return strategy, cands
    raise RuntimeError(
        "не удалось понять, как искать новые материалы (нет RSS, в sitemap нет ссылок под "
        f"{path_prefix(opts['url']) or '/'}, на странице нет ссылок на материалы — возможно, сайт рисуется "
        "JavaScript). Укажите feed_url или link_pattern. Попытки: " + "; ".join(tried)
    )


# ── запуск ───────────────────────────────────────────────────────────────────


async def collect(ctx):
    opts = options(ctx.config)
    site = Site(ctx.http, opts["delay"], opts["user_agent"])
    seen = dict.fromkeys(ctx.state.get("seen", []))

    def known(url):
        return url_key(url) in seen

    def mark(url):
        key = url_key(url)
        seen.pop(key, None)
        seen[key] = None  # в конец: виденное недавно вытесняется последним

    try:
        robots, robots_sitemaps = await load_robots(site, opts["url"])
        agent = opts["user_agent"] or ctx.http.headers.get("User-Agent", "*")
        if opts["respect_robots"] and not robots.can_fetch(agent, opts["url"]):
            raise RuntimeError("robots.txt запрещает обход этого раздела (respect_robots: false — обойти)")
        strategy = ctx.state.get("strategy")
        first = strategy is None or ctx.state.get("fingerprint") != fingerprint(opts)
        if first:
            strategy, cands = await discover(site, opts, robots_sitemaps, ctx)
        else:
            try:
                cands = await candidates(site, strategy, opts, known)
            except DiscoveryError as exc:
                raise RuntimeError(
                    f"поиск новых материалов ({strategy['mode']}): {exc}. "
                    "Если сайт переделали — сбросьте курсор (POST /connectors/{name}/state/reset), и стратегия выберется заново"
                ) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"сайт недоступен: {http_error(exc)}") from exc

    unique = {}
    for c in cands:
        unique.setdefault(c.url, c)
    unique = list(unique.values())
    fresh = [c for c in unique if not known(c.url)]
    for c in unique:
        if known(c.url):
            mark(c.url)  # всё ещё на виду — не вытеснять
    if first:
        limit = opts["backfill"]
        for c in fresh[limit:]:
            mark(c.url)  # история: видели, но не берём
    else:
        limit = opts["max_new_per_run"]
    fresh = fresh[:max(limit, 0)]

    from_feed = strategy["mode"] == "rss"
    items, failures = [], 0
    for c in fresh:
        try:
            response = await site.get(c.url)
        except httpx.HTTPError as exc:
            failures += 1
            ctx.log(f"{c.url}: {http_error(exc)} — попробую в следующий раз")
            continue
        if response.status_code in (404, 410):
            mark(c.url)
            continue
        if response.status_code != 200 or "html" not in response.headers.get("content-type", "html"):
            failures += 1
            ctx.log(f"{c.url}: HTTP {response.status_code} — попробую в следующий раз")
            continue
        art = extract_article(response.text, str(response.url))
        mark(c.url)
        mark(art["url"])
        body = art["text"] or strip_html(c.summary)
        # заголовок из ленты написан для этой самой статьи; og:title бывает названием сайта
        title = (c.title if from_feed else None) or art["title"] or c.title
        if not body and not title:
            continue
        published, precision = art["published_at"], art["date_precision"]
        if c.published_at is not None and (published is None or (precision == "day" and from_feed)):
            # pubDate ленты точен, lastmod из sitemap — только подсказка дня
            published, precision = c.published_at, "exact" if from_feed else "day"
        text = body if not title or body.startswith(title) else f"{title}\n\n{body}"
        item = {
            "id": art["url"],
            "text": text,
            "url": art["url"],
            "title": title,
            "published": published.isoformat() if published else None,
            "date_precision": precision,
            "site": art["site"],
        }
        if opts["images"]:
            pictures = art["images"] or ([c.image] if c.image else [])
            if pictures:
                item["image"] = pictures[: opts["max_images"]]
        items.append(item)
    if fresh and failures == len(fresh):
        raise RuntimeError(f"не открылась ни одна из {len(fresh)} новых страниц")

    cap = max(SEEN_MIN, 2 * len(unique))
    ctx.state.update(strategy=strategy, fingerprint=fingerprint(opts), seen=list(seen)[-cap:])
    ctx.log(f"{strategy['mode']}: ссылок {len(unique)}, новых {len(fresh)}, забрано {len(items)}")
    return items
