# Публичный Telegram-канал через веб-превью https://t.me/s/<канал> — без
# аккаунта и токенов.
#
# Настройки (`config` источника в sources.py, приходят в ctx.config):
#   channel   — имя канала, как в ссылке https://t.me/<имя>: например tsunews;
#   backfill  — сколько последних постов взять при первом запуске
#               (0 — только то, что появится после него);
#   max_pages — сколько страниц превью (~20 постов) листать назад за запуск;
#   proxy     — http(s)-прокси, если t.me напрямую недоступен.
#
# Курсор — id последнего поста в ctx.state["last_id"]: каждый запуск берёт
# только посты новее него. Читаются только каналы с включённым превью;
# у групп, закрытых каналов и ссылок-приглашений его нет — t.me уводит с
# /s/ редиректом, и запуск падает с понятной причиной.
#
# Запись: text — текст поста, image — фото и обложки видео, id — <канал>/<номер>;
# в data — url, published, source (название канала), forwarded_from, links.

import asyncio
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
BACKGROUND_RE = re.compile(r"background-image:\s*url\(['\"]?([^'\")]+)")
PAGE_DELAY = 1.0  # пауза между страницами: t.me не любит частых запросов


def channel_name(raw):
    name = str(raw or "").strip().removeprefix("@")
    if not name:
        raise ValueError("channel: укажите имя канала — то, что после https://t.me/, например tsunews")
    if "/" in name or "." in name:
        raise ValueError(f"channel: нужно только имя канала, без https://t.me/ — а не {name!r}")
    if name.startswith("+") or name == "joinchat":
        raise ValueError("channel: ссылки-приглашения не читаются — у закрытых каналов и групп нет превью")
    if not NAME_RE.match(name):
        raise ValueError(f"channel: некорректное имя канала {name!r}")
    return name


def message_text(element):
    """Текст поста с переносами строк, но без разрывов вокруг ссылок и выделений."""
    if element is None:
        return ""
    for br in element.find_all("br"):
        br.replace_with("\n")
    return element.get_text().strip()


def background_url(element):
    match = BACKGROUND_RE.search(element.get("style") or "")
    return match.group(1) if match else None


def parse_page(html):
    """(посты, номера всех сообщений на странице, похоже ли это на превью канала)."""
    soup = BeautifulSoup(html, "lxml")
    is_preview = bool(soup.select_one(".tgme_channel_info, .tgme_widget_message"))
    title = soup.select_one(".tgme_channel_info_header_title")
    source = title.get_text(" ", strip=True) if title else None

    posts, ids = [], []
    for message in soup.select("div.tgme_widget_message[data-post]"):
        post = message["data-post"]
        try:
            number = int(post.rsplit("/", 1)[1])
        except (IndexError, ValueError):
            continue
        if "service_message" in (message.get("class") or []):
            continue
        ids.append(number)

        text_element = message.select_one(".tgme_widget_message_text")
        links = []
        if text_element is not None:
            for a in text_element.find_all("a", href=True):
                href = a["href"]
                # хештеги и упоминания ведут на поиск по каналу — это не ссылки
                if href.startswith("http") and "?q=" not in href and href not in links:
                    links.append(href)
        text = message_text(text_element)
        images = []
        for media in message.select(".tgme_widget_message_photo_wrap, .tgme_widget_message_video_thumb"):
            url = background_url(media)
            if url and url not in images:
                images.append(url)
        if not text and not images:
            continue  # опросы, стикеры, голосовые: собирать нечего

        published = None
        time = message.select_one(".tgme_widget_message_date time[datetime]")
        if time is not None:
            try:
                published = datetime.fromisoformat(time["datetime"]).isoformat()
            except ValueError:
                pass
        forwarded = message.select_one("a.tgme_widget_message_forwarded_from_name[href]")
        posts.append({
            "number": number,
            "id": post,
            "text": text,
            "image": images,
            "url": f"https://t.me/{post}",
            "published": published,
            "source": source,
            "forwarded_from": forwarded["href"].split("?")[0] if forwarded is not None else None,
            "links": links,
        })
    return posts, ids, is_preview


async def load_images(http, post, ctx):
    """Через прокси картинки качаем сами: CDN Telegram закрыт так же, как t.me."""
    loaded = []
    for url in post["image"]:
        try:
            response = await http.get(url)
            response.raise_for_status()
            loaded.append(response.content)
        except httpx.HTTPError as exc:
            ctx.log(f"картинка {post['id']}: {exc}")
    post["image"] = loaded


async def collect(ctx):
    channel = channel_name(ctx.config.get("channel"))
    backfill = int(ctx.config.get("backfill", 10))
    max_pages = int(ctx.config.get("max_pages", 10))
    proxy = ctx.config.get("proxy") or None

    if ctx.state.get("channel") != channel:
        ctx.state.clear()  # другой канал — свой курсор
    last = ctx.state.get("last_id")
    first = last is None

    http = ctx.http
    if proxy:
        http = httpx.AsyncClient(proxy=proxy, timeout=30, headers=dict(ctx.http.headers))
    try:
        collected = {}
        newest = last or 0
        before = None
        for page in range(max_pages):
            if page:
                await asyncio.sleep(PAGE_DELAY)
            try:
                response = await http.get(
                    f"https://t.me/s/{channel}",
                    params={"before": before} if before else None,
                    follow_redirects=False,
                )
            except httpx.TransportError as exc:
                hint = "" if proxy else " — если t.me закрыт из сети воркера, задайте proxy"
                raise RuntimeError(f"t.me недоступен: {type(exc).__name__} {exc}".rstrip() + hint) from exc
            if response.is_redirect:
                raise RuntimeError(
                    f"у @{channel} нет публичного превью t.me/s/: это группа, закрытый канал "
                    "или канал с отключённым превью — такие не читаются"
                )
            response.raise_for_status()
            posts, ids, is_preview = parse_page(response.text)
            if not is_preview:
                raise RuntimeError(f"t.me/s/{channel} не похожа на превью канала")
            if not ids:
                break
            newest = max(newest, *ids)
            for post in posts:
                if first or post["number"] > last:
                    collected[post["number"]] = post
            if first:
                if len(collected) >= backfill:
                    break
            elif min(ids) <= last + 1:
                break
            before = min(ids)

        posts = [collected[n] for n in sorted(collected)]
        if first:
            posts = posts[-backfill:] if backfill > 0 else []
        if proxy:
            for post in posts:
                await load_images(http, post, ctx)
    finally:
        if http is not ctx.http:
            await http.aclose()

    ctx.state.update(channel=channel, last_id=newest)
    ctx.log(f"@{channel}: новых постов {len(posts)}, последний №{newest}")
    for post in posts:
        del post["number"]
    return posts
