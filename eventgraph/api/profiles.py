from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, ValidationError
from sqlalchemy import Integer, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from eventgraph import analyst, profiles
from eventgraph.db import get_session
from eventgraph.deps import require_auth
from eventgraph.models import CompanyProfile, Connector, Item

router = APIRouter(prefix="/profiles", tags=["profiles"], dependencies=[Depends(require_auth)])


class ProfileOut(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    name: str
    company_id: str | None
    active: bool
    source: str
    created_at: datetime
    updated_at: datetime


class FeedItem(BaseModel):
    uid: str
    connector: str
    title: str
    url: str
    published: str
    collected_at: datetime
    verdict: str | None
    event: str | None
    # Оценка аналитика для этого клиента: importance, impact, direction, …
    analysis: dict[str, Any]


async def _get(session: AsyncSession, profile_id: str) -> CompanyProfile:
    row = await session.get(CompanyProfile, profile_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Профиль не найден")
    return row


@router.get("", response_model=list[ProfileOut])
async def list_profiles(session: AsyncSession = Depends(get_session)) -> list[CompanyProfile]:
    """Профили клиентов. Пока нет ни одного, аналитик работает с COMPANY_PROFILE."""
    return (await session.execute(select(CompanyProfile).order_by(CompanyProfile.created_at))).scalars().all()


@router.put("", response_model=ProfileOut)
async def put_profile(payload: dict[str, Any] = Body(..., description="экспорт мастера профиля, schema_version 1")
                      ) -> CompanyProfile:
    """Загрузить профиль клиента (тот же JSON, что мастер кладёт в PROFILES_DIR).
    Повторная загрузка с тем же profile_id — новая версия; новые записи
    оцениваются уже по ней."""
    try:
        return await profiles.upsert(payload, source="api")
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            exc.errors(include_url=False, include_context=False, include_input=False)) from None


@router.get("/{profile_id}")
async def get_profile(profile_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Профиль целиком: экспорт и текст, который видит аналитик."""
    row = await _get(session, profile_id)
    return {**ProfileOut.model_validate(row).model_dump(mode="json"), "prompt": row.prompt, "payload": row.payload}


@router.delete("/{profile_id}")
async def deactivate_profile(profile_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Снять профиль с оценки: новые записи для него не оцениваются, прошлые
    оценки и лента остаются. PUT того же профиля включает его снова."""
    row = await _get(session, profile_id)
    row.active = False
    await session.commit()
    return {"id": row.id, "active": False}


@router.get("/{profile_id}/feed", response_model=list[FeedItem])
async def feed(
    profile_id: str,
    min_importance: int = Query(default=1, ge=0, le=5),
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[FeedItem]:
    """Лента клиента: записи, оценённые для него не ниже `min_importance`,
    важные первыми, внутри важности — свежие. Оценка — его, а не самая важная."""
    own = Item.analysis["profiles"][profile_id]["importance"].astext
    if profile_id == profiles.DEFAULT_ID:
        # Оценки, сделанные до профилей клиентов (без `profiles`), — его.
        own = func.coalesce(own, case((~Item.analysis.has_key("profiles"), Item.analysis["importance"].astext)))
    else:
        await _get(session, profile_id)
    importance = cast(own, Integer)
    rows = (await session.execute(
        select(Item, Connector.name)
        .join(Connector, Connector.id == Item.connector_id)
        .where(own.isnot(None), importance >= min_importance)
        .order_by(importance.desc(), Item.collected_at.desc())
        .limit(limit)
    )).all()
    out = []
    for item, name in rows:
        data = item.data or {}
        out.append(FeedItem(
            uid=item.uid, connector=name, title=str(data.get("title") or ""),
            url=str(data.get("url") or data.get("link") or ""),
            published=str(data.get("published") or data.get("published_at") or data.get("date") or ""),
            collected_at=item.collected_at, verdict=item.verdict, event=(item.outcome or {}).get("event"),
            analysis=analyst.by_profile(item.analysis)[profile_id],
        ))
    return out
