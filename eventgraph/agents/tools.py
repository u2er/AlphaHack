from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from eventgraph.agents import images as _images
from eventgraph.agents.specs import AgentSpec
from eventgraph.config import settings
from eventgraph.connectors.images import shrink
from eventgraph.graph import upkeep
from eventgraph.graph.merge import graph_merger
from eventgraph.graph.store import GraphConflict, GraphNotFound, graph_store, summary
from eventgraph.graph.upkeep import clip, ntype, parse_day


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters,
        }}


@dataclass
class ToolContext:
    spec: AgentSpec
    graph: str
    # Картинки рана: имя, под которым их видит агент, → путь от каталога данных.
    attachments: dict[str, str] = field(default_factory=dict)


# Каждый ответ инструмента остаётся в разговоре и отправляется заново с каждым
# следующим шагом, так что его размер оплачивается на каждом шаге. Хаб —
# издание, тема — собирает по ребру на событие: у «Интерфакса» было 93 ребра с
# цитатой на каждом, 52 тыс. символов на один просмотр. Поэтому просмотр узла
# показывает несколько рёбер, а поля — только у небольших узлов.
GRAPH_NODE_EDGES = 15
GRAPH_EDGE_META_UP_TO = 12
GRAPH_HIT_TEXT = 300
GRAPH_HIT_MATCH = 200
_SOURCE_FIELDS = ("title", "url", "source_name", "published_at", "authors", "lang", "tags")


def _int(value: Any, fallback: int, low: int = 1, high: int = 100) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return fallback


def _threshold(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(settings.graph_score_threshold if value is None else value)))
    except (TypeError, ValueError):
        return settings.graph_score_threshold


def as_list(value: Any) -> list[str]:
    """Список, строка через запятую или None."""
    if value is None:
        return []
    if isinstance(value, str):
        value = value.replace(";", ",").split(",")
    return [str(v).strip() for v in value if str(v).strip()]


def graph_meta(meta: Any) -> dict[str, Any]:
    """Метаданные графа сохраняют свои JSON-типы: список псевдонимов остаётся
    списком. Пустые значения отбрасываются."""
    return {
        str(k).strip(): v
        for k, v in (meta or {}).items()
        if str(k).strip() and "." not in str(k) and v is not None and v != "" and v != [] and v != {}
    } if isinstance(meta, dict) else {}


def graph_items(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Элементы узла или ребра из простых полей модели.

    `text` — текстовый элемент; каждая запись `sources` — элемент `source`
    (публикация: заголовок, ссылка, издание, дата и цитата как содержимое);
    `refs` — ссылки на уже записанные элементы; `items` — как есть."""
    items: list[dict[str, Any]] = []
    text = spec.get("text").strip() if isinstance(spec.get("text"), str) else ""
    if text:
        items.append({"type": "text", "data": text})
    for source in spec.get("sources") or []:
        if not isinstance(source, dict):
            continue
        data: dict[str, Any] = {"kind": source.get("kind") or "article"}
        if source.get("outlet") and not source.get("source_name"):
            data["source_name"] = source["outlet"]
        for key in (*_SOURCE_FIELDS, "item_uid"):
            if source.get(key):
                data[key] = source[key]
        quote = str(source.get("quote") or source.get("content") or "").strip()
        if quote:
            data["content"] = quote
        if len(data) > 1:
            items.append({"type": "source", "data": data})
    for ref in spec.get("refs") or []:
        if ref:
            items.append({"type": "ref", "id": str(ref)})
    for item in spec.get("items") or []:
        if isinstance(item, dict) and item.get("type"):
            items.append(item)
    return items


def element_out(element: dict[str, Any]) -> dict[str, Any]:
    """Узел или ребро так, как их нужно модели: кто, что и его поля."""
    out = {"id": element.get("id", ""), "kind": element.get("kind", ""), "name": element.get("name", "")}
    if element.get("meta"):
        out["meta"] = element["meta"]
    for key in ("source_id", "target_id", "directed", "updated_at"):
        if element.get(key) not in (None, ""):
            out[key] = element[key]
    return out


def item_brief(item: dict[str, Any], chars: int = 400) -> dict[str, Any]:
    out: dict[str, Any] = {"type": item.get("type", "")}
    text = item.get("text") or ""
    if text:
        out["text"] = " ".join(str(text).split())[:chars]
    for key in ("title", "url", "source_name", "published_at", "item_uid", "key", "missing", "name"):
        if item.get(key):
            out[key] = item[key]
    return out


def subgraph_out(data: dict[str, Any]) -> dict[str, Any]:
    """{nodes, edges} обхода — читаемыми тройками."""
    names = {n.get("id"): n.get("name", "") for n in data.get("nodes") or []}
    out: dict[str, Any] = {
        "nodes": [element_out(n) for n in data.get("nodes") or []],
        "edges": [
            {
                "source": names.get(e.get("source_id"), e.get("source_id", "")),
                "name": e.get("name", ""),
                "target": names.get(e.get("target_id"), e.get("target_id", "")),
                **({"meta": e["meta"]} if e.get("meta") else {}),
            }
            for e in data.get("edges") or []
        ],
    }
    if data.get("truncated"):
        out["truncated"] = True
    return out


def node_out(detail: dict[str, Any], chars: int = 400) -> dict[str, Any]:
    """Узел целиком, но компактно: поля, элементы и рёбра — все с полями у
    небольшого узла, счёт и первые несколько у хаба."""
    out = element_out(detail)
    out["items"] = [item_brief(i, chars) for i in (detail.get("items") or []) + (detail.get("refs") or [])]
    all_edges = detail.get("edges") or []
    with_meta = len(all_edges) <= GRAPH_EDGE_META_UP_TO
    edges = []
    for edge in all_edges[:GRAPH_NODE_EDGES]:
        other = edge.get("other") or {}
        entry = {"name": edge.get("name", ""), "direction": edge.get("direction", ""),
                 "other": other.get("name") or other.get("id", "")}
        if with_meta and edge.get("meta"):
            entry["meta"] = edge["meta"]
        edges.append(entry)
    if edges:
        out["edges"] = edges
    if len(all_edges) > len(edges) or not with_meta:
        counts: dict[str, int] = {}
        for edge in all_edges:
            counts[edge.get("name", "")] = counts.get(edge.get("name", ""), 0) + 1
        out["edges_total"] = len(all_edges)
        out["edges_by_name"] = counts
        out["edges_note"] = (
            "A hub: only a few edges are listed, without their fields. You do not need "
            "the rest to link to this node by its name; graph_neighbors lists them if you really do."
        )
    return out


def _failed(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def system_text(ctx: ToolContext) -> str:
    """Что агенту сказать об инструментах — дописывается к его промпту."""
    spec, graph = ctx.spec, ctx.graph
    text = (
        f"## Graph tools\nThe knowledge graph `{graph}` is available: `graph_node` looks a node up by "
        "its exact name, `graph_search` finds nodes and edges by meaning, and `graph_neighbors` walks the "
        "links around a node. `graph_period` lists the events between two dates with the people, "
        "organisations, places and stories at their centre."
    )
    if spec.write:
        text += (" `graph_upsert` writes nodes and edges; an existing name is updated, not duplicated. Before "
                 "writing a name, look it up — exactly, then by meaning — and reuse the name that is already there.")
    else:
        text += " Apart from that the graph is read-only for you."
    if spec.write and spec.maintenance:
        text += ("\n\nUpkeep tools are on: `graph_duplicates` → `graph_merge`, `graph_not_duplicates` or "
                 "`graph_doubt`; `graph_islands` → link the piece, merge it, delete it or `graph_check` it; "
                 "`graph_prune` — always a dry run first.")
    if ctx.attachments:
        text += ("\n\n`view_image(name)` shows a picture attached to this run. Each picture can be viewed once "
                 "per run and is visible only in that step.")
    return text


def build_tools(ctx: ToolContext) -> list[Tool]:
    tools = _read_tools(ctx)
    if ctx.spec.write:
        tools += _write_tools(ctx)
    if ctx.spec.write and ctx.spec.maintenance:
        tools += _upkeep_tools(ctx)
    if ctx.attachments:
        tools += _image_tools(ctx)
    return tools


# ── чтение ───────────────────────────────────────────────────────────────────


def _read_tools(ctx: ToolContext) -> list[Tool]:
    graph = ctx.graph

    async def graph_search(query, kinds=None, meta=None, expand=0, limit=None, score_threshold=None):
        try:
            data = await graph_store.search(
                graph, str(query or ""), limit=_int(limit, settings.graph_search_limit, high=50),
                score_threshold=_threshold(score_threshold),
                kinds=[k for k in as_list(kinds) if k in ("node", "edge")] or None,
                meta=graph_meta(meta) or None, expand=_int(expand, 0, low=0, high=2),
            )
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)
        results = []
        for r in data.get("results") or []:
            hit = element_out(r.get("element") or {})
            hit.pop("updated_at", None)
            if isinstance(hit.get("meta"), dict):
                hit["meta"] = {k: clip(v, GRAPH_HIT_TEXT) for k, v in hit["meta"].items()}
            hit["score"] = round(float(r.get("score") or 0.0), 3)
            matched = [item_brief(m, GRAPH_HIT_MATCH) for m in r.get("matches") or []]
            if matched:
                hit["matched"] = matched
            results.append(hit)
        out: dict[str, Any] = {"ok": True, "graph": graph, "query": query, "results": results}
        if data.get("subgraph"):
            out["around"] = subgraph_out(data["subgraph"])
        return out

    async def graph_node(name):
        try:
            node = await graph_store.find_node(graph, str(name or ""))
            if node is None:
                return {"ok": True, "found": False, "name": name}
            detail = node_out(await graph_store.detail(node))
        except ValueError:
            return {"ok": True, "found": False, "name": name}
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)
        return {"ok": True, "found": True, "node": detail}

    async def graph_neighbors(name, depth=1, direction="both", edge_names=None):
        try:
            node = await graph_store.resolve_node(graph, name=str(name or ""))
            data = await graph_store.neighbors(
                graph, [node], depth=_int(depth, 1, low=1, high=3),
                direction=direction if direction in ("in", "out", "both") else "both",
                edge_names=as_list(edge_names) or None, limit=200,
            )
        except GraphNotFound:
            return {"ok": True, "found": False, "name": name}
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)
        return {"ok": True, "name": name, **subgraph_out(data)}

    async def graph_period(since, until=None, min_importance=1, topics=None, entity=None, limit=30):
        start, end = parse_day(since), parse_day(until or since)
        if start is None or end is None:
            return {"ok": False, "error": "since and until are dates, YYYY-MM-DD"}
        if end < start:
            start, end = end, start
        if (end - start).days > upkeep.PERIOD_MAX_DAYS:
            return {"ok": False, "error": f"A period is at most {upkeep.PERIOD_MAX_DAYS} days"}
        try:
            dump = await graph_store.dump(graph)
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)
        out = upkeep.period(dump, start, end, _int(min_importance, 1, low=1, high=5), as_list(topics),
                            str(entity or ""), _int(limit, 30, high=80))
        if dump.get("truncated"):
            out["truncated"] = True
        return {"ok": True, "graph": graph, **out}

    return [
        Tool("graph_search", (
            "Search the graph by meaning — names, fields and attached texts. Use it to find whether an event "
            "or an entity is already there under other wording. `meta` filters exactly, e.g. {\"type\": \"event\"}."
        ), {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for"},
                "kinds": {"type": "array", "items": {"type": "string", "enum": ["node", "edge"]}},
                "meta": {"type": "object", "description": "Exact filter on fields: {key: value | [values]}"},
                "expand": {"type": "integer", "description": "0–2: also return the neighbourhood of the hits"},
                "limit": {"type": "integer", "description": "Max hits"},
                "score_threshold": {"type": "number", "description": "Minimum similarity 0..1"},
            },
            "required": ["query"],
        }, graph_search),
        Tool("graph_node", (
            "Exact lookup of a node by name (case and spacing do not matter): its fields, attached items and "
            "its edges — on a hub with many edges only a count and a few of them. `found: false` means no node "
            "has this exact name — search by meaning before creating one. Outlets and topics you link by name "
            "need no lookup."
        ), {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, graph_node),
        Tool("graph_neighbors", "Nodes and edges around a node, up to `depth` hops, as source —name→ target triples.", {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "depth": {"type": "integer", "description": "1–3"},
                "direction": {"type": "string", "enum": ["out", "in", "both"]},
                "edge_names": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name"],
        }, graph_neighbors),
        Tool("graph_period", (
            "Events dated between `since` and `until` (inclusive, YYYY-MM-DD), heaviest first: importance, "
            "then how many outlets covered it — each with topics, places, participants and story links; the "
            "next ones as bare headlines. Also who and where was at the centre (people, orgs, places, `new` = "
            "appeared in the period), story chains and counts by topic and by day."
        ), {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "First day, YYYY-MM-DD"},
                "until": {"type": "string", "description": "Last day, YYYY-MM-DD; default = since"},
                "min_importance": {"type": "integer", "description": "1–5, default 1"},
                "topics": {"type": "array", "items": {"type": "string"}, "description": "Only events about these topics"},
                "entity": {"type": "string",
                           "description": "Only events involving or happening in this node (name or alias)"},
                "limit": {"type": "integer", "description": "Events listed in full, up to 80, default 30"},
            },
            "required": ["since"],
        }, graph_period),
    ]


# ── запись ───────────────────────────────────────────────────────────────────


NODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Unique name of the node; an existing name updates that node."},
        "meta": {"type": "object", "description": "Fields of the node: type, aliases, date, … Merged into the current ones."},
        "text": {"type": "string", "description": "A text item to add: a summary, a one-line description."},
        "sources": {
            "type": "array",
            "description": "Publications to attach: each becomes a source item.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "outlet": {"type": "string", "description": "Who published it"},
                    "published_at": {"type": "string"},
                    "quote": {"type": "string", "description": "The key passage, verbatim"},
                    "kind": {"type": "string", "enum": ["article", "post", "webpage"]},
                    "item_uid": {"type": "string", "description": "Id of the raw record it came from"},
                },
            },
        },
        "refs": {"type": "array", "items": {"type": "string"}, "description": "Ids of stored elements to reference"},
    },
    "required": ["name"],
}


def _write_tools(ctx: ToolContext) -> list[Tool]:
    graph = ctx.graph

    async def graph_upsert(nodes=None, edges=None):
        payload_nodes, payload_edges = [], []
        for spec in nodes or []:
            if not isinstance(spec, dict) or not str(spec.get("name", "")).strip():
                return {"ok": False, "error": "Every node needs a name"}
            payload_nodes.append({"name": str(spec["name"]).strip(), "meta": graph_meta(spec.get("meta")),
                                  "items": graph_items(spec)})
        for spec in edges or []:
            if not isinstance(spec, dict) or not spec.get("name") \
                    or not (spec.get("source") or spec.get("source_id")) \
                    or not (spec.get("target") or spec.get("target_id")):
                return {"ok": False, "error": "Every edge needs a name, a source and a target"}
            edge = {"name": spec["name"], "directed": spec.get("directed", True) is not False,
                    "meta": graph_meta(spec.get("meta")), "items": graph_items(spec)}
            for key in ("source", "source_id", "target", "target_id"):
                if spec.get(key):
                    edge[key] = str(spec[key]).strip()
            payload_edges.append(edge)
        if not payload_nodes and not payload_edges:
            return {"ok": False, "error": "Nothing to write"}
        try:
            data = await graph_store.import_graph(graph, payload_nodes, payload_edges)
        except Exception as exc:  # noqa: BLE001 — запись одной транзакцией: не легло ничего
            return {**_failed(exc), "note": "Nothing of this call was written; fix the input and call again."}
        return {
            "ok": True,
            "graph": graph,
            "nodes": [{"name": n["name"], "id": n["id"], "created": n["created"]} for n in data["nodes"]],
            "edges": [{"name": e["name"], "id": e["id"], "created": e["created"]} for e in data["edges"]],
        }

    async def graph_update(id, kind="node", new_name=None, meta=None, replace_meta=False):  # noqa: A002 — имя параметра видит модель
        if not new_name and meta is None:
            return {"ok": False, "error": "Pass new_name, meta or both"}
        try:
            el = await graph_store.update(graph, "edge" if kind == "edge" else "node", str(id), name=new_name or None,
                                          meta=graph_meta(meta) if meta is not None else None,
                                          replace_meta=bool(replace_meta))
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)
        return {"ok": True, "element": element_out(summary(el))}

    tools = [
        Tool("graph_upsert", (
            "Write nodes and edges in one call — all or nothing. A node whose name exists is updated: fields "
            "merge, items are appended. An edge is identified by source, target, name and direction; its ends "
            "are given by name and created if missing — so look every name up first."
        ), {
            "type": "object",
            "properties": {
                "nodes": {"type": "array", "items": NODE_SCHEMA},
                "edges": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string", "description": "Name of the start node"},
                            "target": {"type": "string", "description": "Name of the end node"},
                            "name": {"type": "string", "description": "Relation: involves, in, about, reported_by, …"},
                            "directed": {"type": "boolean"},
                            "meta": {"type": "object"},
                            "text": {"type": "string"},
                            "sources": NODE_SCHEMA["properties"]["sources"],
                        },
                        "required": ["source", "target", "name"],
                    },
                },
            },
        }, graph_upsert),
        Tool("graph_update", "Rename a node or edge and/or change its fields by id. Fields merge unless replace_meta is true.", {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "kind": {"type": "string", "enum": ["node", "edge"]},
                "new_name": {"type": "string"},
                "meta": {"type": "object"},
                "replace_meta": {"type": "boolean"},
            },
            "required": ["id"],
        }, graph_update),
    ]

    if ctx.spec.delete:
        async def graph_delete(id, kind="node"):  # noqa: A002
            try:
                await graph_store.delete(graph, "edge" if kind == "edge" else "node", str(id))
            except Exception as exc:  # noqa: BLE001
                return _failed(exc)
            return {"ok": True, "deleted": id}

        tools.append(Tool("graph_delete", "Delete a node (with its edges) or an edge by id. Irreversible.", {
            "type": "object",
            "properties": {"id": {"type": "string"}, "kind": {"type": "string", "enum": ["node", "edge"]}},
            "required": ["id"],
        }, graph_delete))
    return tools


# ── обслуживание ─────────────────────────────────────────────────────────────


def _upkeep_tools(ctx: ToolContext) -> list[Tool]:
    graph = ctx.graph

    async def graph_duplicates(limit=20, threshold=0.85):
        limit = _int(limit, 20, high=60)
        try:
            threshold = max(0.5, min(0.99, float(threshold)))
        except (TypeError, ValueError):
            threshold = 0.85
        try:
            data = await graph_merger.candidates(graph, threshold, min(500, limit * 3))
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)
        pairs = []
        for pair in data.get("pairs") or []:
            a, b = pair.get("a") or {}, pair.get("b") or {}
            doubted = as_list((a.get("meta") or {}).get("merge_doubt")) + as_list((b.get("meta") or {}).get("merge_doubt"))
            if a.get("id") in doubted or b.get("id") in doubted:
                continue
            pairs.append({
                "score": round(float(pair.get("score") or 0), 3),
                "brief_score": round(float(pair.get("brief_score") or 0), 3),
                "a": upkeep.upkeep_side(a),
                "b": upkeep.upkeep_side(b),
            })
            if len(pairs) >= limit:
                break
        return {"ok": True, "graph": graph, "candidates_total": data.get("total", len(pairs)), "pairs": pairs}

    async def graph_merge(keep_id, drop_id, name=None):
        try:
            keep = await graph_store.get(graph, str(keep_id), "node")
            drop = await graph_store.get(graph, str(drop_id), "node")
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)
        final = str(name or "").strip() or keep.name
        km, dm = dict(keep.attrs or {}), dict(drop.attrs or {})
        keep_name, drop_name = keep.name, drop.name
        try:
            merged = await graph_merger.merge(graph, keep.id, drop.id, "a")
            patch: dict[str, Any] = {}
            if (km.get("type") or dm.get("type")) == "event":
                detail = await graph_store.detail(merged)
                reported = sum(1 for e in detail.get("edges") or [] if e.get("name") == "reported_by")
                if reported:
                    patch["sources_count"] = reported
                seen = [v for v in (km.get("first_seen"), dm.get("first_seen")) if v]
                if seen:
                    patch["first_seen"] = min(str(v) for v in seen)
                seen = [v for v in (km.get("last_seen"), dm.get("last_seen")) if v]
                if seen:
                    patch["last_seen"] = max(str(v) for v in seen)
            else:
                spellings = [*as_list(km.get("aliases")), *as_list(dm.get("aliases")), keep_name, drop_name]
                aliases, taken = [], {final.casefold()}
                for alias in spellings:
                    if alias and alias.casefold() not in taken:
                        taken.add(alias.casefold())
                        aliases.append(alias)
                if aliases:
                    patch["aliases"] = aliases
            renamed = final if final != keep_name else None
            if patch or renamed:
                await graph_store.update(graph, "node", keep.id, name=renamed, meta=patch or None)
        except GraphConflict as exc:
            return {"ok": True, "kept": keep_name, "dropped": drop_name,
                    "warning": f"Merged, but not renamed: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)
        return {"ok": True, "kept": final, "dropped": drop_name}

    async def graph_not_duplicates(a_id, b_id):
        try:
            await graph_merger.reject(graph, str(a_id), str(b_id))
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)
        return {"ok": True, "rejected": [a_id, b_id]}

    async def graph_doubt(a_id, b_id):
        try:
            node = await graph_store.get(graph, str(a_id), "node")
            doubt = as_list((node.attrs or {}).get("merge_doubt"))
            if str(b_id) not in doubt:
                await graph_store.update(graph, "node", node.id, meta={"merge_doubt": [*doubt, str(b_id)]})
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)
        return {"ok": True, "left": [a_id, b_id]}

    async def graph_islands(max_size=3, limit=20):
        try:
            dump = await graph_store.dump(graph)
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)
        found = upkeep.islands(dump, _int(max_size, 3, high=10))
        return {"ok": True, "graph": graph, "islands_total": len(found), "islands": found[:_int(limit, 20, high=60)]}

    async def graph_check(ids):
        ids = as_list(ids)
        if not ids:
            return {"ok": False, "error": "Pass the ids to mark"}
        today = datetime.date.today().isoformat()
        errors = {}
        for node_id in ids:
            try:
                await graph_store.update(graph, "node", node_id, meta={"checked": today})
            except Exception as exc:  # noqa: BLE001 — один плохой id не валит остальные
                errors[node_id] = str(exc)[:200]
        return {"ok": True, "checked": len(ids) - len(errors), **({"errors": errors} if errors else {})}

    async def graph_prune(dry_run=True, ids=None, older_than_days=30, max_importance=2, max_sources=1,
                          protect_topics=None):
        live = dry_run is False or str(dry_run).lower() == "false"
        if live and not ctx.spec.delete:
            return {"ok": False, "error": "Deleting is disabled for this agent"}
        # Пустой список — законный ответ: ни одно событие не уходит, мусор — уходит.
        if live and ids is None:
            return {"ok": False, "error": "A real prune needs `ids`: the events from the dry run you agree "
                                          "to remove, or [] to remove only the junk"}
        try:
            dump = await graph_store.dump(graph)
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)
        if dump.get("truncated"):
            return {"ok": False, "error": "The graph came back truncated; refusing to prune on a partial view"}
        protect = as_list(protect_topics) if protect_topics is not None else ["Томск"]
        plan = upkeep.prune_plan(
            dump, _int(older_than_days, 30, low=1, high=3650), _int(max_importance, 2, low=1, high=5),
            _int(max_sources, 1, low=0, high=100), protect, only_events=as_list(ids) if live else None,
        )
        names = {n["id"]: n.get("name", "") for n in dump.get("nodes") or []}
        counts = {key: len(value) for key, value in plan.items()}
        if not live:
            return {
                "ok": True,
                "dry_run": True,
                **counts,
                "events": [
                    {"id": n["id"], "name": n.get("name", ""), "importance": (n.get("meta") or {}).get("importance"),
                     "sources_count": (n.get("meta") or {}).get("sources_count")}
                    for n in plan["events"][:150]
                ],
                "orphans": [f"{n.get('name', '')} ({ntype(n)})" for n in plan["orphans"][:150]],
                "empty": [n.get("name", "") for n in plan["empty"][:100]],
                "junk_edges": [
                    f"{names.get(e.get('source_id'), '?')} —{e.get('name')}→ {names.get(e.get('target_id'), '?')}"
                    for e in plan["junk_edges"][:100]
                ],
            }
        deleted, errors = 0, 0
        targets = [("edge", e["id"]) for e in plan["junk_edges"]] + [
            ("node", n["id"]) for key in ("events", "orphans", "empty") for n in plan[key]
        ]
        for kind, element_id in targets:
            try:
                await graph_store.delete(graph, kind, element_id)
                deleted += 1
            except GraphNotFound:
                pass  # ушло вместе с узлом — так и надо
            except Exception:  # noqa: BLE001
                errors += 1
        return {"ok": True, "dry_run": False, **counts, "deleted": deleted, "errors": errors,
                "events_removed": [n.get("name", "") for n in plan["events"]]}

    return [
        Tool("graph_duplicates", (
            "Pairs of nodes whose names are close — candidates for one thing written twice. Pairs already "
            "rejected or left as doubtful are not offered."
        ), {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max pairs, up to 60"},
                "threshold": {"type": "number", "description": "Name similarity 0.5–0.99, default 0.85"},
            },
        }, graph_duplicates),
        Tool("graph_merge", (
            "Merge drop_id into keep_id: its edges, items and fields move over and it is gone. Aliases of an "
            "entity are united; an event's sources_count is recounted. `name` renames the kept node, e.g. to "
            "the full official name."
        ), {
            "type": "object",
            "properties": {
                "keep_id": {"type": "string"},
                "drop_id": {"type": "string"},
                "name": {"type": "string", "description": "New name of the kept node (optional)"},
            },
            "required": ["keep_id", "drop_id"],
        }, graph_merge),
        Tool("graph_not_duplicates", "Record that a pair is two different things; it is never offered again.", {
            "type": "object", "properties": {"a_id": {"type": "string"}, "b_id": {"type": "string"}},
            "required": ["a_id", "b_id"],
        }, graph_not_duplicates),
        Tool("graph_doubt", "Leave a pair for a human: not merged, not rejected, no longer offered to you.", {
            "type": "object", "properties": {"a_id": {"type": "string"}, "b_id": {"type": "string"}},
            "required": ["a_id", "b_id"],
        }, graph_doubt),
        Tool("graph_islands", (
            "Small pieces of the graph linked to nothing but topics and outlets, worst first, each node with "
            "its type, date and summary. Nodes checked recently are skipped."
        ), {
            "type": "object",
            "properties": {
                "max_size": {"type": "integer", "description": "Largest piece to report, 1–10, default 3"},
                "limit": {"type": "integer", "description": "Max pieces, up to 60"},
            },
        }, graph_islands),
        Tool("graph_check", "Mark nodes as checked: right as they are, not offered as islands for a month.", {
            "type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["ids"],
        }, graph_check),
        Tool("graph_prune", (
            "Remove what the graph no longer needs: old minor single-source events outside any story and not "
            "about a protected topic, the entities only they held, junk edges and empty untyped nodes. dry_run "
            "(the default) only lists it. A real prune takes `ids` — the events from the dry-run list you agree "
            "to remove ([] — none); orphans and junk follow on their own."
        ), {
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean"},
                "ids": {"type": "array", "items": {"type": "string"}},
                "older_than_days": {"type": "integer"},
                "max_importance": {"type": "integer"},
                "max_sources": {"type": "integer"},
                "protect_topics": {"type": "array", "items": {"type": "string"}},
            },
        }, graph_prune),
    ]


# ── картинки ─────────────────────────────────────────────────────────────────


def _image_tools(ctx: ToolContext) -> list[Tool]:
    # Что уже показано в этом ране: повторный просмотр той же картинки — то,
    # на чём модель зацикливается (ран 66 Loom сделал 440 шагов).
    viewed: set[str] = set()

    async def view_image(path):
        name = Path(str(path or "")).name
        rel = ctx.attachments.get(name)
        if rel is None:
            return {"error": f"No picture named '{path}' in this run", "pictures": sorted(ctx.attachments)}
        if name in viewed:
            return {"path": name, "error": "Already viewed in this run — it can't be viewed again. "
                                           "Work from what you noted when you looked at it."}
        target = settings.data_dir / rel
        if not target.is_file():
            return {"path": name, "error": "The picture file is gone"}
        # Модель получает картинку уменьшенной: новостной иллюстрации, графику
        # или подписи хватает NEWS_IMAGE_EDGE px, а цена — по пикселям.
        data = await asyncio.to_thread(target.read_bytes)
        data, _ = await asyncio.to_thread(shrink, data, target.suffix.lstrip("."), settings.news_image_edge)
        mime = _images.sniff_mime(data, name)
        if mime is None:
            return {"path": name, "error": "Not a picture the model can see"}
        viewed.add(name)
        image = _images.ToolImage(data=data, mime=mime, name=name)
        return _images.ToolResult({"path": name, "bytes": len(data)}, [image])

    return [Tool("view_image", (
        "Look at a picture attached to this run, by its name. The picture is visible only in this step and "
        "each one can be viewed once per run — note what you need from it right away."
    ), {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Picture name, e.g. 01K…-0.jpg"}},
        "required": ["path"],
    }, view_image)]
