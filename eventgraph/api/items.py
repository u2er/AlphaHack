from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from eventgraph.config import settings
from eventgraph.connectors.images import MEDIA_TYPES
from eventgraph.db import get_session
from eventgraph.deps import require_auth
from eventgraph.models import Connector, Item

router = APIRouter(prefix="/items", tags=["items"], dependencies=[Depends(require_auth)])


class ItemOut(BaseModel):
    model_config = {"from_attributes": True}

    uid: str
    connector_id: int
    connector: str = ""
    external_id: str
    text: str
    images: list[str]
    data: dict[str, Any]
    collected_at: datetime
    processed_at: datetime | None
    agent_run_id: int | None
    verdict: str | None = None
    outcome: dict[str, Any] | None = None
    analysis: dict[str, Any] | None = None
    story_id: int | None = None


async def _get(session: AsyncSession, uid: str) -> Item:
    item = await session.scalar(select(Item).where(Item.uid == uid))
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Запись не найдена")
    return item


@router.get("", response_model=list[ItemOut])
async def list_items(
    connector_id: int | None = None,
    processed: bool | None = None,
    verdict: str | None = Query(default=None, description="stored | merged | skipped | duplicate | below_threshold | failed"),
    before: str | None = Query(default=None, description="uid последней показанной записи"),
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[ItemOut]:
    """Свежие первыми. Курсор — uid: ULID сортируется по времени."""
    query = select(Item, Connector.name).join(Connector, Connector.id == Item.connector_id)
    if connector_id is not None:
        query = query.where(Item.connector_id == connector_id)
    if processed is not None:
        query = query.where(
            Item.processed_at.is_not(None) if processed else Item.processed_at.is_(None)
        )
    if verdict:
        query = query.where(Item.verdict == verdict)
    if before:
        query = query.where(Item.uid < before)
    rows = (await session.execute(query.order_by(Item.uid.desc()).limit(limit))).all()
    out = []
    for item, name in rows:
        row = ItemOut.model_validate(item)
        row.connector = name
        out.append(row)
    return out


# Статусы разбора, по которым чистится лента: pending — ещё не разобраны.
STATES = ("pending", "processed", "stored", "merged", "skipped", "duplicate", "below_threshold", "failed")


def _where(
    connector_id: int | None, state: str | None, older_than_days: int | None, keep_processed: bool = False
) -> list:
    if state and state not in STATES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"state — один из: {', '.join(STATES)}")
    conditions = []
    if connector_id is not None:
        conditions.append(Item.connector_id == connector_id)
    if state == "pending":
        conditions.append(Item.processed_at.is_(None))
    elif state == "processed":
        conditions.append(Item.processed_at.is_not(None))
    elif state:
        conditions.append(Item.verdict == state)
    if older_than_days:
        conditions.append(Item.collected_at < datetime.now(timezone.utc) - timedelta(days=older_than_days))
    if keep_processed:
        # То, что агент уже разобрал, не трогаем — чистится только очередь.
        conditions.append(Item.processed_at.is_(None))
    return conditions


@router.get("/count")
async def count_items(
    connector_id: int | None = None,
    state: str | None = None,
    older_than_days: int | None = Query(default=None, ge=1, le=3650),
    keep_processed: bool = False,
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Сколько записей попадёт под очистку с такими фильтрами."""
    conditions = _where(connector_id, state, older_than_days, keep_processed)
    total = await session.scalar(select(func.count()).select_from(Item).where(*conditions))
    return {"count": total or 0}


@router.delete("")
async def clear_items(
    connector_id: int | None = None,
    state: str | None = None,
    older_than_days: int | None = Query(default=None, ge=1, le=3650),
    keep_processed: bool = Query(default=False, description="true — разобранное агентом оставить"),
    confirm: bool = Query(default=False, description="true — да, удалить"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Очистка ленты: удаляет записи по фильтрам вместе с их картинками.

    Без `confirm=true` ничего не удаляет — случайный DELETE /items не должен
    стирать всю ленту. То, что агент уже записал в граф, остаётся.
    """
    if not confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Подтвердите очистку: confirm=true")
    conditions = _where(connector_id, state, older_than_days, keep_processed)
    images = (
        await session.execute(select(Item.images).where(*conditions))
    ).scalars().all()
    result = await session.execute(delete(Item).where(*conditions).returning(Item.id))
    deleted = len(result.scalars().all())
    await session.commit()

    def unlink() -> None:
        for paths in images:
            for rel in paths or []:
                (settings.data_dir / rel).unlink(missing_ok=True)

    await asyncio.to_thread(unlink)
    return {"deleted": deleted}


@router.get("/{uid}", response_model=ItemOut)
async def get_item(uid: str, session: AsyncSession = Depends(get_session)) -> ItemOut:
    item = await _get(session, uid)
    row = ItemOut.model_validate(item)
    row.connector = (await session.get(Connector, item.connector_id)).name
    return row


@router.get("/{uid}/images/{n}")
async def get_image(uid: str, n: int, session: AsyncSession = Depends(get_session)) -> Response:
    item = await _get(session, uid)
    if not 0 <= n < len(item.images):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Картинки нет")
    path = settings.data_dir / item.images[n]
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл картинки пропал")
    content = await asyncio.to_thread(path.read_bytes)
    ext = path.suffix.lstrip(".")
    return Response(content=content, media_type=MEDIA_TYPES.get(ext, "application/octet-stream"))


@router.delete("/{uid}", status_code=204, response_class=Response)
async def delete_item(uid: str, session: AsyncSession = Depends(get_session)) -> Response:
    item = await _get(session, uid)
    paths = [settings.data_dir / rel for rel in item.images]
    await session.delete(item)
    await session.commit()
    for path in paths:
        path.unlink(missing_ok=True)
    return Response(status_code=204)
