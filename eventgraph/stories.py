from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import or_, select

from eventgraph import embed as embedder
from eventgraph.config import settings
from eventgraph.db import SessionLocal
from eventgraph.models import Item

logger = logging.getLogger(__name__)

# Вердикты агента, после которых у записи есть событие в графе.
IN_GRAPH = ("stored", "merged")

# Коннекторы кончают сбор когда придётся: два разом взяли бы одни и те же
# записи и дважды гоняли эмбеддер.
_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def configured() -> bool:
    return settings.stories_enabled


def record_text(item: Item, chars: int) -> str:
    """Заголовок и текст записи, обрезанные до `chars`: для сюжета хватает начала."""
    data = item.data or {}
    title = str(data.get("title") or "").strip()
    text = (item.text or "").strip()
    body = text if not title or text.startswith(title) else f"{title}\n{text}"
    return body[:chars]


async def embed(texts: list[str]) -> np.ndarray:
    """Нормированные векторы текстов."""
    return await embedder.embed(texts)


def pack(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def unpack(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.float32)


@dataclass
class Known:
    """Запись, с которой сравнивается новая."""

    id: int
    story: int
    vector: np.ndarray
    collected_at: datetime
    # Событие графа, если агент уже разобрал запись (stored/merged).
    event: str | None = None
    title: str = ""


@dataclass
class Place:
    story: int
    # Повтор разобранного: запись, чьё событие она повторяет.
    duplicate_of: Known | None = None


def place(
    item_id: int,
    vector: np.ndarray,
    at: datetime,
    known: list[Known],
    story_threshold: float,
    story_window: timedelta,
    duplicate_threshold: float,
    duplicate_window: timedelta,
) -> Place:
    """Куда встаёт новая запись (см. описание модуля). `known` — записи с
    векторами за последние сутки-двое, в любом порядке."""
    # Векторы другой модели (эмбеддер сменили) сравнивать не с чем.
    known = [k for k in known if k.vector.shape == vector.shape]
    if not known:
        return Place(story=item_id)
    sims = np.stack([k.vector for k in known]) @ vector
    starts: dict[int, datetime] = {}
    for k in known:
        starts[k.story] = min(starts.get(k.story, k.collected_at), k.collected_at)

    best_dup, best_dup_sim = None, duplicate_threshold
    best_story, best_story_sim = None, story_threshold
    for k, sim in zip(known, sims.tolist()):
        if k.event and 0 < duplicate_threshold <= sim and sim >= best_dup_sim and at - k.collected_at <= duplicate_window:
            best_dup, best_dup_sim = k, sim
        if sim >= best_story_sim and at - starts[k.story] <= story_window:
            best_story, best_story_sim = k.story, sim
    if best_dup is not None:
        return Place(story=best_dup.story, duplicate_of=best_dup)
    return Place(story=best_story if best_story is not None else item_id)


def _title(item: Item) -> str:
    data = item.data or {}
    return " ".join(str(data.get("title") or item.text or "").split())[:200]


async def organize(cutoff: datetime | None = None) -> dict[str, int]:
    """Векторы и сюжеты для записей, у которых их ещё нет.
    Счётчики: placed — получили сюжет, duplicates — закрыты как повторы."""
    if not configured():
        return {"placed": 0, "duplicates": 0}
    async with _lock:
        return await _organize(cutoff or _now())


async def _organize(now: datetime) -> dict[str, int]:
    counts = {"placed": 0, "duplicates": 0}
    async with SessionLocal() as session:
        fresh = (
            await session.execute(
                select(Item)
                .where(
                    Item.processed_at.is_(None),
                    Item.embedding.is_(None),
                    Item.collected_at <= now,
                )
                .order_by(Item.collected_at, Item.id)
            )
        ).scalars().all()
        if not fresh:
            return counts
        try:
            vectors = await embed([record_text(item, settings.embed_chars) for item in fresh])
        except Exception as exc:  # noqa: BLE001 — без векторов записи просто идут по одной
            logger.warning("эмбеддер сюжетов не ответил: %s", exc)
            return counts

        story_window = timedelta(hours=settings.story_window_hours)
        duplicate_window = timedelta(hours=settings.duplicate_window_hours)
        since = fresh[0].collected_at - max(story_window, duplicate_window)
        rows = (
            await session.execute(
                select(Item).where(
                    Item.embedding.is_not(None),
                    Item.collected_at >= since,
                    or_(Item.verdict.is_(None), Item.verdict != "failed"),
                )
            )
        ).scalars().all()
        known = [_known(item) for item in rows]

        for item, vector in zip(fresh, vectors):
            spot = place(
                item.id, vector, item.collected_at, known,
                settings.story_threshold, story_window, settings.duplicate_threshold, duplicate_window,
            )
            item.embedding = pack(vector)
            item.story_id = spot.story
            counts["placed"] += 1
            if spot.duplicate_of is not None:
                item.processed_at = _now()
                item.verdict = "duplicate"
                item.outcome = {
                    "event": spot.duplicate_of.event,
                    "reason": f"повтор уже разобранного: {spot.duplicate_of.title}"[:500],
                }
                counts["duplicates"] += 1
            known.append(_known(item))
        await session.commit()
    logger.info("сюжеты: размещено %s, повторов %s", counts["placed"], counts["duplicates"])
    return counts


def _known(item: Item) -> Known:
    event = (item.outcome or {}).get("event") if item.verdict in IN_GRAPH else None
    return Known(
        id=item.id,
        story=item.story_id or item.id,
        vector=unpack(item.embedding),
        collected_at=item.collected_at,
        event=str(event) if event else None,
        title=_title(item),
    )
