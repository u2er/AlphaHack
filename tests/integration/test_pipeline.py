"""Сквозной путь на живом Postgres: записи коннектора → сюжеты → каждая
запись отдельным раном агента → инструменты пишут граф → вердикты; пауза
разбора после сбоя модели.

Модель подставная: отвечает по сценарию, но инструменты, раны и граф —
настоящие.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from sqlalchemy import delete, select

from eventgraph import embed as embed_module
from eventgraph import graph_settings, news
from eventgraph.agents import runner
from eventgraph.config import settings
from eventgraph.db import SessionLocal, create_schema, engine
from eventgraph.graph.store import graph_store
from eventgraph.models import AgentRun, Connector, Item
from eventgraph.worker import Worker

from .test_graph_store import fake_vector

pytestmark = pytest.mark.integration


def call(name, args, call_id):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def answer(content=None, calls=None):
    return {"content": content, "reasoning": None, "tool_calls": calls,
            "usage": {"prompt_tokens": 100, "completion_tokens": 10}}


@pytest.fixture
async def world(monkeypatch):
    async def create(texts):
        return np.stack([fake_vector(t) for t in texts])

    monkeypatch.setattr(embed_module, "_create", create)
    graph = f"p-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "graph_name", graph)
    monkeypatch.setattr(settings, "llm_api_key", "test")
    monkeypatch.setattr(settings, "agents", "news")
    monkeypatch.setattr(settings, "duplicate_threshold", 0.97)
    await create_schema()
    name = f"it_{uuid.uuid4().hex[:8]}"
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    async with SessionLocal() as s:
        connector = Connector(name=name, state={})
        s.add(connector)
        await s.flush()
        texts = [
            ("Мэр Томска Иван Петров подал в отставку", "ria"),
            ("Мэр Томска Иван Петров подал в отставку сегодня утром", "tass"),
            ("В Томске открыли новый мост через Томь", "ria"),
        ]
        for n, (text, outlet) in enumerate(texts):
            s.add(Item(uid=f"01T{uuid.uuid4().hex[:10].upper()}", connector_id=connector.id, external_id=f"{name}-{n}",
                       text=text, data={"title": text, "url": f"https://{outlet}.ru/{n}", "source": outlet},
                       images=[], collected_at=t0 + timedelta(minutes=n)))
        await s.commit()
        connector_id = connector.id
    yield {"graph": graph, "connector_id": connector_id}
    await graph_store.delete_graph(graph)
    async with SessionLocal() as s:
        await s.execute(delete(Connector).where(Connector.id == connector_id))
        await s.commit()
    await engine.dispose()


EVENT = "2026-10-20 · Мэр Томска ушёл в отставку"


def uid_of(task: str) -> str:
    return next(line.split(" · ")[0][4:] for line in task.splitlines() if line.startswith("### "))


async def scripted(messages, tools, params, client=None):
    """Агент по сценарию: первая запись об отставке — новое событие, вторая
    (того же сюжета) — ещё одно издание у него, мост — пропуск."""
    task = next(m["content"] for m in messages if m["role"] == "user")
    uid = uid_of(task)
    if messages[-1]["role"] == "tool":
        verdict = ({"uid": uid, "verdict": "merged", "event": EVENT} if "Сюжет: в графе по нему уже есть" in task
                   else {"uid": uid, "verdict": "stored", "event": EVENT})
        return answer("Готово.\n```json\n" + json.dumps({"items": [verdict]}, ensure_ascii=False) + "\n```")
    if "мост" in task:
        return answer('Не про отставку.\n```json\n{"items": [{"uid": "%s", "verdict": "skipped", "reason": "проверка пропуска"}]}\n```' % uid)
    if "Сюжет: в графе по нему уже есть" in task:
        return answer(calls=[call("graph_upsert", {"edges": [
            {"source": EVENT, "target": "ТАСС", "name": "reported_by", "meta": {"item_uid": uid}}]}, "c2")])
    return answer(calls=[call("graph_upsert", {
        "nodes": [{"name": EVENT, "meta": {"type": "event", "date": "2026-10-20", "importance": 4,
                                           "summary": "Иван Петров подал в отставку."}},
                  {"name": "Иван Петров", "meta": {"type": "person", "role": "мэр Томска"}}],
        "edges": [{"source": EVENT, "target": "Иван Петров", "name": "involves", "meta": {"role": "ушедший"}},
                  {"source": EVENT, "target": "РИА Новости", "name": "reported_by", "meta": {"item_uid": uid}}],
    }, "c1")])


async def test_each_record_is_processed_on_its_own_as_it_comes(world, monkeypatch):
    seen: list[str] = []

    async def generate(messages, tools, params, client=None):
        if messages[-1]["role"] == "user":
            seen.append(messages[-1]["content"])
        return await scripted(messages, tools, params, client)

    monkeypatch.setattr(runner, "generate", generate)
    worker = Worker()

    await worker._graph()  # noqa: SLF001 — один проход воркера: вся очередь

    # Каждая запись — свой ран; вторая запись сюжета видит событие первой.
    assert [task.count("### ") for task in seen] == [1, 1, 1]
    assert "Сюжет" not in seen[0] and f"Сюжет: в графе по нему уже есть «{EVENT}»" in seen[1]
    async with SessionLocal() as s:
        items = (await s.execute(select(Item).where(Item.connector_id == world["connector_id"]).order_by(Item.id))).scalars().all()
        runs = (await s.execute(select(AgentRun).where(AgentRun.id.in_([i.agent_run_id for i in items])))).scalars().all()
    assert [i.verdict for i in items] == ["stored", "merged", "skipped"]
    assert len({i.agent_run_id for i in items}) == 3 and all(r.status == "done" for r in runs)
    assert items[0].story_id == items[1].story_id != items[2].story_id

    dump = await graph_store.dump(world["graph"])
    assert {n["name"] for n in dump["nodes"]} == {EVENT, "Иван Петров", "РИА Новости", "ТАСС"}
    assert len(dump["edges"]) == 3
    assert await news.queue_length() == 0


async def test_an_outage_pauses_processing_and_a_good_run_lifts_the_pause(world, monkeypatch):
    calls = {"n": 0}

    async def down(messages, tools, params, client=None):
        calls["n"] += 1
        return {"error": "Provider returned 503: busy"}

    monkeypatch.setattr(runner, "generate", down)
    worker = Worker()
    await worker._graph()  # noqa: SLF001

    # Первая запись упала — разбор встал на паузу, остальные не тронуты.
    assert calls["n"] == 1
    async with SessionLocal() as s:
        control = await graph_settings.control(s)
        attempts = (await s.execute(select(Item.attempts).where(Item.connector_id == world["connector_id"]).order_by(Item.id))).scalars().all()
    assert control["failures"] == 1 and graph_settings.paused_until(control) > datetime.now(timezone.utc)
    assert attempts == [1, 0, 0]

    await worker._graph()  # noqa: SLF001 — на паузе ничего не разбирается
    assert calls["n"] == 1

    async with SessionLocal() as s:
        await graph_settings.set_control(s, paused_until=None)
    monkeypatch.setattr(runner, "generate", scripted)
    await worker._graph()  # noqa: SLF001
    async with SessionLocal() as s:
        control = await graph_settings.control(s)
        verdicts = (await s.execute(select(Item.verdict).where(Item.connector_id == world["connector_id"]).order_by(Item.id))).scalars().all()
        await graph_settings.set_control(s, failures=0, paused_until=None)
    assert control["failures"] == 0 and verdicts == ["stored", "merged", "skipped"]


async def test_a_source_from_code_runs_saves_and_keeps_its_cursor(monkeypatch, tmp_path):
    from eventgraph.connectors import service
    from eventgraph.models import ConnectorRun
    from eventgraph.sources import Source

    async def collect(ctx):
        seen = ctx.state.get("n", 0)
        ctx.state["n"] = seen + 1
        return [{"id": f"p{seen}", "text": f"пост {seen} {ctx.config['tag']}"}]

    name = f"it_src_{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(service, "BY_NAME", {name: Source(name, collect, {"tag": "#тест"})})
    monkeypatch.setattr(settings, "stories_enabled", False)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    await create_schema()
    ids = await service.sync()
    assert set(ids) == {name}
    assert await service.sync() == ids  # повторный старт не плодит строк

    async def run(kind):
        async with SessionLocal() as s:
            row = ConnectorRun(connector_id=ids[name], kind=kind, status="running")
            s.add(row)
            await s.commit()
        await service.execute(row.id)
        async with SessionLocal() as s:
            return await s.get(ConnectorRun, row.id), await s.get(Connector, ids[name])

    try:
        first, connector = await run("manual")
        assert (first.status, first.items_new) == ("ok", 1) and connector.state == {"n": 1}
        trial, connector = await run("test")
        assert trial.preview[0]["text"] == "пост 1 #тест" and connector.state == {"n": 1}
        second, connector = await run("scheduled")
        assert second.items_new == 1 and connector.state == {"n": 2}
        async with SessionLocal() as s:
            texts = (await s.execute(select(Item.text).where(Item.connector_id == ids[name]).order_by(Item.id))).scalars().all()
        assert texts == ["пост 0 #тест", "пост 1 #тест"]
    finally:
        async with SessionLocal() as s:
            await s.execute(delete(Connector).where(Connector.id == ids[name]))
            await s.commit()
        await engine.dispose()
