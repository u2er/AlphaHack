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


async def test_each_snapshot_version_is_merged_once_in_batches(tmp_path, monkeypatch):
    import json
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(SNAPSHOT), encoding="utf-8")
    calls, marks = [], {}

    async def import_graph(graph, nodes, edges):
        calls.append((graph, len(nodes), len(edges)))

    async def marker(graph):
        return marks.get(graph, {})

    async def mark(graph, value):
        marks[graph] = value

    async def present(graph, names):
        return 0  # the graph has its own events, none of the snapshot's

    monkeypatch.setattr(seed.graph_store, "import_graph", import_graph)
    monkeypatch.setattr(seed, "BATCH", 1)
    monkeypatch.setattr(seed, "marker", marker)
    monkeypatch.setattr(seed, "mark", mark)
    monkeypatch.setattr(seed, "present", present)

    assert await seed.seed_graph("news", path) == 3  # merged into a non-empty graph too
    assert calls == [("news", 1, 0), ("news", 1, 0), ("news", 0, 1)]
    assert await seed.seed_graph("news", path) == 0 and len(calls) == 3  # this version is done
    path.write_text(json.dumps({**SNAPSHOT, "nodes": SNAPSHOT["nodes"][:1]}), encoding="utf-8")
    assert await seed.seed_graph("news", path) == 1  # a new version is merged again


async def test_a_graph_seeded_before_markers_is_only_marked(tmp_path, monkeypatch):
    import json
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(SNAPSHOT), encoding="utf-8")
    marks = {}

    async def marker(graph):
        return {}

    async def mark(graph, value):
        marks[graph] = value

    async def present(graph, names):
        return len(names)

    async def import_graph(graph, nodes, edges):
        raise AssertionError("must not import")

    monkeypatch.setattr(seed.graph_store, "import_graph", import_graph)
    monkeypatch.setattr(seed, "marker", marker)
    monkeypatch.setattr(seed, "mark", mark)
    monkeypatch.setattr(seed, "present", present)
    assert await seed.seed_graph("news", path) == 0
    assert marks["news"]["file"] == "snap.json"


@pytest.mark.parametrize("value, expected", [("builtin", seed.BUILTIN), ("off", None), ("", None)])
def test_graph_seed_setting(monkeypatch, value, expected):
    monkeypatch.setattr(seed.settings, "graph_seed", value)
    assert seed.source() == expected
