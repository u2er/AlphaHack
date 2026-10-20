from __future__ import annotations

import json
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from eventgraph.config import settings
from eventgraph.deps import require_auth
from eventgraph.graph import upkeep
from eventgraph.graph.store import GraphNotFound, graph_store

router = APIRouter(prefix="/graphs", tags=["graphs"], dependencies=[Depends(require_auth)])


class SearchIn(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=100)
    score_threshold: float = Field(default_factory=lambda: settings.graph_score_threshold, ge=0, le=1)
    kinds: list[str] | None = None
    meta: dict[str, Any] | None = None
    expand: int = Field(default=0, ge=0, le=3)


class NeighborsIn(BaseModel):
    name: str | None = None
    node_id: str | None = None
    depth: int = Field(default=1, ge=1, le=3)
    direction: str = "both"
    edge_names: list[str] | None = None
    edge_meta: dict[str, Any] | None = None
    node_meta: dict[str, Any] | None = None
    limit: int = Field(default=500, ge=1, le=5000)


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


@router.get("")
async def list_graphs() -> dict[str, Any]:
    return {"graphs": await graph_store.list_graphs()}


@router.get("/{graph}")
async def get_graph(graph: str, limit: int = Query(default=50_000, ge=1, le=200_000)) -> dict[str, Any]:
    """Граф целиком без элементов: узлы и рёбра с метаданными."""
    return await graph_store.dump(graph, limit)


@router.get("/{graph}/nodes")
async def nodes(
    graph: str,
    name: str | None = Query(default=None, description="Точное имя (без учёта регистра) — узел целиком"),
    meta: str | None = Query(default=None, description='Фильтр по метаданным, JSON: {"type": "person"}'),
    limit: int = Query(default=1000, ge=1, le=50_000),
) -> dict[str, Any]:
    if name:
        try:
            node = await graph_store.resolve_node(graph, name=name)
        except GraphNotFound as exc:
            raise _not_found(exc) from exc
        return {"node": await graph_store.detail(node)}
    try:
        flt = json.loads(meta) if meta else None
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "meta — JSON-объект") from exc
    from eventgraph.graph.store import summary

    return {"nodes": [summary(n) for n in await graph_store.list_nodes(graph, flt, limit=limit)]}


@router.post("/{graph}/search")
async def search(graph: str, body: SearchIn) -> dict[str, Any]:
    """Смысловой поиск: по меткам, сводкам, длинным метаданным и элементам."""
    return await graph_store.search(graph, body.query, body.limit, body.score_threshold, body.kinds, body.meta,
                                    body.expand)


@router.post("/{graph}/neighbors")
async def neighbors(graph: str, body: NeighborsIn) -> dict[str, Any]:
    try:
        node = await graph_store.resolve_node(graph, body.node_id, body.name)
        return await graph_store.neighbors(graph, [node], body.depth, body.direction, body.edge_names,
                                           body.edge_meta, body.node_meta, body.limit)
    except GraphNotFound as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("/{graph}/period")
async def period(
    graph: str,
    since: date,
    until: date | None = None,
    min_importance: int = Query(default=1, ge=1, le=5),
    topics: list[str] = Query(default=[]),
    entity: str = "",
    limit: int = Query(default=30, ge=1, le=200),
) -> dict[str, Any]:
    """События периода по весу и кто был в их центре — по выгрузке графа, без модели."""
    until = until or since
    if until < since:
        since, until = until, since
    if (until - since).days > upkeep.PERIOD_MAX_DAYS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Период — не длиннее {upkeep.PERIOD_MAX_DAYS} дней")
    dump = await graph_store.dump(graph)
    return upkeep.period(dump, since, until, min_importance, topics, entity, limit)


@router.get("/{graph}/{kind}/{element_id}")
async def element(graph: str, kind: str, element_id: str) -> dict[str, Any]:
    """Узел (`nodes`) или ребро (`edges`) целиком: метаданные, элементы, связи."""
    if kind not in ("nodes", "edges"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "kind — nodes или edges")
    try:
        found = await graph_store.get(graph, element_id, "node" if kind == "nodes" else "edge")
    except GraphNotFound as exc:
        raise _not_found(exc) from exc
    return {kind[:-1]: await graph_store.detail(found)}
