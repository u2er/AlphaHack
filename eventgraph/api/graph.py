from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from eventgraph import gardening, graph_settings, news
from eventgraph.db import get_session
from eventgraph.deps import require_auth
from eventgraph.graph_settings import GraphSettings
from eventgraph.models import AgentRun, Connector, Item, MaintenanceRun, MaintenanceTask

# Разбор записей агентом новостей и обслуживание графа садовником.
router = APIRouter(prefix="/graph", tags=["graph"], dependencies=[Depends(require_auth)])


class MaintenanceOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    tasks: int
    merged: int
    tool_calls: int
    tokens: int
    report: dict[str, Any] | None = None
    error: str


class TaskOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    agent_run_id: int | None
    task: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    merged: int
    steps: int
    tool_calls: int
    tokens: int
    duration_ms: int
    summary: str
    error: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/status")
async def graph_status(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Как идёт разбор: очередь, запись в работе, последняя разобранная, пауза
    после сбоя и обслуживание."""
    cfg = await graph_settings.load(session)
    control = await graph_settings.control(session)
    current = await session.scalar(
        select(AgentRun).where(AgentRun.agent.in_(("news", "gardener")), AgentRun.status == "running")
        .order_by(AgentRun.id.desc()).limit(1)
    )
    working = None
    if current is not None:
        # Запись, которую разбирает ран, названа в его сообщении: «### <uid> · …».
        found = re.search(r"^### (\S+) · ", current.input or "", re.M) if current.agent == "news" else None
        uid = found.group(1) if found else None
        working = {"agent": current.agent, "run_id": current.id, "started_at": current.started_at,
                   "steps": current.steps, **({"uid": uid} if uid else {})}
    last = (await session.execute(
        select(Item.uid, Item.verdict, Item.outcome, Item.processed_at, Item.agent_run_id)
        .where(Item.agent_run_id.is_not(None), Item.processed_at.is_not(None))
        .order_by(Item.processed_at.desc()).limit(1)
    )).first()
    failed_total = await session.scalar(select(func.count()).select_from(Item).where(Item.verdict == "failed"))
    maintenance = await session.scalar(select(MaintenanceRun).order_by(MaintenanceRun.started_at.desc()).limit(1))
    reference = await gardening.reference()
    paused = graph_settings.paused_until(control)
    return {
        "agent_configured": news.configured(),
        "gardener_configured": gardening.configured(),
        "settings": cfg.to_dict(),
        "processing": news.configured() and cfg.enabled,
        "queue": await news.queue_length(),
        "failed_total": failed_total or 0,
        "working": working,
        "last_processed": dict(last._mapping) if last else None,
        # После рана без ответа разбор ждёт — дольше с каждым сбоем подряд.
        "paused_until": paused if paused and paused > _now() else None,
        "failures_in_a_row": control["failures"],
        "maintenance": MaintenanceOut.model_validate(maintenance).model_dump() if maintenance else None,
        "maintenance_due_after": reference + timedelta(days=cfg.maintenance_every_days) if reference else None,
        "maintain_requested": control["maintain"],
    }


@router.get("/settings")
async def get_settings(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return (await graph_settings.load(session)).to_dict()


@router.put("/settings")
async def put_settings(body: dict[str, Any], session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """`enabled: false` — поставить разбор на паузу: записи копятся."""
    try:
        cfg = GraphSettings.from_dict({**(await graph_settings.load(session)).to_dict(), **body})
        error = cfg.validate()
    except (TypeError, ValueError) as exc:
        error = f"Некорректные настройки: {exc}"
    if error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, error)
    await graph_settings.save(session, cfg)
    return cfg.to_dict()


@router.post("/resume", status_code=202)
async def resume(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Снять паузу после сбоя — не дожидаясь её конца."""
    await graph_settings.set_control(session, failures=0, paused_until=None)
    return {"status": "resumed"}


@router.post("/maintain", status_code=202)
async def maintain(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if not gardening.configured():
        raise HTTPException(status.HTTP_409_CONFLICT, "Садовник выключен: нужен LLM_API_KEY и gardener в AGENTS")
    if await session.scalar(select(MaintenanceRun.id).where(MaintenanceRun.status == "running").limit(1)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Обслуживание уже идёт")
    await graph_settings.set_control(session, maintain=True, stop=False)
    return {"status": "requested", "note": "садовник начнёт после записи, которая сейчас разбирается"}


@router.post("/maintain/stop", status_code=202)
async def stop_maintenance(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if not await session.scalar(select(MaintenanceRun.id).where(MaintenanceRun.status == "running").limit(1)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Обслуживания сейчас нет")
    await graph_settings.set_control(session, stop=True)
    return {"status": "requested", "note": "обслуживание остановится после текущей задачи"}


@router.post("/requeue-failed")
async def requeue_failed(
    uids: list[str] | None = None, session: AsyncSession = Depends(get_session)
) -> dict[str, int]:
    """Вернуть неразобранные записи в очередь: попытки с нуля.

    Записи получают failed, когда раны с ними раз за разом срываются, — часто
    не из-за самих записей (модель была недоступна, сменились её параметры).
    `uids` — только эти; без них — все неразобранные.
    """
    query = update(Item).where(Item.verdict == "failed")
    if uids:
        query = query.where(Item.uid.in_(uids))
    result = await session.execute(
        query.values(processed_at=None, verdict=None, outcome=None, attempts=0).returning(Item.id)
    )
    count = len(result.scalars().all())
    await session.commit()
    return {"requeued": count}


@router.post("/requeue-below-threshold")
async def requeue_below_threshold(
    uids: list[str] | None = None, session: AsyncSession = Depends(get_session)
) -> dict[str, int]:
    """Вернуть в очередь записи, закрытые порогом важности, — например, после
    того как порог снизили. Оценка остаётся: аналитик второй раз не зовётся,
    запись сверяется с новым порогом и, если проходит, идёт агенту новостей.
    `uids` — только эти; без них — все такие записи."""
    query = update(Item).where(Item.verdict == "below_threshold")
    if uids:
        query = query.where(Item.uid.in_(uids))
    result = await session.execute(
        query.values(processed_at=None, verdict=None, outcome=None, agent_run_id=None, attempts=0).returning(Item.id)
    )
    count = len(result.scalars().all())
    await session.commit()
    return {"requeued": count}


@router.get("/maintenance", response_model=list[MaintenanceOut])
async def list_maintenance(
    before: int | None = Query(default=None, description="id последнего показанного прогона"),
    limit: int = Query(default=30, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[MaintenanceRun]:
    query = select(MaintenanceRun)
    if before:
        query = query.where(MaintenanceRun.id < before)
    return (await session.execute(query.order_by(MaintenanceRun.id.desc()).limit(limit))).scalars().all()


@router.get("/maintenance/{run_id}")
async def get_maintenance(run_id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    run = await session.get(MaintenanceRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Прогон обслуживания не найден")
    tasks = (await session.execute(
        select(MaintenanceTask).where(MaintenanceTask.run_id == run_id).order_by(MaintenanceTask.id)
    )).scalars().all()
    return {"run": MaintenanceOut.model_validate(run).model_dump(),
            "tasks": [TaskOut.model_validate(t).model_dump() for t in tasks]}


@router.get("/processed")
async def processed(
    verdict: str | None = Query(default=None, description="stored | merged | skipped | duplicate | below_threshold | failed"),
    min_importance: int | None = Query(default=None, ge=0, le=5, description="важность для компании от"),
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Последние разобранные записи: вердикт, событие или причина, ран агента
    и оценка аналитика для компании."""
    query = (
        select(Item.uid, Connector.name, Item.verdict, Item.outcome, Item.processed_at, Item.agent_run_id,
               Item.attempts, Item.data["title"].astext, Item.analysis)
        .join(Connector, Connector.id == Item.connector_id)
        .where(Item.processed_at.is_not(None))
    )
    if verdict:
        query = query.where(Item.verdict == verdict)
    if min_importance is not None:
        query = query.where(Item.analysis["importance"].as_integer() >= min_importance)
    rows = (await session.execute(query.order_by(Item.processed_at.desc()).limit(limit))).all()
    return [
        {"uid": uid, "connector": name, "title": title, "verdict": v, **(outcome or {}), "processed_at": at,
         "run_id": run_id, "attempts": attempts, "analysis": analysis}
        for uid, name, v, outcome, at, run_id, attempts, title, analysis in rows
    ]


@router.get("/stats")
async def stats(days: int = Query(default=14, ge=1, le=90), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """По дням (в часовом поясе настроек): новые события, склеено, отсеяно, ошибки."""
    cfg = await graph_settings.load(session)
    since = _now() - timedelta(days=days)
    day = func.date(func.timezone(cfg.timezone, Item.processed_at))
    rows = (
        await session.execute(
            select(day, Item.verdict, func.count(), func.count(func.distinct(Item.outcome["event"].astext)))
            .where(Item.processed_at >= since, Item.verdict.is_not(None))
            .group_by(day, Item.verdict)
        )
    ).all()
    out: dict[str, dict[str, int]] = {}
    for date, verdict, count, distinct_events in rows:
        entry = out.setdefault(str(date), {"events": 0, "stored": 0, "merged": 0, "skipped": 0, "duplicate": 0, "below_threshold": 0, "failed": 0})
        if verdict in entry:
            entry[verdict] += count
        if verdict == "stored":
            entry["events"] += distinct_events
    return {"timezone": cfg.timezone, "days": [{"date": d, **v} for d, v in sorted(out.items())]}
