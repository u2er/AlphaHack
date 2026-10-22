"""Базовый граф: при пустом графе воркер загружает снимок новостного графа.

Снимок — выгрузка `GET /graphs/{graph}` (узлы и рёбра с метаданными,
eventgraph/seed/news-graph.json.gz: 2270 узлов, 6265 рёбер на 21.10.2026),
чтобы лента и вкладка «Граф» не начинались с пустоты, а аналитик сверял записи
с прошлыми событиями с первого дня. Загрузка идёт пачками, в фоне: каждый
элемент получает векторы, так что на CPU это минуты, а новости тем временем
разбираются. Рёбра ищут концы по именам — id снимка в базе не сохраняются.

    python -m eventgraph.seed [путь.json[.gz]] [--force]   — загрузить вручную;
    --force — и в непустой граф: узлы и рёбра находятся по именам, повтор не дублирует
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from eventgraph.config import settings
from eventgraph.db import SessionLocal
from eventgraph.graph.store import graph_store
from eventgraph.models import GraphElement

logger = logging.getLogger(__name__)

BUILTIN = Path(__file__).resolve().parent / "seed" / "news-graph.json.gz"
BATCH = 100


def load(path: Path) -> dict[str, Any]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as file:
        return json.load(file)


def payload(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Узлы и рёбра в форме import_graph: рёбра — с концами по именам узлов."""
    names = {n["id"]: n["name"] for n in snapshot.get("nodes") or [] if n.get("name")}
    nodes = [{"name": n["name"], "meta": n.get("meta") or {}} for n in snapshot.get("nodes") or [] if n.get("name")]
    edges = [{"name": e["name"], "source": names[e["source_id"]], "target": names[e["target_id"]],
              "directed": e.get("directed", True), "meta": e.get("meta") or {}}
             for e in snapshot.get("edges") or []
             if e.get("name") and e.get("source_id") in names and e.get("target_id") in names]
    return nodes, edges


async def node_count(graph: str) -> int:
    async with SessionLocal() as session:
        return int(await session.scalar(
            select(func.count()).select_from(GraphElement).where(GraphElement.graph == graph, GraphElement.kind == "node")
        ) or 0)


def source() -> Path | None:
    """GRAPH_SEED: builtin (по умолчанию), путь к своему снимку или off."""
    value = (settings.graph_seed or "").strip()
    if value.lower() in ("", "off", "none", "false", "0"):
        return None
    return BUILTIN if value.lower() == "builtin" else Path(value)


async def seed_if_empty(graph: str | None = None, path: Path | None = None, force: bool = False) -> int:
    """Загрузить снимок, если в графе нет ни одного узла (или force). Число записанных элементов."""
    graph = graph or settings.graph_name
    path = path or source()
    if path is None or not path.is_file():
        return 0
    if not force and await node_count(graph):
        return 0
    nodes, edges = payload(load(path))
    logger.info("граф %s пуст — загружаю базовый граф %s: узлов %s, рёбер %s", graph, path.name, len(nodes), len(edges))
    done = 0
    for kind, entries in (("nodes", nodes), ("edges", edges)):
        for start in range(0, len(entries), BATCH):
            batch = entries[start:start + BATCH]
            try:
                await graph_store.import_graph(graph, batch if kind == "nodes" else [], batch if kind == "edges" else [])
            except Exception as exc:  # noqa: BLE001 — пачка с битым элементом не останавливает загрузку
                logger.warning("базовый граф: пачка %s %s–%s не записана: %s", kind, start, start + len(batch), exc)
                continue
            done += len(batch)
            if (start // BATCH) % 10 == 0:
                logger.info("базовый граф: %s %s/%s", kind, min(start + BATCH, len(entries)), len(entries))
    logger.info("базовый граф загружен: %s элементов", done)
    return done


async def main() -> None:
    from eventgraph.db import create_schema
    from eventgraph.logging_config import setup_logging

    setup_logging()
    await create_schema()
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv[1:]
    path = Path(args[0]) if args else BUILTIN
    graph = settings.graph_name
    if not force and await node_count(graph):
        print(f"граф {graph} не пуст — снимок не загружен (--force — загрузить поверх)")
        return
    print(await seed_if_empty(graph, path, force=force))


if __name__ == "__main__":
    asyncio.run(main())
