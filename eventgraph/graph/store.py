from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import String, and_, delete, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from eventgraph.db import SessionLocal
from eventgraph.embed import embed, embed_one
from eventgraph.models import GraphElement, GraphItem

MAX_NAME_LENGTH = 300
MAX_GRAPH_NAME_LENGTH = 100
BRIEF_TEXT_LIMIT = 8000  # сколько текста уходит в вектор brief
MAX_DEPTH = 3
# Строковые метаданные не короче этого получают свой вектор (элемент attr).
ATTR_TEXT_MIN = 60
ITEM_TYPES = ("text", "source", "meta", "ref")
SOURCE_KEYS = ("kind", "title", "url", "source_name", "published_at", "authors", "item_uid")


class GraphNotFound(LookupError):
    """Граф, узел, ребро или элемент не найден."""


class GraphConflict(Exception):
    """Имя уже занято другим узлом (или ребром с теми же концами)."""


# ─────────────────────────────────────────────
# Вспомогательные
# ─────────────────────────────────────────────


def new_id() -> str:
    return uuid.uuid4().hex


def hex_id(value: Any) -> str:
    try:
        return uuid.UUID(str(value)).hex
    except ValueError:
        return str(value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clean_name(value: Any, what: str = "name") -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise ValueError(f"{what} must not be empty")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"{what} is too long (max {MAX_NAME_LENGTH})")
    return name


def name_key(name: str) -> str:
    """Ключ уникальности имени: без учёта регистра и лишних пробелов."""
    return clean_name(name).casefold()


def clean_graph(graph: Any) -> str:
    name = clean_name(graph, "graph name")
    if len(name) > MAX_GRAPH_NAME_LENGTH or "/" in name:
        raise ValueError(f"graph name must be at most {MAX_GRAPH_NAME_LENGTH} chars and contain no '/'")
    return name


def clean_meta(meta: Any) -> dict[str, Any]:
    if meta is None:
        return {}
    if not isinstance(meta, dict):
        raise ValueError("meta must be an object")
    for key in meta:
        if not isinstance(key, str) or not key.strip() or "." in key:
            raise ValueError(f"invalid meta key '{key}': must be a non-empty string without '.'")
    return dict(meta)


def match_meta(attrs: dict[str, Any], flt: dict[str, Any] | None) -> bool:
    """Метаданные под фильтром {ключ: значение | [значения]}: список в фильтре —
    «любое из», список в значении — совпадение хотя бы одного элемента."""
    for key, expected in (flt or {}).items():
        wanted = expected if isinstance(expected, list) else [expected]
        actual = (attrs or {}).get(key)
        have = actual if isinstance(actual, list) else [actual]
        if not any(h in wanted for h in have):
            return False
    return True


def _meta_conditions(flt: dict[str, Any] | None) -> list[Any]:
    """Тот же фильтр — условиями SQL: значение равно или входит в список."""
    conditions = []
    for key, value in clean_meta(flt).items():
        values = value if isinstance(value, list) else [value]
        if not values:
            continue
        options = []
        for v in values:
            options.append(GraphElement.attrs.contains({key: v}))
            options.append(GraphElement.attrs.contains({key: [v]}))
        conditions.append(or_(*options))
    return conditions


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def summary(el: GraphElement) -> dict[str, Any]:
    """Узел или ребро без элементов — для списков, обхода и выгрузки."""
    data: dict[str, Any] = {
        "id": el.id,
        "kind": el.kind,
        "graph": el.graph,
        "name": el.name,
        "meta": el.attrs or {},
        "created_at": _iso(el.created_at),
        "updated_at": _iso(el.updated_at),
    }
    if el.kind == "edge":
        data.update(source_id=el.source_id, target_id=el.target_id, directed=el.directed)
    return data


def item_out(item: GraphItem) -> dict[str, Any]:
    data = item.data or {}
    out: dict[str, Any] = {"id": item.id, "type": item.type, "text": item.content}
    if item.type == "source":
        out.update({k: data[k] for k in SOURCE_KEYS if data.get(k)})
    elif item.type == "attr":
        out["key"] = data.get("key", "")
    return out


# ─────────────────────────────────────────────
# Элементы узлов и рёбер
# ─────────────────────────────────────────────


def validate_items(items: list[dict[str, Any]]) -> None:
    """Проверяет форму элементов до дорогой обработки."""
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{index}]: expected an object")
        item_type = item.get("type")
        if item_type not in ITEM_TYPES:
            raise ValueError(f"items[{index}]: unknown type '{item_type}', expected one of {', '.join(ITEM_TYPES)}")
        if item_type == "ref":
            if not item.get("id"):
                raise ValueError(f"items[{index}]: ref needs an 'id'")
        elif item.get("data") in (None, "", {}, []):
            raise ValueError(f"items[{index}]: '{item_type}' needs 'data'")
        elif item_type == "text":
            if not isinstance(item["data"], str):
                raise ValueError(f"items[{index}]: 'text' data must be a string")
        elif not isinstance(item["data"], dict):
            raise ValueError(f"items[{index}]: '{item_type}' data must be an object")


def item_content(item_type: str, data: Any) -> str:
    """Текст элемента — то, что эмбеддится и показывается."""
    if item_type == "text":
        return str(data).strip()
    if item_type == "source":
        head = " · ".join(str(data[k]) for k in ("title", "source_name", "published_at") if data.get(k))
        return "\n".join(p for p in (head, str(data.get("content") or "").strip()) if p)
    return "; ".join(f"{k}: {v}" for k, v in (data or {}).items())


async def prepare_items(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Проверка и векторы элементов — до блокировки графа: это самая долгая часть."""
    items = list(items or [])
    validate_items(items)
    prepared: list[dict[str, Any]] = []
    for item in items:
        if item["type"] == "ref":
            prepared.append({"type": "ref", "id": hex_id(item["id"])})
        else:
            prepared.append({"type": item["type"], "data": item["data"], "content": item_content(item["type"], item["data"])})
    texts = [p["content"] for p in prepared if p["type"] != "ref"]
    if texts:
        vectors = iter(await embed(texts))
        for p in prepared:
            if p["type"] != "ref":
                p["vec"] = next(vectors).tolist()
    return prepared


# ─────────────────────────────────────────────
# Хранилище
# ─────────────────────────────────────────────


class GraphStore:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, graph: str) -> asyncio.Lock:
        """Одна запись в граф за раз."""
        lock = self._locks.get(graph)
        if lock is None:
            lock = self._locks[graph] = asyncio.Lock()
        return lock

    # ── чтение ───────────────────────────────────────────────────────────────

    @staticmethod
    async def _find_node(s: AsyncSession, graph: str, name: str) -> GraphElement | None:
        return await s.scalar(
            select(GraphElement).where(
                GraphElement.graph == graph, GraphElement.kind == "node", GraphElement.key == name_key(name)
            ).limit(1)
        )

    @staticmethod
    async def _get(s: AsyncSession, graph: str, element_id: str, kind: str | None = None) -> GraphElement:
        el = await s.get(GraphElement, hex_id(element_id))
        if el is None or el.graph != graph or (kind and el.kind != kind):
            raise GraphNotFound(f"{kind or 'element'} '{element_id}' not found in graph '{graph}'")
        return el

    @staticmethod
    async def _get_many(s: AsyncSession, graph: str, ids: Iterable[str]) -> dict[str, GraphElement]:
        wanted = list({hex_id(i) for i in ids if i})
        if not wanted:
            return {}
        rows = (await s.execute(
            select(GraphElement).where(GraphElement.id.in_(wanted), GraphElement.graph == graph)
        )).scalars().all()
        return {el.id: el for el in rows}

    @staticmethod
    async def _find_edge(s: AsyncSession, graph: str, name: str, source_id: str, target_id: str,
                         directed: bool) -> GraphElement | None:
        """Ребро однозначно задано четвёркой (откуда, куда, имя, направленность)."""
        pairs = [(source_id, target_id)]
        if not directed and source_id != target_id:
            pairs.append((target_id, source_id))
        return await s.scalar(
            select(GraphElement).where(
                GraphElement.graph == graph,
                GraphElement.kind == "edge",
                GraphElement.key == name_key(name),
                GraphElement.directed.is_(directed),
                or_(*[and_(GraphElement.source_id == a, GraphElement.target_id == b) for a, b in pairs]),
            ).limit(1)
        )

    @staticmethod
    async def _incident(s: AsyncSession, graph: str, node_ids: list[str]) -> list[GraphElement]:
        if not node_ids:
            return []
        return list((await s.execute(
            select(GraphElement).where(
                GraphElement.graph == graph,
                GraphElement.kind == "edge",
                or_(GraphElement.source_id.in_(node_ids), GraphElement.target_id.in_(node_ids)),
            ).order_by(GraphElement.created_at)
        )).scalars().all())

    @staticmethod
    async def _items(s: AsyncSession, element_id: str) -> list[GraphItem]:
        return list((await s.execute(
            select(GraphItem).where(GraphItem.element_id == element_id).order_by(GraphItem.created_at, GraphItem.id)
        )).scalars().all())

    async def get(self, graph: str, element_id: str, kind: str | None = None) -> GraphElement:
        async with SessionLocal() as s:
            return await self._get(s, graph, element_id, kind)

    async def find_node(self, graph: str, name: str) -> GraphElement | None:
        async with SessionLocal() as s:
            return await self._find_node(s, graph, name)

    async def resolve_node(self, graph: str, node_id: str | None = None, name: str | None = None) -> GraphElement:
        """Узел по id или по имени; ошибка, если не задано ни то ни другое."""
        if node_id:
            return await self.get(graph, node_id, "node")
        if name:
            node = await self.find_node(graph, name)
            if node is None:
                raise GraphNotFound(f"node '{name}' not found in graph '{graph}'")
            return node
        raise ValueError("pass a node name or id")

    # ── запись ───────────────────────────────────────────────────────────────

    async def _label(self, s: AsyncSession, el: GraphElement, names: dict[str, str] | None = None) -> str:
        if el.kind == "node":
            return el.name
        names = dict(names or {})
        missing = [i for i in (el.source_id, el.target_id) if i and i not in names]
        if missing:
            names.update({i: e.name for i, e in (await self._get_many(s, el.graph, missing)).items()})
        arrow = "→" if el.directed else "—"
        return f"{names.get(el.source_id, '?')} — {el.name} {arrow} {names.get(el.target_id, '?')}"

    async def _refresh(self, s: AsyncSession, el: GraphElement, names: dict[str, str] | None = None) -> None:
        """Пересчитывает сводку, метку и векторы элемента."""
        items = await self._items(s, el.id)
        lines: list[str] = []
        if el.attrs:
            lines.append("; ".join(f"{k}: {v}" for k, v in el.attrs.items()))
        lines += [i.content for i in items if i.type != "attr"]
        if el.refs:
            referenced = await self._get_many(s, el.graph, el.refs)
            lines += [referenced[r].brief or referenced[r].label for r in el.refs if r in referenced]
        el.brief = "\n".join(line for line in lines if line)
        el.label = await self._label(s, el, names)
        el.updated_at = _now()
        label_vec, brief_vec = await embed([el.label, f"{el.label}\n{el.brief}"[:BRIEF_TEXT_LIMIT]])
        el.label_vec, el.brief_vec = label_vec.tolist(), brief_vec.tolist()

    async def _write_attrs(self, s: AsyncSession, el: GraphElement) -> None:
        """Заменяет векторы длинных метаданных элемента актуальными."""
        await s.execute(delete(GraphItem).where(GraphItem.element_id == el.id, GraphItem.type == "attr"))
        long = [(k, v) for k, v in (el.attrs or {}).items()
                if isinstance(v, str) and len(v) >= ATTR_TEXT_MIN and not v.startswith(("http://", "https://"))]
        if not long:
            return
        vectors = await embed([f"{k}: {v}" for k, v in long])
        for (key, value), vec in zip(long, vectors):
            s.add(GraphItem(id=new_id(), element_id=el.id, type="attr", data={"key": key},
                            content=f"{key}: {value}", vec=vec.tolist(), created_at=_now()))

    async def _check_refs(self, s: AsyncSession, graph: str, prepared: list[dict[str, Any]]) -> None:
        ids = [p["id"] for p in prepared if p["type"] == "ref"]
        if not ids:
            return
        found = await self._get_many(s, graph, ids)
        missing = [i for i in ids if i not in found]
        if missing:
            raise GraphNotFound(f"referenced elements not found in graph '{graph}': {', '.join(missing)}")

    async def _apply(
        self,
        s: AsyncSession,
        el: GraphElement,
        prepared: list[dict[str, Any]],
        meta: dict[str, Any] | None = None,
        replace_meta: bool = False,
        created: bool = False,
        names: dict[str, str] | None = None,
    ) -> GraphElement:
        """Дописывает элементы и метаданные и пересчитывает элемент графа."""
        contents = [p for p in prepared if p["type"] != "ref"]
        new_refs = [
            ref for ref in dict.fromkeys(p["id"] for p in prepared if p["type"] == "ref")
            if ref not in (el.refs or []) and ref != el.id
        ]
        attrs = dict(el.attrs or {})
        if meta is not None:
            attrs = dict(meta) if replace_meta else {**attrs, **meta}
        attrs_changed = attrs != (el.attrs or {})
        if not (created or contents or new_refs or attrs_changed):
            return el

        # JSONB меняется целиком — иначе SQLAlchemy правки не заметит.
        el.refs = [*(el.refs or []), *new_refs]
        el.attrs = attrs
        await s.flush()
        for p in contents:
            s.add(GraphItem(id=new_id(), element_id=el.id, type=p["type"], data=p["data"] if isinstance(p["data"], dict)
                            else {"text": p["data"]}, content=p["content"], vec=p.get("vec"), created_at=_now()))
        if attrs_changed or created:
            await self._write_attrs(s, el)
        await s.flush()
        await self._refresh(s, el, names)
        return el

    async def _upsert_node(self, s: AsyncSession, graph: str, name: str, prepared: list[dict[str, Any]],
                           meta: dict[str, Any] | None = None, replace_meta: bool = False) -> tuple[GraphElement, bool]:
        el = await self._find_node(s, graph, name)
        created = el is None
        if created:
            el = GraphElement(id=new_id(), graph=graph, kind="node", name=name, key=name_key(name), attrs={},
                              refs=[], directed=True, created_at=_now(), updated_at=_now())
            s.add(el)
        return await self._apply(s, el, prepared, meta, replace_meta, created), created

    async def _upsert_edge(
        self,
        s: AsyncSession,
        graph: str,
        name: str,
        source: str | None,
        source_id: str | None,
        target: str | None,
        target_id: str | None,
        directed: bool,
        prepared: list[dict[str, Any]],
        meta: dict[str, Any] | None = None,
        replace_meta: bool = False,
    ) -> tuple[GraphElement, bool]:
        async def endpoint(node_id: str | None, node_name: str | None, role: str) -> GraphElement:
            if node_id:
                return await self._get(s, graph, node_id, "node")
            if node_name:
                node_name = clean_name(node_name, f"{role} name")
                found = await self._find_node(s, graph, node_name)
                if found is None:
                    found, _ = await self._upsert_node(s, graph, node_name, [])
                return found
            raise ValueError(f"edge {role} is required: pass '{role}' (name) or '{role}_id'")

        src = await endpoint(source_id, source, "source")
        dst = await endpoint(target_id, target, "target")
        el = await self._find_edge(s, graph, name, src.id, dst.id, directed)
        created = el is None
        if created:
            el = GraphElement(id=new_id(), graph=graph, kind="edge", name=name, key=name_key(name), attrs={},
                              refs=[], source_id=src.id, target_id=dst.id, directed=directed,
                              created_at=_now(), updated_at=_now())
            s.add(el)
        el = await self._apply(s, el, prepared, meta, replace_meta, created, names={src.id: src.name, dst.id: dst.name})
        return el, created

    async def upsert_node(self, graph: str, name: str, items: list[dict[str, Any]] | None = None,
                          meta: dict[str, Any] | None = None, replace_meta: bool = False) -> tuple[GraphElement, bool]:
        """Создаёт узел или дописывает в существующий с тем же именем."""
        graph, name = clean_graph(graph), clean_name(name)
        meta = clean_meta(meta) if meta is not None else None
        prepared = await prepare_items(items)
        async with self.lock(graph), SessionLocal() as s:
            await self._check_refs(s, graph, prepared)
            result = await self._upsert_node(s, graph, name, prepared, meta, replace_meta)
            await s.commit()
        return result

    async def upsert_edge(
        self,
        graph: str,
        name: str,
        source: str | None = None,
        source_id: str | None = None,
        target: str | None = None,
        target_id: str | None = None,
        directed: bool = True,
        items: list[dict[str, Any]] | None = None,
        meta: dict[str, Any] | None = None,
        replace_meta: bool = False,
    ) -> tuple[GraphElement, bool]:
        """Создаёт ребро или дописывает в существующее с той же четвёркой."""
        graph, name = clean_graph(graph), clean_name(name)
        meta = clean_meta(meta) if meta is not None else None
        prepared = await prepare_items(items)
        async with self.lock(graph), SessionLocal() as s:
            await self._check_refs(s, graph, prepared)
            result = await self._upsert_edge(s, graph, name, source, source_id, target, target_id, bool(directed),
                                             prepared, meta, replace_meta)
            await s.commit()
        return result

    async def import_graph(self, graph: str, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
        """Массовая запись одной транзакцией: сначала узлы, потом рёбра (концы — по
        именам или id). Повтор той же записи идемпотентен по структуре: узлы и
        рёбра находятся по именам, элементы дописываются."""
        graph = clean_graph(graph)
        for entry in [*nodes, *edges]:
            validate_items(entry.get("items") or [])
        node_specs = [
            (clean_name(n.get("name")), clean_meta(n.get("meta")) if n.get("meta") is not None else None,
             bool(n.get("replace_meta")), await prepare_items(n.get("items")))
            for n in nodes
        ]
        edge_specs = []
        for e in edges:
            edge_specs.append((
                clean_name(e.get("name")), e.get("source"), e.get("source_id"), e.get("target"), e.get("target_id"),
                bool(e.get("directed", True)), await prepare_items(e.get("items")),
                clean_meta(e.get("meta")) if e.get("meta") is not None else None, bool(e.get("replace_meta")),
            ))
        out: dict[str, Any] = {"nodes": [], "edges": []}
        async with self.lock(graph), SessionLocal() as s:
            for name, meta, replace, prepared in node_specs:
                await self._check_refs(s, graph, prepared)
                el, created = await self._upsert_node(s, graph, name, prepared, meta, replace)
                out["nodes"].append({"id": el.id, "name": el.name, "created": created})
            for name, source, source_id, target, target_id, directed, prepared, meta, replace in edge_specs:
                await self._check_refs(s, graph, prepared)
                el, created = await self._upsert_edge(s, graph, name, source, source_id, target, target_id,
                                                      directed, prepared, meta, replace)
                out["edges"].append({**summary(el), "created": created})
            await s.commit()
        return out

    async def update(self, graph: str, kind: str, element_id: str, name: str | None = None,
                     meta: dict[str, Any] | None = None, replace_meta: bool = False) -> GraphElement:
        """Переименование и/или изменение метаданных узла или ребра."""
        meta = clean_meta(meta) if meta is not None else None
        async with self.lock(graph), SessionLocal() as s:
            el = await self._get(s, graph, element_id, kind)
            new_name = clean_name(name) if name is not None else el.name
            renamed = new_name != el.name
            if renamed:
                if el.kind == "node":
                    other = await self._find_node(s, graph, new_name)
                    what = f"node '{new_name}'"
                else:
                    other = await self._find_edge(s, graph, new_name, el.source_id, el.target_id, el.directed)
                    what = f"edge '{new_name}' between these nodes"
                if other is not None and other.id != el.id:
                    raise GraphConflict(f"{what} already exists in graph '{graph}'")
            attrs = dict(el.attrs or {})
            if meta is not None:
                attrs = dict(meta) if replace_meta else {**attrs, **meta}
            attrs_changed = attrs != (el.attrs or {})
            if not (renamed or attrs_changed):
                return el
            el.name, el.key, el.attrs = new_name, name_key(new_name), attrs
            if attrs_changed:
                await self._write_attrs(s, el)
            await s.flush()
            await self._refresh(s, el)
            # Имя узла входит в метки его рёбер.
            if renamed and el.kind == "node":
                for edge in await self._incident(s, graph, [el.id]):
                    await self._refresh(s, edge, names={el.id: el.name})
            await s.commit()
            return el

    async def delete(self, graph: str, kind: str, element_id: str) -> None:
        """Удаляет узел (вместе с его рёбрами) или ребро со всеми элементами."""
        async with self.lock(graph), SessionLocal() as s:
            el = await self._get(s, graph, element_id, kind)
            await s.execute(delete(GraphElement).where(GraphElement.id == el.id))
            await s.commit()

    async def delete_graph(self, graph: str) -> int:
        async with self.lock(graph), SessionLocal() as s:
            # Сначала рёбра: их концы — узлы того же графа.
            result = await s.execute(delete(GraphElement).where(GraphElement.graph == graph, GraphElement.kind == "edge"))
            removed = result.rowcount or 0
            result = await s.execute(delete(GraphElement).where(GraphElement.graph == graph))
            await s.commit()
            return removed + (result.rowcount or 0)

    # ── представления ────────────────────────────────────────────────────────

    async def detail(self, el: GraphElement) -> dict[str, Any]:
        """Элемент графа целиком: метаданные, элементы, ссылки и связи."""
        async with SessionLocal() as s:
            data = summary(el)
            data["brief"] = el.brief
            data["items"] = [item_out(i) for i in await self._items(s, el.id) if i.type != "attr"]
            referenced = await self._get_many(s, el.graph, el.refs or [])
            data["refs"] = [
                {"id": r, "kind": referenced[r].kind, "name": referenced[r].name} if r in referenced
                else {"id": r, "missing": True}
                for r in el.refs or []
            ]
            if el.kind == "edge":
                ends = await self._get_many(s, el.graph, [el.source_id, el.target_id])
                for role, end_id in (("source", el.source_id), ("target", el.target_id)):
                    end = ends.get(end_id)
                    data[role] = {"id": end.id, "name": end.name} if end else {"id": end_id, "missing": True}
                return data
            edges = await self._incident(s, el.graph, [el.id])
            others = await self._get_many(
                s, el.graph, [e.target_id if e.source_id == el.id else e.source_id for e in edges]
            )
            data["edges"] = []
            for edge in edges:
                if edge.source_id == edge.target_id:
                    direction, other_id = "self", el.id
                elif edge.source_id == el.id:
                    direction, other_id = ("out" if edge.directed else "both"), edge.target_id
                else:
                    direction, other_id = ("in" if edge.directed else "both"), edge.source_id
                other = el if other_id == el.id else others.get(other_id)
                data["edges"].append({
                    **summary(edge),
                    "direction": direction,
                    "other": {"id": other.id, "name": other.name} if other else {"id": other_id, "missing": True},
                })
            return data

    async def list_graphs(self) -> list[dict[str, Any]]:
        async with SessionLocal() as s:
            rows = (await s.execute(
                select(GraphElement.graph, GraphElement.kind, func.count()).group_by(GraphElement.graph, GraphElement.kind)
            )).all()
        counts: dict[str, dict[str, int]] = {}
        for graph, kind, count in rows:
            counts.setdefault(graph, {"nodes": 0, "edges": 0})["nodes" if kind == "node" else "edges"] += count
        return [{"name": name, **c} for name, c in sorted(counts.items())]

    async def list_nodes(self, graph: str, meta: dict[str, Any] | None = None, names: list[str] | None = None,
                         limit: int | None = None) -> list[GraphElement]:
        query = select(GraphElement).options(defer(GraphElement.label_vec), defer(GraphElement.brief_vec)).where(
            GraphElement.graph == graph, GraphElement.kind == "node", *_meta_conditions(meta)
        )
        if names:
            query = query.where(GraphElement.key.in_([name_key(n) for n in names]))
        async with SessionLocal() as s:
            return list((await s.execute(query.order_by(GraphElement.created_at).limit(limit))).scalars().all())

    async def dump(self, graph: str, limit: int = 50_000) -> dict[str, Any]:
        """Весь граф без элементов: узлы и рёбра с метаданными (для агентов —
        островков, обрезки, периода — и для выгрузки)."""
        async with SessionLocal() as s:
            rows = (await s.execute(
                select(GraphElement)
                .options(defer(GraphElement.label_vec), defer(GraphElement.brief_vec), defer(GraphElement.brief))
                .where(GraphElement.graph == graph)
                .order_by(GraphElement.created_at, GraphElement.id)
                .limit(limit)
            )).scalars().all()
        return {
            "graph": graph,
            "nodes": [summary(e) for e in rows if e.kind == "node"],
            "edges": [summary(e) for e in rows if e.kind == "edge"],
            "truncated": len(rows) >= limit,
        }

    # ── обход ────────────────────────────────────────────────────────────────

    @staticmethod
    def _edge_conditions(edge_names: list[str] | None, edge_meta: dict[str, Any] | None) -> list[Any]:
        conditions = _meta_conditions(edge_meta)
        if edge_names:
            conditions.append(GraphElement.key.in_([name_key(n) for n in edge_names]))
        return conditions

    async def neighbors(
        self,
        graph: str,
        seeds: list[GraphElement],
        depth: int = 1,
        direction: str = "both",
        edge_names: list[str] | None = None,
        edge_meta: dict[str, Any] | None = None,
        node_meta: dict[str, Any] | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Окрестность узлов на depth шагов (обход в ширину).

        direction: out — по направлению рёбер, in — против, both — в обе
        стороны; ненаправленные рёбра проходятся всегда. Узлы, не прошедшие
        node_meta, в окрестность не попадают и дальше не раскрываются.
        """
        if direction not in ("out", "in", "both"):
            raise ValueError("direction must be one of: out, in, both")
        depth = max(0, min(depth, MAX_DEPTH))
        nodes: dict[str, GraphElement] = {seed.id: seed for seed in seeds}
        edges: dict[str, GraphElement] = {}
        frontier = set(nodes)
        truncated = False
        base = [GraphElement.graph == graph, GraphElement.kind == "edge", *self._edge_conditions(edge_names, edge_meta)]

        async with SessionLocal() as s:
            for _ in range(depth):
                if not frontier:
                    break
                ids = list(frontier)
                outgoing = GraphElement.source_id.in_(ids)
                incoming = GraphElement.target_id.in_(ids)
                undirected = GraphElement.directed.is_(False)
                if direction == "both":
                    reach = or_(outgoing, incoming)
                elif direction == "out":
                    reach = or_(outgoing, and_(incoming, undirected))
                else:
                    reach = or_(incoming, and_(outgoing, undirected))
                found = [e for e in (await s.execute(select(GraphElement).where(*base, reach))).scalars().all()
                         if e.id not in edges]
                candidates = {end for e in found for end in (e.source_id, e.target_id) if end not in nodes}
                accepted = {
                    node_id: node for node_id, node in (await self._get_many(s, graph, candidates)).items()
                    if match_meta(node.attrs, node_meta)
                }
                frontier = set()
                for edge in found:
                    ends = (edge.source_id, edge.target_id)
                    if not all(end in nodes or end in accepted for end in ends):
                        continue
                    new_ends = {end for end in ends if end not in nodes}
                    if len(nodes) + len(new_ends) > limit:
                        truncated = True
                        continue
                    for end in new_ends:
                        nodes[end] = accepted[end]
                        frontier.add(end)
                    edges[edge.id] = edge
        return {
            "nodes": [summary(n) for n in nodes.values()],
            "edges": [summary(e) for e in edges.values()],
            "truncated": truncated,
        }

    async def subgraph(
        self,
        graph: str,
        seeds: list[GraphElement],
        expand: int = 0,
        edge_names: list[str] | None = None,
        edge_meta: dict[str, Any] | None = None,
        node_meta: dict[str, Any] | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Подграф, порождённый узлами seeds (и их окрестностью на expand шагов)."""
        nodes = {seed.id: seed for seed in seeds}
        truncated = False
        async with SessionLocal() as s:
            if expand > 0:
                around = await self.neighbors(graph, seeds, expand, "both", edge_names, edge_meta, node_meta, limit)
                nodes.update(await self._get_many(s, graph, [n["id"] for n in around["nodes"]]))
                truncated = around["truncated"]
            ids = list(nodes)
            edges: list[GraphElement] = []
            if ids:
                edges = list((await s.execute(select(GraphElement).where(
                    GraphElement.graph == graph, GraphElement.kind == "edge",
                    *self._edge_conditions(edge_names, edge_meta),
                    GraphElement.source_id.in_(ids), GraphElement.target_id.in_(ids),
                ))).scalars().all())
        return {
            "nodes": [summary(n) for n in nodes.values()],
            "edges": [summary(e) for e in edges],
            "truncated": truncated,
        }

    # ── поиск ────────────────────────────────────────────────────────────────

    async def search(
        self,
        graph: str,
        query: str,
        limit: int = 10,
        score_threshold: float = 0.35,
        kinds: list[str] | None = None,
        meta: dict[str, Any] | None = None,
        expand: int = 0,
    ) -> dict[str, Any]:
        """Смысловой поиск внутри графа: по меткам, сводкам, длинным метаданным и
        элементам. Совпадения поднимаются до узла или ребра, которому
        принадлежат; `expand` добавляет окрестность найденного (основа GraphRAG).
        Фильтры `kinds` и `meta` — точные, и применяются до отбора лучших."""
        q = (await embed_one(query)).tolist()
        where = [GraphElement.graph == graph, *_meta_conditions(meta)]
        if kinds:
            where.append(GraphElement.kind.in_(kinds))
        no_item = literal(None, String).label("item")
        by_label = select(GraphElement.id.label("root"), no_item,
                          (1 - GraphElement.label_vec.cosine_distance(q)).label("score")
                          ).where(*where, GraphElement.label_vec.is_not(None))
        by_brief = select(GraphElement.id.label("root"), literal(None, String).label("item"),
                          (1 - GraphElement.brief_vec.cosine_distance(q)).label("score")
                          ).where(*where, GraphElement.brief_vec.is_not(None))
        by_item = (
            select(GraphItem.element_id.label("root"), GraphItem.id.label("item"),
                   (1 - GraphItem.vec.cosine_distance(q)).label("score"))
            .join(GraphElement, GraphElement.id == GraphItem.element_id)
            .where(*where, GraphItem.vec.is_not(None))
        )
        hits = union_all(by_label, by_brief, by_item).subquery()
        async with SessionLocal() as s:
            rows = (await s.execute(
                select(hits.c.root, hits.c.item, hits.c.score)
                .where(hits.c.score >= score_threshold)
                .order_by(hits.c.score.desc())
                .limit(min(limit * 5, 200))
            )).all()
            best: dict[str, float] = {}
            matched: dict[str, list[tuple[str, float]]] = {}
            for root, item, score in rows:
                best[root] = max(best.get(root, 0.0), float(score))
                if item:
                    matched.setdefault(root, []).append((item, float(score)))
            elements = await self._get_many(s, graph, best)
            item_ids = [i for pairs in matched.values() for i, _ in pairs]
            items = {i.id: i for i in (await s.execute(select(GraphItem).where(GraphItem.id.in_(item_ids)))).scalars()} \
                if item_ids else {}

        results = []
        for root, score in sorted(best.items(), key=lambda kv: kv[1], reverse=True):
            element = elements.get(root)
            if element is None:
                continue
            results.append({
                "score": score,
                "element": summary(element),
                "matches": [{**item_out(items[i]), "score": sc} for i, sc in matched.get(root, [])[:5] if i in items],
            })
            if len(results) >= limit:
                break

        out: dict[str, Any] = {"results": results}
        if expand > 0 and results:
            seed_ids: set[str] = set()
            for r in results:
                e = r["element"]
                seed_ids.update([e["source_id"], e["target_id"]] if e["kind"] == "edge" else [e["id"]])
            async with SessionLocal() as s:
                seeds = list((await self._get_many(s, graph, seed_ids)).values())
            out["subgraph"] = await self.subgraph(graph, seeds, expand)
        return out


graph_store = GraphStore()
