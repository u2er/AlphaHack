from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eventgraph.agents import runner
from eventgraph.agents.specs import SPECS
from eventgraph.config import settings
from eventgraph.db import get_session
from eventgraph.deps import require_auth
from eventgraph.models import AgentRun

router = APIRouter(tags=["agents"], dependencies=[Depends(require_auth)])


class RunBrief(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    agent: str
    status: str
    steps: int
    tool_calls: int
    tokens_prompt: int
    tokens_completion: int
    duration_ms: int
    error: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunOut(RunBrief):
    input: str
    output: str
    attachments: dict[str, str]


@router.get("/agents")
async def agents() -> list[dict[str, Any]]:
    """Агенты проекта: права, параметры и включён ли."""
    return [
        {
            "name": spec.name,
            "enabled": settings.agent_on(spec.name),
            "max_steps": spec.max_steps,
            "temperature": spec.temperature,
            "write": spec.write,
            "delete": spec.delete,
            "maintenance": spec.maintenance,
        }
        for spec in SPECS.values()
    ]


@router.get("/agent-runs", response_model=list[RunBrief])
async def list_runs(
    agent: str | None = None,
    status_: str | None = Query(default=None, alias="status"),
    before: int | None = Query(default=None, description="id последнего показанного рана"),
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[AgentRun]:
    query = select(AgentRun)
    if agent:
        query = query.where(AgentRun.agent == agent)
    if status_:
        query = query.where(AgentRun.status == status_)
    if before:
        query = query.where(AgentRun.id < before)
    return (await session.execute(query.order_by(AgentRun.id.desc()).limit(limit))).scalars().all()


@router.get("/agent-runs/{run_id}")
async def get_run(run_id: int, logs: bool = True, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Ран с его шагами: рассуждения, вызовы инструментов с аргументами и ответами."""
    row = await session.get(AgentRun, run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ран не найден")
    out: dict[str, Any] = {"run": RunOut.model_validate(row).model_dump()}
    if logs:
        out["log"] = [
            {"step": entry.step, "at": entry.at, "kind": entry.kind, "tool": entry.tool, **entry.data}
            for entry in await runner.logs(run_id)
        ]
    return out


@router.post("/agent-runs/{run_id}/stop", status_code=202)
async def stop_run(run_id: int) -> dict[str, str]:
    """Остановить ран после текущего шага."""
    if not await runner.cancel(run_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Ран не идёт")
    return {"status": "requested"}
