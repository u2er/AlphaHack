"""Граф на живом Postgres с pgvector: запись по имени, рёбра, поиск, обход,
слияние и инструменты агентов.

Нужен Postgres с pgvector и `DATABASE_URL` — до импорта пакета:

    DATABASE_URL=postgresql+asyncpg://eventgraph:eventgraph@localhost:55432/eventgraph \\
        pytest -m integration tests/integration

Эмбеддер подставной: мешок слов, свёрнутый в 64 измерения, — похожие тексты
дают близкие векторы, и всё детерминировано.
"""

from __future__ import annotations

import hashlib
import re
import uuid

import numpy as np
import pytest
from sqlalchemy import func, select

from eventgraph.agents.specs import SPECS
from eventgraph.agents.tools import ToolContext, build_tools
from eventgraph.db import SessionLocal, create_schema, engine
from eventgraph.graph import store as store_module
from eventgraph.graph.merge import graph_merger
from eventgraph.graph.store import GraphConflict, GraphNotFound, graph_store
from eventgraph.models import GraphItem, GraphMergeDecision

pytestmark = pytest.mark.integration

DIM = 64


def fake_vector(text: str) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    for word in re.findall(r"\w+", text.casefold()):
        v[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1.0
    norm = np.linalg.norm(v)
    return v / norm if norm else v + 1 / np.sqrt(DIM)


@pytest.fixture
async def graph(monkeypatch):
    async def embed(texts, urgent=False):
        return np.stack([fake_vector(t) for t in texts])

    async def embed_one(text, urgent=True):
        return fake_vector(text)

    monkeypatch.setattr(store_module, "embed", embed)
    monkeypatch.setattr(store_module, "embed_one", embed_one)
    await create_schema()
    name = f"t-{uuid.uuid4().hex[:8]}"
    yield name
    await graph_store.delete_graph(name)
    # Пул соединений привязан к циклу событий теста.
    await engine.dispose()


async def test_a_node_name_is_unique_and_meta_merges(graph):
    a, created = await graph_store.upsert_node(graph, "Томский государственный университет", meta={"type": "org"})
    b, again = await graph_store.upsert_node(graph, "  томский   ГОСУДАРСТВЕННЫЙ университет ", meta={"aliases": ["ТГУ"]})
    assert created and not again and a.id == b.id
    assert b.name == "Томский государственный университет"
    assert b.attrs == {"type": "org", "aliases": ["ТГУ"]}
    replaced, _ = await graph_store.upsert_node(graph, "Томский государственный университет",
                                                meta={"type": "university"}, replace_meta=True)
    assert replaced.attrs == {"type": "university"}


async def test_edges_create_missing_ends_and_are_found_by_their_four(graph):
    edge, created = await graph_store.upsert_edge(graph, "involves", source="Событие", target="ТГУ",
                                                  meta={"role": "получатель"})
    assert created and edge.label == "Событие — involves → ТГУ"
    again, created = await graph_store.upsert_edge(graph, "INVOLVES", source="событие", target="тгу", meta={"x": 1})
    assert not created and again.id == edge.id and again.attrs == {"role": "получатель", "x": 1}
    # Другое направление — другое ребро; ненаправленное находится с обеих сторон.
    back, created = await graph_store.upsert_edge(graph, "involves", source="ТГУ", target="Событие")
    assert created and back.id != edge.id
    link, _ = await graph_store.upsert_edge(graph, "near", source="А", target="Б", directed=False)
    same, created = await graph_store.upsert_edge(graph, "near", source="Б", target="А", directed=False)
    assert not created and same.id == link.id and link.label == "А — near — Б"


async def test_import_is_all_or_nothing(graph):
    with pytest.raises(ValueError):
        await graph_store.import_graph(graph, [{"name": "Узел"}], [{"name": "e", "source": "Узел", "target": "Б",
                                                                  "items": [{"type": "video", "data": "x"}]}])
    assert (await graph_store.dump(graph))["nodes"] == []
    out = await graph_store.import_graph(
        graph,
        [{"name": "Узел", "meta": {"type": "event"}, "items": [{"type": "text", "data": "Текст узла"}]}],
        [{"name": "about", "source": "Узел", "target": "наука"}],
    )
    assert [n["created"] for n in out["nodes"]] == [True] and out["edges"][0]["created"] is True
    dump = await graph_store.dump(graph)
    assert {n["name"] for n in dump["nodes"]} == {"Узел", "наука"} and len(dump["edges"]) == 1


async def test_search_by_meaning_with_exact_filters(graph):
    await graph_store.import_graph(graph, [
        {"name": "2026-10-13 · ТГУ получил сертификат на полёты БПЛА",
         "meta": {"type": "event", "summary": "Росавиация выдала университету сертификат на коммерческие полёты дронов"}},
        {"name": "2026-10-14 · В Томске прошёл марафон", "meta": {"type": "event"}},
        {"name": "Росавиация", "meta": {"type": "org"}},
    ], [{"name": "involves", "source": "2026-10-13 · ТГУ получил сертификат на полёты БПЛА", "target": "Росавиация"}])

    found = await graph_store.search(graph, "сертификат полёты дронов", limit=5, score_threshold=0.2)
    top = found["results"][0]
    assert top["element"]["name"].startswith("2026-10-13")
    # Длинная сводка ищется своим вектором и видна как совпадение.
    assert any(m["type"] == "attr" and m["key"] == "summary" for m in top["matches"])

    only_orgs = await graph_store.search(graph, "Росавиация сертификат", score_threshold=0.1, meta={"type": "org"})
    assert [r["element"]["name"] for r in only_orgs["results"]] == ["Росавиация"]
    edges = await graph_store.search(graph, "involves Росавиация", score_threshold=0.1, kinds=["edge"], expand=1)
    assert edges["results"][0]["element"]["kind"] == "edge"
    assert {n["name"] for n in edges["subgraph"]["nodes"]} >= {"Росавиация"}


async def test_neighbors_and_detail(graph):
    await graph_store.import_graph(graph, [], [
        {"name": "involves", "source": "E1", "target": "P1"},
        {"name": "works_at", "source": "P1", "target": "O1"},
        {"name": "in", "source": "E1", "target": "Город"},
    ])
    e1 = await graph_store.resolve_node(graph, name="E1")
    one = await graph_store.neighbors(graph, [e1], depth=1)
    assert {n["name"] for n in one["nodes"]} == {"E1", "P1", "Город"}
    two = await graph_store.neighbors(graph, [e1], depth=2, edge_names=["involves", "works_at"])
    assert {n["name"] for n in two["nodes"]} == {"E1", "P1", "O1"}
    p1 = await graph_store.resolve_node(graph, name="P1")
    incoming = await graph_store.neighbors(graph, [p1], depth=1, direction="in")
    assert {n["name"] for n in incoming["nodes"]} == {"P1", "E1"}
    detail = await graph_store.detail(p1)
    assert sorted((e["name"], e["direction"], e["other"]["name"]) for e in detail["edges"]) == [
        ("involves", "in", "E1"), ("works_at", "out", "O1")]


async def test_rename_relabels_edges_and_refuses_a_taken_name(graph):
    await graph_store.upsert_edge(graph, "involves", source="Событие", target="Минобороны")
    node = await graph_store.resolve_node(graph, name="Минобороны")
    await graph_store.update(graph, "node", node.id, name="Министерство обороны")
    edge = (await graph_store.dump(graph))["edges"][0]
    full = await graph_store.get(graph, edge["id"], "edge")
    assert full.label == "Событие — involves → Министерство обороны"
    with pytest.raises(GraphConflict):
        await graph_store.update(graph, "node", node.id, name="событие")


async def test_deleting_a_node_takes_its_edges(graph):
    await graph_store.upsert_edge(graph, "about", source="Событие", target="наука",
                                  items=[{"type": "text", "data": "пояснение"}])
    node = await graph_store.resolve_node(graph, name="наука")
    await graph_store.delete(graph, "node", node.id)
    dump = await graph_store.dump(graph)
    assert [n["name"] for n in dump["nodes"]] == ["Событие"] and dump["edges"] == []
    with pytest.raises(GraphNotFound):
        await graph_store.get(graph, node.id)


async def test_merge_moves_edges_absorbs_twins_and_logs(graph):
    await graph_store.import_graph(graph, [
        {"name": "Министерство обороны Российской Федерации", "meta": {"type": "org", "about": "ведомство"}},
        {"name": "Минобороны России", "meta": {"type": "org", "about": "другое", "aliases": ["МО"]},
         "items": [{"type": "text", "data": "заметка"}]},
    ], [
        {"name": "involves", "source": "E1", "target": "Министерство обороны Российской Федерации"},
        {"name": "involves", "source": "E1", "target": "Минобороны России", "meta": {"role": "заказчик"}},
        {"name": "involves", "source": "E2", "target": "Минобороны России"},
        {"name": "part_of", "source": "Минобороны России", "target": "Министерство обороны Российской Федерации"},
    ])
    keep = await graph_store.resolve_node(graph, name="Министерство обороны Российской Федерации")
    drop = await graph_store.resolve_node(graph, name="Минобороны России")

    merged = await graph_merger.merge(graph, keep.id, drop.id, "a")

    assert merged.attrs == {"type": "org", "about": "ведомство", "aliases": ["МО"]}
    dump = await graph_store.dump(graph)
    assert "Минобороны России" not in {n["name"] for n in dump["nodes"]}
    names = {n["id"]: n["name"] for n in dump["nodes"]}
    edges = sorted((names[e["source_id"]], e["name"], names[e["target_id"]], tuple(e["meta"].items())) for e in dump["edges"])
    # Двойник слит с существующим ребром (поля — оставшегося, недостающие — дописаны),
    # связь между самими дублями удалена.
    assert edges == [
        ("E1", "involves", "Министерство обороны Российской Федерации", (("role", "заказчик"),)),
        ("E2", "involves", "Министерство обороны Российской Федерации", ()),
    ]
    async with SessionLocal() as s:
        items = (await s.execute(select(GraphItem).where(GraphItem.element_id == keep.id, GraphItem.type == "text"))).scalars().all()
        decisions = await s.scalar(select(func.count()).select_from(GraphMergeDecision).where(GraphMergeDecision.graph == graph))
    assert [i.content for i in items] == ["заметка"] and decisions == 1


async def test_candidates_skip_rejected_pairs(graph):
    await graph_store.import_graph(graph, [
        {"name": "Федеральная налоговая служба", "meta": {"type": "org"}},
        {"name": "Федеральная налоговая служба России", "meta": {"type": "org"}},
        {"name": "Совсем другое", "meta": {"type": "org"}},
    ], [])
    found = await graph_merger.candidates(graph, threshold=0.8)
    assert found["total"] == 1
    pair = found["pairs"][0]
    await graph_merger.reject(graph, pair["a"]["id"], pair["b"]["id"])
    assert (await graph_merger.candidates(graph, threshold=0.8))["total"] == 0


async def test_agent_tools_on_the_real_graph(graph):
    news_tools = {t.name: t.handler for t in build_tools(ToolContext(SPECS["news"], graph))}
    out = await news_tools["graph_upsert"](
        nodes=[{"name": "2026-10-17 · Мэр Томска ушёл в отставку",
                "meta": {"type": "event", "date": "2026-10-17", "importance": 5, "summary": "Мэр Томска подал в отставку."}},
               {"name": "Иван Петров", "meta": {"type": "person", "role": "мэр Томска"}}],
        edges=[{"source": "2026-10-17 · Мэр Томска ушёл в отставку", "target": "Иван Петров", "name": "involves",
                "meta": {"role": "ушедший"}},
               {"source": "2026-10-17 · Мэр Томска ушёл в отставку", "target": "РИА Новости", "name": "reported_by",
                "meta": {"quote": "Мэр Томска Иван Петров подал в отставку", "url": "https://ria.ru/1"}}],
    )
    assert out["ok"] is True and len(out["edges"]) == 2

    node = await news_tools["graph_node"](name="иван петров")
    assert node["found"] and node["node"]["edges"][0]["other"] == "2026-10-17 · Мэр Томска ушёл в отставку"
    hits = await news_tools["graph_search"](query="отставка мэра", meta={"type": "event"}, score_threshold=0.1)
    assert hits["results"][0]["name"] == "2026-10-17 · Мэр Томска ушёл в отставку"

    period = await news_tools["graph_period"](since="2026-10-12", until="2026-10-18")
    assert period["events_total"] == 1 and period["people"][0]["name"] == "Иван Петров"
    around = await news_tools["graph_neighbors"](name="2026-10-17 · Мэр Томска ушёл в отставку")
    assert {e["target"] for e in around["edges"]} == {"Иван Петров", "РИА Новости"}
