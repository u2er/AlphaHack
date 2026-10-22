"""Сборщики коннекторов (connectors/kinds) ведут себя как обещано — на
подставном HTTP, без сети."""

from __future__ import annotations

import httpx
import pytest

from eventgraph.connectors.kinds import telegram_channel, website
from eventgraph.connectors.runtime import Context

KINDS = {"telegram_channel": telegram_channel, "website": website}


class Web:
    """Подставной интернет: адрес → (статус, тело, заголовки)."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.requested: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        route = self.routes.get(url)
        if route is None:
            return httpx.Response(404, text="нет такой страницы")
        if isinstance(route, tuple):
            status, body, headers = route
            return httpx.Response(status, text=body, headers=headers)
        return httpx.Response(200, text=str(route), headers={"content-type": "text/html; charset=utf-8"})


async def run(kind: str, web: Web, config: dict, state: dict | None = None) -> tuple[list[dict], Context]:
    collect = KINDS[kind].collect
    ctx = Context(config=config, state=state if state is not None else {})
    await ctx.http.aclose()
    ctx.http = httpx.AsyncClient(transport=httpx.MockTransport(web.handler), follow_redirects=True)
    try:
        return await collect(ctx), ctx
    finally:
        await ctx.aclose()


# ── без настройки ────────────────────────────────────────────────────────────


async def test_website_without_url_says_what_to_fill():
    with pytest.raises(ValueError, match="укажите страницу раздела"):
        await run("website", Web({}), {"url": ""})


# ── Telegram ─────────────────────────────────────────────────────────────────


def tg_message(number: int, text: str = "", photo: str | None = None, service: bool = False) -> str:
    media = (
        f'<a class="tgme_widget_message_photo_wrap" href="https://t.me/chan/{number}" '
        f"style=\"width:800px;background-image:url('{photo}')\"></a>"
        if photo
        else ""
    )
    body = f'<div class="tgme_widget_message_text">{text}</div>' if text else ""
    kind = " service_message" if service else ""
    return (
        f'<div class="tgme_widget_message{kind}" data-post="Chan/{number}">{media}{body}'
        f'<a class="tgme_widget_message_date"><time datetime="2026-10-0{number % 9 + 1}T10:00:00+00:00"></time></a>'
        "</div>"
    )


def tg_page(*messages: str) -> str:
    return (
        '<div class="tgme_channel_info"><div class="tgme_channel_info_header_title"><span>Канал</span></div></div>'
        + "".join(messages)
    )


async def test_telegram_first_run_takes_backfill_then_only_newer():
    web = Web(
        {
            "https://t.me/s/chan": tg_page(
                tg_message(5, "пятый"),
                tg_message(6, "", service=True),
                tg_message(7, 'седьмой <a href="https://ex.com/a">ссылка</a><br/>вторая строка'),
                tg_message(8, "", photo="https://cdn.example/8.jpg"),
            )
        }
    )

    posts, ctx = await run("telegram_channel", web, {"channel": "@chan", "backfill": 2})

    assert [p["id"] for p in posts] == ["Chan/7", "Chan/8"]
    assert posts[0]["text"] == "седьмой ссылка\nвторая строка"
    assert posts[0]["links"] == ["https://ex.com/a"]
    assert posts[0]["source"] == "Канал"
    assert posts[1]["image"] == ["https://cdn.example/8.jpg"]
    assert ctx.state == {"channel": "chan", "last_id": 8}

    web.routes["https://t.me/s/chan"] = tg_page(tg_message(7, "седьмой"), tg_message(8, "", photo="x"), tg_message(9, "девятый"))
    posts, ctx = await run("telegram_channel", web, {"channel": "chan"}, ctx.state)

    assert [p["id"] for p in posts] == ["Chan/9"]
    assert ctx.state["last_id"] == 9


async def test_telegram_pages_back_to_the_cursor(monkeypatch):
    web = Web(
        {
            "https://t.me/s/chan": tg_page(tg_message(30, "тридцатый"), tg_message(31, "тридцать первый")),
            "https://t.me/s/chan?before=30": tg_page(tg_message(28, "старый"), tg_message(29, "двадцать девятый")),
        }
    )
    collect = telegram_channel.collect
    monkeypatch.setattr(telegram_channel, "PAGE_DELAY", 0)
    ctx = Context(config={"channel": "chan"}, state={"channel": "chan", "last_id": 28})
    await ctx.http.aclose()
    ctx.http = httpx.AsyncClient(transport=httpx.MockTransport(web.handler))

    posts = await collect(ctx)
    await ctx.aclose()

    assert [p["id"] for p in posts] == ["Chan/29", "Chan/30", "Chan/31"]


async def test_telegram_without_preview_is_refused():
    web = Web({"https://t.me/s/chan": (302, "", {"location": "https://t.me/chan"})})

    with pytest.raises(RuntimeError, match="нет публичного превью"):
        await run("telegram_channel", web, {"channel": "chan"})


async def test_telegram_unreachable_suggests_proxy():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("", request=request)

    collect = telegram_channel.collect
    ctx = Context(config={"channel": "chan"}, state={})
    await ctx.http.aclose()
    ctx.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="задайте proxy"):
        await collect(ctx)
    await ctx.aclose()


@pytest.mark.parametrize("channel", ["", "https://t.me/chan", "t.me/chan", "+AbCdEf", "ab"])
async def test_telegram_rejects_unreadable_channels(channel):
    with pytest.raises(ValueError, match="channel"):
        await run("telegram_channel", Web({}), {"channel": channel})


# ── Сайт ─────────────────────────────────────────────────────────────────────

SITE = "https://site.test"


def rss(*slugs: str) -> tuple[int, str, dict]:
    items = "".join(
        f"<item><title>Заголовок {s}</title><link>{SITE}/news/{s}/?utm_source=rss</link>"
        f"<pubDate>Tue, 06 Oct 2026 0{n}:00:00 +0000</pubDate></item>"
        for n, s in enumerate(slugs)
    )
    body = f'<?xml version="1.0"?><rss version="2.0"><channel><title>Новости</title>{items}</channel></rss>'
    return 200, body, {"content-type": "application/rss+xml"}


def article(slug: str) -> str:
    paragraphs = "".join(
        f"<p>Абзац {n} материала {slug}: достаточно длинный текст, чтобы извлекатель счёл его телом статьи.</p>"
        for n in range(6)
    )
    return (
        f'<html><head><link rel="canonical" href="{SITE}/news/{slug}/"/>'
        '<meta property="og:site_name" content="Сайт"/></head>'
        f"<body><article><h1>Заголовок {slug}</h1>{paragraphs}</article></body></html>"
    )


def site_with_feed(*slugs: str) -> Web:
    routes: dict[str, object] = {
        f"{SITE}/robots.txt": (200, "User-agent: *\nDisallow: /admin/\n", {"content-type": "text/plain"}),
        f"{SITE}/news/": f'<html><head><link rel="alternate" type="application/rss+xml" href="/news/rss.xml"/></head>'
        f"<body>листинг</body></html>",
        f"{SITE}/news/rss.xml": rss(*slugs),
    }
    for slug in slugs:
        routes[f"{SITE}/news/{slug}/"] = article(slug)
    return Web(routes)


async def test_website_rss_backfill_then_only_new():
    web = site_with_feed("c", "b", "a")
    config = {"url": f"{SITE}/news/", "backfill": 1, "delay": 0}

    items, ctx = await run("website", web, config)

    assert [i["id"] for i in items] == [f"{SITE}/news/c/"]
    # заголовок в тексте ровно один раз — даже если извлекатель уже прихватил <h1>
    assert items[0]["text"].startswith("Заголовок c\n")
    assert items[0]["text"].count("Заголовок c") == 1
    assert "Абзац 5 материала c" in items[0]["text"]
    assert items[0]["date_precision"] == "exact"
    assert items[0]["site"] == "Сайт"
    assert ctx.state["strategy"] == {"mode": "rss", "feed_url": f"{SITE}/news/rss.xml"}
    # b и a — история: виденные, но не взятые
    assert len(ctx.state["seen"]) == 3

    web.routes[f"{SITE}/news/rss.xml"] = rss("d", "c", "b", "a")
    web.routes[f"{SITE}/news/d/"] = article("d")
    web.requested.clear()
    items, ctx = await run("website", web, config, ctx.state)

    assert [i["id"] for i in items] == [f"{SITE}/news/d/"]
    # стратегия запомнена: страницу раздела и robots заново не ищем ленту
    assert f"{SITE}/news/" not in web.requested
    assert not any(u.endswith(("/b/", "/a/")) for u in web.requested)


async def test_website_failed_page_is_retried_next_time():
    web = site_with_feed("b", "a")
    config = {"url": f"{SITE}/news/", "backfill": 0, "delay": 0}
    _, ctx = await run("website", web, config)

    web.routes[f"{SITE}/news/rss.xml"] = rss("c", "b", "a")
    web.routes[f"{SITE}/news/c/"] = (503, "busy", {"content-type": "text/html"})
    with pytest.raises(RuntimeError, match="не открылась ни одна"):
        await run("website", web, config, dict(ctx.state))

    web.routes[f"{SITE}/news/c/"] = article("c")
    items, _ = await run("website", web, config, ctx.state)
    assert [i["id"] for i in items] == [f"{SITE}/news/c/"]


async def test_website_listing_keeps_article_links_only():
    links = "".join(f'<a href="/news/{n}.html">Длинный заголовок материала номер {n}</a>' for n in (3, 2, 1))
    rubrics = '<a href="/news/science/">Наука</a><a href="/news/?page=2">2</a>'
    routes: dict[str, object] = {
        f"{SITE}/news/": f"<html><body><nav><a href='/news/menu/x.html'>меню</a></nav>{rubrics}{links}</body></html>",
    }
    for n in (3, 2, 1):
        routes[f"{SITE}/news/{n}.html"] = article(str(n)).replace(f"{SITE}/news/{n}/", f"{SITE}/news/{n}.html")
    web = Web(routes)

    items, ctx = await run("website", web, {"url": f"{SITE}/news/", "mode": "listing", "backfill": 5, "delay": 0})

    assert [i["id"] for i in items] == [f"{SITE}/news/{n}.html" for n in (3, 2, 1)]
    assert ctx.state["strategy"] == {"mode": "listing", "shape": "/S/N.html"}


async def test_website_explains_when_nothing_is_found():
    web = Web({f"{SITE}/news/": "<html><body><div id='app'></div></body></html>"})

    with pytest.raises(RuntimeError, match="не удалось понять, как искать"):
        await run("website", web, {"url": f"{SITE}/news/", "delay": 0})


async def test_website_respects_robots():
    web = Web({f"{SITE}/robots.txt": (200, "User-agent: *\nDisallow: /news/\n", {"content-type": "text/plain"})})

    with pytest.raises(RuntimeError, match="robots.txt"):
        await run("website", web, {"url": f"{SITE}/news/", "delay": 0})


def test_article_images_keep_illustrations_and_drop_noise():
    from bs4 import BeautifulSoup

    g = vars(website)
    page = f"""<html><head><meta property="og:image" content="/upload/cover.jpg"></head><body>
      <header><img src="/static/logo.png"></header>
      <article>
        <img src="/upload/cover.jpg">
        <img data-src="/upload/photo-2.jpg" src="data:image/gif;base64,R0lG">
        <img src="/upload/tiny.jpg" width="40" height="40">
        <img src="/static/icons/share.png">
        <img src="/upload/chart.svg">
        <img srcset="/upload/p-480.jpg 480w, /upload/p-1600.jpg 1600w">
      </article></body></html>"""
    images = g["article_images"](BeautifulSoup(page, "lxml"), f"{SITE}/news/a/", 10)
    assert images == [f"{SITE}/upload/cover.jpg", f"{SITE}/upload/photo-2.jpg", f"{SITE}/upload/p-1600.jpg"]
    assert len(g["article_images"](BeautifulSoup(page, "lxml"), f"{SITE}/news/a/", 2)) == 2


async def test_website_items_carry_images_by_default():
    web = site_with_feed("c")
    web.routes[f"{SITE}/news/c/"] = article("c").replace(
        "</head>", f'<meta property="og:image" content="{SITE}/upload/c.jpg"></head>'
    )
    items, _ = await run("website", web, {"url": f"{SITE}/news/", "backfill": 1, "delay": 0})
    assert items[0]["image"] == [f"{SITE}/upload/c.jpg"]

    items, _ = await run("website", web, {"url": f"{SITE}/news/", "backfill": 1, "delay": 0, "images": False})
    assert "image" not in items[0]


def test_query_only_root_rules_do_not_block_the_whole_site():
    from urllib.robotparser import RobotFileParser

    from eventgraph.connectors.kinds.website import robots_lines

    robots = RobotFileParser()
    robots.parse(robots_lines(["User-agent: *", "Disallow: /search", "Disallow: /?", "Disallow: /?utm="]))
    assert robots.can_fetch("EventGraph", "https://lenta.ru/rubrics/economics/")
    assert not robots.can_fetch("EventGraph", "https://lenta.ru/search")
