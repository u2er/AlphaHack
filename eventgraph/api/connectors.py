from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from eventgraph.connectors import service
from eventgraph.db import get_session
from eventgraph.deps import require_auth
from eventgraph.models import Connector, ConnectorRun, Item
from eventgraph.sources import BY_NAME, SOURCES, Source

# Коннекторы заданы в sources.py: через API их запускают и смотрят, но не правят.
router = APIRouter(tags=["connectors"], dependencies=[Depends(require_auth)])


class RunOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    connector_id: int
    kind: str
    status: str
    items_total: int
    items_new: int
    items_dup: int
    preview: list[dict[str, Any]] | None
    log: str
    error: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def _out(source: Source, connector: Connector | None, items: int) -> dict[str, Any]:
    return {
        "name": source.name,
        "kind": source.kind,
        "description": source.description,
        "config": source.config,
        "interval_seconds": source.interval_seconds,
        "enabled": source.enabled,
        "images": source.images,
        "id": connector.id if connector else None,
        "state": connector.state if connector else {},
        "next_run_at": connector.next_run_at if connector else None,
        "last_run_at": connector.last_run_at if connector else None,
        "last_status": connector.last_status if connector else "",
        "last_error": connector.last_error if connector else "",
        "items_count": items,
    }


async def _get(session: AsyncSession, name: str) -> tuple[Source, Connector]:
    source = BY_NAME.get(name)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Такого источника нет в sources.py")
    connector = await session.scalar(select(Connector).where(Connector.name == name))
    if connector is None:
        # Источник добавлен в код, а воркер с тех пор не стартовал.
        await service.sync()
        connector = await session.scalar(select(Connector).where(Connector.name == name))
    return source, connector


@router.get("/connectors")
async def list_connectors(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rows = {c.name: c for c in (await session.execute(select(Connector))).scalars().all()}
    counts = dict((await session.execute(select(Item.connector_id, func.count()).group_by(Item.connector_id))).all())
    return [_out(s, rows.get(s.name), counts.get(rows[s.name].id, 0) if s.name in rows else 0) for s in SOURCES]


@router.get("/connectors/{name}")
async def get_connector(name: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    source, connector = await _get(session, name)
    items = await session.scalar(select(func.count()).select_from(Item).where(Item.connector_id == connector.id))
    return _out(source, connector, items or 0)


async def _queue(session: AsyncSession, connector: Connector, kind: str) -> ConnectorRun:
    if kind != "test":
        waiting = await session.scalar(
            select(ConnectorRun.id).where(
                ConnectorRun.connector_id == connector.id,
                ConnectorRun.status.in_(("queued", "running")),
                ConnectorRun.kind != "test",
            )
        )
        if waiting:
            return await session.get(ConnectorRun, waiting)
    run = ConnectorRun(connector_id=connector.id, kind=kind)
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


@router.post("/connectors/{name}/run", response_model=RunOut, status_code=202)
async def run_connector(name: str, session: AsyncSession = Depends(get_session)) -> ConnectorRun:
    """Запустить сейчас — с сохранением записей и курсора."""
    _, connector = await _get(session, name)
    return await _queue(session, connector, "manual")


@router.post("/connectors/{name}/test", response_model=RunOut, status_code=202)
async def test_connector(name: str, session: AsyncSession = Depends(get_session)) -> ConnectorRun:
    """Пробный запуск: что вернул бы источник (`preview`), без записи и без
    сдвига курсора. Итог — по `GET /connector-runs/{id}`."""
    _, connector = await _get(session, name)
    return await _queue(session, connector, "test")


@router.post("/connectors/{name}/state/reset")
async def reset_state(name: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Сбросить курсор — следующий запуск начнёт источник с начала."""
    source, connector = await _get(session, name)
    connector.state = {}
    await session.commit()
    items = await session.scalar(select(func.count()).select_from(Item).where(Item.connector_id == connector.id))
    return _out(source, connector, items or 0)


@router.get("/connectors/{name}/runs", response_model=list[RunOut])
async def connector_runs(
    name: str,
    limit: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[ConnectorRun]:
    _, connector = await _get(session, name)
    return (
        await session.execute(
            select(ConnectorRun).where(ConnectorRun.connector_id == connector.id)
            .order_by(ConnectorRun.id.desc()).limit(limit)
        )
    ).scalars().all()


@router.get("/connector-runs/{run_id}", response_model=RunOut)
async def get_run(run_id: int, session: AsyncSession = Depends(get_session)) -> ConnectorRun:
    run = await session.get(ConnectorRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Запуск не найден")
    return run
