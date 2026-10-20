from __future__ import annotations

from typing import Any, Iterable

import numpy as np
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from eventgraph.db import SessionLocal
from eventgraph.graph.store import GraphElement, GraphItem, GraphNotFound, GraphStore, graph_store, hex_id, summary
from eventgraph.models import GraphMergeDecision

MATRIX_BLOCK = 1024  # строк матрицы сходства за раз
BRIEF_LOG_LIMIT = 1000  # сколько сводки узла уходит в журнал


def _unit(vectors: list[Any]) -> np.ndarray:
    m = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


class GraphMerger:
    def __init__(self, store: GraphStore):
        self.store = store

    # ── чтение ───────────────────────────────────────────────────────────────

    @staticmethod
    async def _degrees(s: AsyncSession, graph: str) -> dict[str, int]:
        rows = (await s.execute(
            select(GraphElement.source_id, GraphElement.target_id)
            .where(GraphElement.graph == graph, GraphElement.kind == "edge")
        )).all()
        degree: dict[str, int] = {}
        for a, b in rows:
            for end in (a, b):
                if end:
                    degree[end] = degree.get(end, 0) + 1
        return degree

    @staticmethod
    async def rejected(s: AsyncSession, graph: str) -> set[frozenset]:
        rows = (await s.execute(
            select(GraphMergeDecision.a_id, GraphMergeDecision.b_id)
            .where(GraphMergeDecision.graph == graph, GraphMergeDecision.decision == "reject")
        )).all()
        return {frozenset(pair) for pair in rows}

    @staticmethod
    def _describe(node: GraphElement, degree: dict[str, int]) -> dict[str, Any]:
        return {**summary(node), "degree": degree.get(node.id, 0), "brief": (node.brief or "")[:400]}

    async def candidates(self, graph: str, threshold: float = 0.85, limit: int = 50,
                         exclude: Iterable[Iterable[str]] | None = None) -> dict[str, Any]:
        """Пары узлов с косинусным сходством имён не ниже порога, по убыванию."""
        async with SessionLocal() as s:
            nodes = list((await s.execute(
                select(GraphElement).where(
                    GraphElement.graph == graph, GraphElement.kind == "node", GraphElement.label_vec.is_not(None)
                ).order_by(GraphElement.created_at, GraphElement.id)
            )).scalars().all())
            if not nodes:
                return {"pairs": [], "total": 0, "nodes": 0}
            skip = await self.rejected(s, graph)
            skip.update(frozenset(hex_id(i) for i in pair) for pair in (exclude or []))
            names = _unit([n.label_vec for n in nodes])
            briefs = _unit([n.brief_vec if n.brief_vec is not None else n.label_vec for n in nodes])

            found: list[tuple[float, int, int]] = []
            for start in range(0, len(nodes), MATRIX_BLOCK):
                sims = names[start:start + MATRIX_BLOCK] @ names.T
                rows, cols = np.nonzero(sims >= threshold)
                for r, c in zip(rows.tolist(), cols.tolist()):
                    i = start + r
                    if c <= i or frozenset((nodes[i].id, nodes[c].id)) in skip:
                        continue
                    found.append((float(sims[r, c]), i, c))
            found.sort(reverse=True)
            degree = await self._degrees(s, graph) if found else {}
        pairs = [
            {
                "score": score,
                "brief_score": float(briefs[i] @ briefs[j]),
                "a": self._describe(nodes[i], degree),
                "b": self._describe(nodes[j], degree),
            }
            for score, i, j in found[:limit]
        ]
        return {"pairs": pairs, "total": len(found), "nodes": len(nodes)}

    async def _snapshot(self, s: AsyncSession, graph: str, a: GraphElement, b: GraphElement) -> dict[str, Any]:
        """Признаки пары для журнала — состояние до решения."""

        def cos(va: Any, vb: Any) -> float | None:
            if va is None or vb is None:
                return None
            ua, ub = _unit([va, vb])
            return round(float(ua @ ub), 6)

        sides = {}
        for side, node in (("a", a), ("b", b)):
            items = await GraphStore._items(s, node.id)
            sides[side] = {
                "id": node.id,
                "name": node.name,
                "type": (node.attrs or {}).get("type", ""),
                "meta": node.attrs or {},
                "degree": len(await GraphStore._incident(s, graph, [node.id])),
                "items": len([i for i in items if i.type != "attr"]) + len(node.refs or []),
                "brief": (node.brief or "")[:BRIEF_LOG_LIMIT],
            }
        return {
            "graph": graph,
            "a_id": a.id,
            "b_id": b.id,
            "name_score": cos(a.label_vec, b.label_vec),
            "brief_score": cos(a.brief_vec, b.brief_vec),
            "snapshot": sides,
        }

    @staticmethod
    async def _pair(s: AsyncSession, graph: str, a_id: str, b_id: str) -> tuple[GraphElement, GraphElement]:
        a = await GraphStore._get(s, graph, a_id, "node")
        b = await GraphStore._get(s, graph, b_id, "node")
        if a.id == b.id:
            raise ValueError("cannot merge a node with itself")
        return a, b

    # ── решения ──────────────────────────────────────────────────────────────

    async def reject(self, graph: str, a_id: str, b_id: str) -> None:
        async with SessionLocal() as s:
            a, b = await self._pair(s, graph, a_id, b_id)
            s.add(GraphMergeDecision(decision="reject", **await self._snapshot(s, graph, a, b)))
            await s.commit()

    async def merge(self, graph: str, a_id: str, b_id: str, keep: str = "a") -> GraphElement:
        """Сливает пару: остаётся узел `keep` ("a" | "b"), второй переносит в него
        элементы, ссылки, метаданные (при совпадении ключей побеждает
        оставшийся) и рёбра. Рёбра между самими дублями удаляются; ребро,
        совпавшее с уже существующим, сливается с ним."""
        if keep not in ("a", "b"):
            raise ValueError("keep must be 'a' or 'b'")
        async with self.store.lock(graph), SessionLocal() as s:
            a, b = await self._pair(s, graph, a_id, b_id)
            row = await self._snapshot(s, graph, a, b)
            kept, dropped = (a, b) if keep == "a" else (b, a)
            result = await self._merge(s, graph, kept, dropped)
            s.add(GraphMergeDecision(decision="merge", kept=keep, **row))
            await s.commit()
            return result

    async def _absorb(self, s: AsyncSession, target: GraphElement, source: GraphElement,
                      names: dict[str, str] | None = None) -> None:
        """Переносит в target элементы, ссылки и метаданные source; source удаляется."""
        await s.execute(
            update(GraphItem).where(GraphItem.element_id == source.id, GraphItem.type != "attr")
            .values(element_id=target.id)
        )
        refs = [r for r in [*(target.refs or []), *(source.refs or [])] if r not in (target.id, source.id)]
        target.refs = list(dict.fromkeys(refs))
        merged_attrs = {**(source.attrs or {}), **(target.attrs or {})}
        attrs_changed = merged_attrs != (target.attrs or {})
        target.attrs = merged_attrs
        await s.delete(source)
        await s.flush()
        if attrs_changed:
            await self.store._write_attrs(s, target)
        await self.store._refresh(s, target, names)

    async def _merge(self, s: AsyncSession, graph: str, keep: GraphElement, drop: GraphElement) -> GraphElement:
        store = self.store
        for edge in await store._incident(s, graph, [drop.id]):
            src = keep.id if edge.source_id == drop.id else edge.source_id
            dst = keep.id if edge.target_id == drop.id else edge.target_id
            was_loop = edge.source_id == edge.target_id
            if src == dst and not was_loop:
                # Связь между самими дублями после слияния смысла не имеет.
                await s.delete(edge)
                await s.flush()
                continue
            twin = await store._find_edge(s, graph, edge.name, src, dst, edge.directed)
            if twin is not None and twin.id != edge.id:
                await self._absorb(s, twin, edge, names={keep.id: keep.name})
                continue
            edge.source_id, edge.target_id = src, dst
            await s.flush()
            await store._refresh(s, edge, names={keep.id: keep.name})

        await self._absorb(s, keep, drop)

        # Ссылки на удалённый узел ведут теперь на оставшийся.
        referrers = (await s.execute(
            select(GraphElement).where(GraphElement.graph == graph, GraphElement.refs.contains([drop.id]))
        )).scalars().all()
        for referrer in referrers:
            refs = list(dict.fromkeys(keep.id if r == drop.id else r for r in referrer.refs or []))
            referrer.refs = [r for r in refs if r != referrer.id]
            await store._refresh(s, referrer)
        await s.flush()
        return keep


graph_merger = GraphMerger(graph_store)

__all__ = ["GraphMerger", "GraphNotFound", "graph_merger"]
