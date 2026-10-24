"""Граф глазами агентов: права инструментов, период, обслуживание —
на подставном хранилище графа."""

from __future__ import annotations

import datetime

import pytest

from eventgraph.agents import tools as agent_tools
from eventgraph.agents.specs import SPECS, AgentSpec
from eventgraph.agents.tools import ToolContext, build_tools, graph_items, system_text
from eventgraph.graph import upkeep
from eventgraph.models import GraphElement


@pytest.fixture(autouse=True)
def no_web(monkeypatch):
    # В контейнере compose задаёт SEARXNG_URL; права в графе проверяются без поиска.
    monkeypatch.setattr(agent_tools.settings, "searxng_url", "")


# Агент без прав в графе: в проекте такого нет, но права проверяются и на нём.
READER = AgentSpec("reader", "news.md", max_steps=5, temperature=0)


def names(spec_name, attachments=None):
    spec = READER if spec_name == "reader" else SPECS[spec_name]
    return {t.name for t in build_tools(ToolContext(spec, "news", attachments or {}))}


def handlers(spec_name, attachments=None):
    return {t.name: t.handler for t in build_tools(ToolContext(SPECS[spec_name], "news", attachments or {}))}


READ = {"graph_search", "graph_node", "graph_neighbors", "graph_period"}
WRITE = {"graph_upsert", "graph_update"}
UPKEEP = {"graph_duplicates", "graph_merge", "graph_not_duplicates", "graph_doubt", "graph_islands",
          "graph_check", "graph_prune"}


def test_tools_follow_the_rights():
    assert names("reader") == READ
    assert names("news") == READ | WRITE
    assert names("news", {"01A-0.jpg": "items/a.jpg"}) == READ | WRITE | {"view_image"}
    assert names("gardener") == READ | WRITE | {"graph_delete"} | UPKEEP


def test_system_text_does_not_lie_about_writing():
    reader = system_text(ToolContext(READER, "news"))
    assert "read-only" in reader and "graph_upsert" not in reader
    assert "graph_upsert" in system_text(ToolContext(SPECS["news"], "news"))
    assert "Upkeep tools are on" in system_text(ToolContext(SPECS["gardener"], "news"))


def test_simple_fields_become_items():
    items = graph_items({
        "text": " Сводка ",
        "sources": [{"title": "Т", "url": "https://x", "outlet": "РИА", "quote": "Цитата", "item_uid": "01A"},
                    {"kind": "post"}],
        "refs": ["abc"],
    })
    assert items == [
        {"type": "text", "data": "Сводка"},
        {"type": "source", "data": {"kind": "article", "source_name": "РИА", "title": "Т", "url": "https://x",
                                    "item_uid": "01A", "content": "Цитата"}},
        {"type": "ref", "id": "abc"},
    ]


# ── подставное хранилище ─────────────────────────────────────────────────────


def _node(i, name, kind, **meta):
    created = meta.pop("_created", "2026-01-01")
    return {"id": i, "kind": "node", "name": name, "meta": {"type": kind, **meta}, "created_at": created}


def _edge(i, a, b, name, **meta):
    return {"id": i, "kind": "edge", "source_id": a, "target_id": b, "name": name, "meta": meta}


def period_dump():
    """Неделя 12–18 октября: два события про ТГУ, одно — продолжение сентябрьского."""
    return {
        "nodes": [
            _node("e1", "2026-10-13 · ТГУ получил сертификат на полёты БПЛА", "event",
                  date="2026-10-13", importance=4, sources_count=3, summary="Росавиация выдала ТГУ сертификат."),
            _node("e2", "2026-10-15 · В ТГУ открыли лабораторию", "event", date="2026-10-15", importance=2),
            _node("e3", "2026-10-17 · Мэр Томска ушёл в отставку", "event", date="2026-10-17", importance=5,
                  sources_count=7),
            _node("e0", "2026-09-20 · Мэр Томска под следствием", "event", date="2026-09-20", importance=4),
            _node("e9", "2026-10-25 · Вне недели", "event", date="2026-10-25", importance=5),
            _node("p1", "Иван Петров", "person", role="мэр Томска", _created="2026-10-17"),
            _node("o1", "Томский государственный университет", "org", aliases=["ТГУ"]),
            _node("pl", "Томск", "place"),
            _node("t1", "Томск", "topic"),
            _node("t2", "наука", "topic"),
        ],
        "edges": [
            _edge("x1", "e1", "o1", "involves", role="получатель"),
            _edge("x2", "e1", "pl", "in"),
            _edge("x3", "e1", "t2", "about"),
            _edge("x4", "e2", "o1", "involves"),
            _edge("x5", "e3", "p1", "involves", role="ушедший"),
            _edge("x6", "e3", "pl", "in"),
            _edge("x7", "e3", "t1", "about"),
            _edge("x8", "e3", "e0", "develops"),
        ],
        "truncated": False,
    }


class FakeStore:
    def __init__(self, dump=None, nodes=None):
        self._dump = dump or {"nodes": [], "edges": [], "truncated": False}
        self.nodes = nodes or {}
        self.imports, self.updates, self.deletes = [], [], []

    async def dump(self, graph, limit=50_000):
        return self._dump

    async def import_graph(self, graph, nodes, edges):
        self.imports.append((nodes, edges))
        return {"nodes": [{"id": f"n{i}", "name": n["name"], "created": True} for i, n in enumerate(nodes)],
                "edges": [{"id": f"e{i}", "name": e["name"], "created": True} for i, e in enumerate(edges)]}

    async def update(self, graph, kind, element_id, name=None, meta=None, replace_meta=False):
        self.updates.append((kind, element_id, name, meta, replace_meta))
        return GraphElement(id=element_id, kind=kind, graph=graph, name=name or "x", attrs=meta or {})

    async def delete(self, graph, kind, element_id):
        self.deletes.append((kind, element_id))

    async def find_node(self, graph, name):
        return self.nodes.get(name.casefold())

    async def detail(self, el):
        return {"id": el.id, "kind": "node", "name": el.name, "meta": el.attrs, "items": [], "refs": [], "edges": []}

    async def search(self, graph, query, **kwargs):
        self.last_search = (query, kwargs)
        return {"results": [{"score": 0.7, "element": {"id": "e3", "kind": "node", "name": "2026-10-17 · Мэр Томска ушёл в отставку",
                                                       "meta": {"type": "event", "summary": "с" * 1000}},
                             "matches": [{"type": "attr", "key": "summary", "text": "сводка"}]}]}


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore(period_dump())
    monkeypatch.setattr(agent_tools, "graph_store", fake)
    return fake


async def test_upsert_builds_the_import_and_refuses_bad_input(store):
    tools = handlers("news")
    out = await tools["graph_upsert"](
        nodes=[{"name": " Событие ", "meta": {"type": "event", "summary": "", "aliases": []}}],
        edges=[{"source": "Событие", "target": "РИА Новости", "name": "reported_by",
                "meta": {"quote": "Цитата", "url": "https://x"}}],
    )
    assert out["ok"] is True and out["nodes"][0]["name"] == "Событие"
    nodes, edges = store.imports[0]
    assert nodes == [{"name": "Событие", "meta": {"type": "event"}, "items": []}]
    assert edges[0] == {"name": "reported_by", "directed": True, "meta": {"quote": "Цитата", "url": "https://x"},
                        "items": [], "source": "Событие", "target": "РИА Новости"}
    assert (await tools["graph_upsert"](nodes=[{"meta": {}}]))["ok"] is False
    assert (await tools["graph_upsert"](edges=[{"source": "A", "name": "in"}]))["ok"] is False
    assert (await tools["graph_upsert"]())["ok"] is False


def test_period_ranks_events_and_finds_the_centre():
    out = upkeep.period(period_dump(), datetime.date(2026, 10, 12), datetime.date(2026, 10, 18))

    assert out["events_total"] == 3
    assert [e["name"][:10] for e in out["events"]] == ["2026-10-17", "2026-10-13", "2026-10-15"]
    top = out["events"][0]
    assert top["involves"] == ["Иван Петров (ушедший)"] and top["develops"] == ["2026-09-20 · Мэр Томска под следствием"]
    assert out["people"] == [{"name": "Иван Петров", "events": 1, "about": "мэр Томска", "roles": ["ушедший"],
                              "top_events": ["2026-10-17 · Мэр Томска ушёл в отставку"], "new": True}]
    assert out["orgs"][0]["name"] == "Томский государственный университет" and out["orgs"][0]["events"] == 2
    assert out["stories"] == [[
        {"date": "2026-09-20", "name": "2026-09-20 · Мэр Томска под следствием", "before": True},
        {"date": "2026-10-17", "name": "2026-10-17 · Мэр Томска ушёл в отставку"},
    ]]
    assert out["topics"] == {"наука": 1, "Томск": 1}
    assert "digests" not in out


def test_period_filters():
    week = (datetime.date(2026, 10, 12), datetime.date(2026, 10, 18))
    assert upkeep.period(period_dump(), *week, min_importance=3)["events_total"] == 2
    assert [e["date"] for e in upkeep.period(period_dump(), *week, topics=["томск"])["events"]] == ["2026-10-17"]
    by_alias = upkeep.period(period_dump(), *week, entity="ТГУ")
    assert by_alias["events_total"] == 2 and by_alias["people"] == []
    short = upkeep.period(period_dump(), *week, limit=1)
    assert len(short["events"]) == 1 and len(short["more_events"]) == 2


async def test_period_tool_reads_the_dump(store):
    out = await handlers("news")["graph_period"](since="2026-10-18", until="2026-10-12", min_importance="4")
    assert out["ok"] is True and out["since"] == "2026-10-12" and out["events_total"] == 2
    assert (await handlers("news")["graph_period"](since="вчера"))["ok"] is False


TEXT = "Неделя отставки.\n\n## Главное\nМэр ушёл.\n\n## Люди\nИван Петров."


async def test_search_hits_are_compact_and_keep_the_filter(store):
    tools = handlers("news")
    hit = (await tools["graph_search"](query="отставка", meta={"type": "event"}))["results"][0]
    assert hit["name"].startswith("2026-10-17") and len(hit["meta"]["summary"]) <= 301
    assert hit["matched"] == [{"type": "attr", "text": "сводка", "key": "summary"}]
    assert store.last_search[1]["meta"] == {"type": "event"}
    assert (await tools["graph_node"](name="Нет такого"))["found"] is False


async def test_prune_is_dry_by_default_and_live_needs_ids(store):
    tools = handlers("gardener")
    dry = await tools["graph_prune"](older_than_days=1, max_importance=5, max_sources=10)
    assert dry["dry_run"] is True and store.deletes == []
    refused = await tools["graph_prune"](dry_run=False)
    assert refused["ok"] is False and "ids" in refused["error"]
    live = await tools["graph_prune"](dry_run=False, ids=[], older_than_days=1)
    assert live["ok"] is True and live["events"] == 0


async def test_view_image_shows_each_picture_once(monkeypatch, tmp_path):
    from eventgraph.config import settings

    (tmp_path / "items").mkdir()
    (tmp_path / "items" / "a.png").write_bytes(
        bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                      "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
    )
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    view = handlers("news", {"01A-0.png": "items/a.png"})["view_image"]
    first = await view(path="01A-0.png")
    assert len(first.images) == 1 and first.images[0].mime == "image/png"
    assert "Already viewed" in (await view(path="01A-0.png"))["error"]
    assert "pictures" in await view(path="чужая.png")
