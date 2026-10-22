"""Страничный профиль на живом Postgres: фрагменты, поиск по словам (русская
морфология), по смыслу и гибридный, чтение страниц окнами и инструменты аналитика.

    DATABASE_URL=postgresql+asyncpg://eventgraph:eventgraph@localhost:55432/eventgraph \\
        pytest -m integration tests/integration/test_profile_pages.py
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import numpy as np
import pytest

from eventgraph import profiles
from eventgraph.agents.specs import SPECS
from eventgraph.agents.tools import ToolContext, build_tools
from eventgraph.db import SessionLocal, create_schema, engine
from eventgraph.models import CompanyProfile
from tests.integration.test_graph_store import fake_vector

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "profiles" / "vectorfood-v2.json"


@pytest.fixture
async def profile_id(monkeypatch):
    class FakeEmbed:
        @staticmethod
        async def embed(texts, urgent=False):
            return np.stack([fake_vector(t) for t in texts])

        @staticmethod
        async def embed_one(text, urgent=True):
            return fake_vector(text)

    monkeypatch.setattr(profiles, "embed", FakeEmbed)
    await create_schema()
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["profile_id"] = f"t-{uuid.uuid4().hex[:8]}"
    payload["pages"][3]["content"] += "\n\n" + "Отчёт о рисках. " * 900  # long finance page: windows
    await profiles.upsert(payload)
    yield payload["profile_id"]
    async with SessionLocal() as session:
        await session.delete(await session.get(CompanyProfile, payload["profile_id"]))
        await session.commit()
    await engine.dispose()


async def test_keyword_search_understands_russian_word_forms(profile_id):
    found = await profiles.search(profile_id, "рефинансировать кредиты", mode="keyword")
    assert found["ok"] and found["results"][0]["page_id"] in ("finance", "file-1")
    assert all(r["page_id"] != "main" for r in found["results"])
    nothing = await profiles.search(profile_id, "криптовалюта", mode="keyword")
    assert nothing["results"] == []


async def test_vector_and_hybrid_search(profile_id):
    vector = await profiles.search(profile_id, "упаковка Tetra Pak замена", mode="vector", limit=3)
    assert vector["results"] and "vector_score" in vector["results"][0]
    hybrid = await profiles.search(profile_id, "упаковка Tetra Pak", mode="hybrid", limit=3)
    assert hybrid["results"][0]["page_id"] in ("counterparties", "file-1")
    only = await profiles.search(profile_id, "упаковка", mode="hybrid", pages=["products"])
    assert {r["page_id"] for r in only["results"]} <= {"products"}


async def test_pages_are_read_in_windows(profile_id):
    first = await profiles.page(profile_id, "finance", limit=1000)
    assert first["ok"] and first["next_offset"] == 1000 and first["total_chars"] > 10000
    later = await profiles.page(profile_id, "finance", offset=first["next_offset"], limit=1000)
    assert later["offset"] == 1000
    missing = await profiles.page(profile_id, "nope")
    assert not missing["ok"] and missing["pages"][0]["id"] == "main"


async def test_analyst_tools_search_and_read_the_profile(profile_id):
    tools = {t.name: t for t in build_tools(ToolContext(spec=SPECS["analyst"], graph="news", profile_id=profile_id))}
    hits = await tools["profile_search"].handler(query="сырое молоко себестоимость", mode="keyword")
    assert hits["results"]
    page = await tools["profile_page"].handler(page_id="counterparties")
    assert "Tetra Pak" in page["content"]


async def test_a_new_version_replaces_the_fragments(profile_id):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["profile_id"] = profile_id
    payload["pages"] = payload["pages"][:2]
    await profiles.upsert(payload)
    found = await profiles.search(profile_id, "Tetra Pak", mode="keyword")
    assert found["results"] == []


async def test_the_seed_snapshot_loads_into_an_empty_graph(monkeypatch):
    from eventgraph import seed
    from eventgraph.graph import store as store_module
    from eventgraph.graph.store import graph_store
    from tests.integration.test_graph_store import fake_vector

    async def embed(texts, urgent=False):
        return np.stack([fake_vector(t) for t in texts])

    monkeypatch.setattr(store_module, "embed", embed)
    await create_schema()
    graph = f"seed-{uuid.uuid4().hex[:6]}"
    snapshot = seed.load(seed.BUILTIN)
    keep = {n["id"] for n in snapshot["nodes"][:40]}
    small = {"nodes": [n for n in snapshot["nodes"] if n["id"] in keep],
             "edges": [e for e in snapshot["edges"] if e["source_id"] in keep and e["target_id"] in keep]}
    path = Path(__import__("tempfile").mkdtemp()) / "small.json"
    path.write_text(json.dumps(small), encoding="utf-8")
    try:
        done = await seed.seed_if_empty(graph, path)
        assert done == len(small["nodes"]) + len(small["edges"])
        assert await seed.node_count(graph) == len(small["nodes"])
        assert await seed.seed_if_empty(graph, path) == 0  # not empty any more
    finally:
        await graph_store.delete_graph(graph)
        await engine.dispose()
