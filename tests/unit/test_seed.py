"""Базовый граф: снимок → узлы и рёбра по именам; загрузка только в пустой граф."""

from __future__ import annotations

import pytest

from eventgraph import seed


SNAPSHOT = {"graph": "news", "nodes": [
    {"id": "a", "name": "2026-10-11 · ЦБ сохранил ставку", "meta": {"type": "event", "importance": 4}},
    {"id": "b", "name": "Банк России", "meta": {"type": "org"}},
], "edges": [
    {"id": "e1", "name": "involves", "source_id": "a", "target_id": "b", "directed": True, "meta": {}},
    {"id": "e2", "name": "involves", "source_id": "a", "target_id": "missing", "directed": True, "meta": {}},
]}


def test_edges_find_their_ends_by_name_and_dangling_ones_are_dropped():
    nodes, edges = seed.payload(SNAPSHOT)
    assert [n["name"] for n in nodes] == ["2026-10-11 · ЦБ сохранил ставку", "Банк России"]
    assert edges == [{"name": "involves", "source": "2026-10-11 · ЦБ сохранил ставку", "target": "Банк России",
                      "directed": True, "meta": {}}]


def test_the_builtin_snapshot_is_shipped():
    nodes, edges = seed.payload(seed.load(seed.BUILTIN))
    assert len(nodes) > 2000 and len(edges) > 6000


async def test_only_an_empty_graph_is_seeded_in_batches(tmp_path, monkeypatch):
    import json
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(SNAPSHOT), encoding="utf-8")
    calls = []

    async def import_graph(graph, nodes, edges):
        calls.append((graph, len(nodes), len(edges)))

    monkeypatch.setattr(seed.graph_store, "import_graph", import_graph)
    monkeypatch.setattr(seed, "BATCH", 1)

    async def full(graph):
        return 5

    async def empty(graph):
        return 0

    monkeypatch.setattr(seed, "node_count", full)
    assert await seed.seed_if_empty("news", path) == 0 and calls == []
    monkeypatch.setattr(seed, "node_count", empty)
    assert await seed.seed_if_empty("news", path) == 3
    assert calls == [("news", 1, 0), ("news", 1, 0), ("news", 0, 1)]


@pytest.mark.parametrize("value, expected", [("builtin", seed.BUILTIN), ("off", None), ("", None)])
def test_graph_seed_setting(monkeypatch, value, expected):
    monkeypatch.setattr(seed.settings, "graph_seed", value)
    assert seed.source() == expected
