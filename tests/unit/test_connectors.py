"""Коннекторы: список источников, контекст и разбор результата — без базы."""

from __future__ import annotations

import base64
import inspect
from datetime import datetime

import httpx
import pytest

from eventgraph.connectors.images import sniff
from eventgraph.connectors.normalize import ResultError, finalize_ids, parse
from eventgraph.connectors.runtime import Context, validate_name
from eventgraph.sources import BY_NAME, SOURCES, Source, check

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
LIMIT = 1024 * 1024


def http_with(routes: dict[str, bytes]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(str(request.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── источники и контекст ─────────────────────────────────────────────────


def test_names():
    assert validate_name("tg_news") == ""
    assert validate_name("Tg") != ""
    assert validate_name("1abc") != ""


def test_sources_are_checked():
    assert set(BY_NAME) == {s.name for s in SOURCES}
    assert all(inspect.iscoroutinefunction(s.collect) for s in SOURCES)
    assert {s.kind for s in SOURCES} <= {"telegram_channel", "website"}

    async def collect(ctx):
        return []

    with pytest.raises(ValueError, match="дважды"):
        check((Source("a_b", collect), Source("a_b", collect)))
    with pytest.raises(ValueError, match="латиница"):
        check((Source("Плохое", collect),))
    with pytest.raises(ValueError, match="60 секунд"):
        check((Source("fast", collect, interval_seconds=5),))
    assert check((Source("manual_only", collect, interval_seconds=None),))["manual_only"].kind == "test_connectors"


async def test_collect_gets_context():
    async def collect(ctx):
        ctx.state["n"] = ctx.state.get("n", 0) + 1
        ctx.log("шаг", ctx.state["n"])
        return {"text": ctx.config["greeting"]}

    ctx = Context(config={"greeting": "привет"}, state={"n": 4})
    result = await collect(ctx)
    await ctx.aclose()
    assert result == {"text": "привет"}
    assert ctx.state == {"n": 5}
    assert "шаг 5" in ctx.text_log


# ── результат ────────────────────────────────────────────────────────────────


def test_single_dict_and_none():
    assert len(parse({"text": "a"})) == 1
    assert parse(None) == []


@pytest.mark.parametrize("bad", ["строка", 42, [{"text": "a"}, "b"]])
def test_wrong_shape_is_an_error(bad):
    with pytest.raises(ResultError):
        parse(bad)


def test_reserved_keys_and_data():
    [draft] = parse(
        {"id": 17, "text": None, "url": "https://x", "at": datetime(2026, 9, 26), "tags": {"a"}}
    )
    assert draft.external_id == "17"
    assert draft.text == ""
    assert draft.data == {"url": "https://x", "at": "2026-09-26T00:00:00", "tags": ["a"]}


async def test_hash_identity_without_id():
    async with http_with({}) as http:
        a = await finalize_ids(parse([{"text": "одно"}, {"text": "одно"}, {"text": "другое"}]), http, LIMIT)
    assert len(a) == 2
    assert all(d.external_id.startswith("sha256:") for d in a)
    assert a[0].external_id != a[1].external_id


async def test_duplicate_ids_in_one_result_collapse():
    async with http_with({}) as http:
        drafts = await finalize_ids(parse([{"id": 1, "text": "a"}, {"id": 1, "text": "b"}]), http, LIMIT)
    assert [d.text for d in drafts] == ["a"]


async def test_image_variants():
    b64 = base64.b64encode(PNG).decode()
    [draft] = parse(
        {
            "id": "x",
            "image": [PNG, b64, f"data:image/png;base64,{b64}", "https://img/1.jpg", "https://img/404"],
        }
    )
    async with http_with({"https://img/1.jpg": JPEG}) as http:
        await draft.load_images(http, LIMIT)
    assert [ext for _, ext in draft.images] == ["png", "png", "png", "jpg"]
    assert len(draft.image_errors) == 1 and "404" in draft.image_errors[0]


async def test_not_an_image_is_reported():
    [draft] = parse({"id": "x", "image": b"plain text"})
    async with http_with({}) as http:
        await draft.load_images(http, LIMIT)
    assert draft.images == [] and draft.image_errors


async def test_images_count_in_hash():
    async with http_with({}) as http:
        drafts = await finalize_ids(parse([{"text": "t", "image": PNG}, {"text": "t", "image": JPEG}]), http, LIMIT)
    assert len(drafts) == 2


def test_sniff_webp():
    assert sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
