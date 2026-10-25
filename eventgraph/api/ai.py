from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import Integer, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from eventgraph.agents import runner
from eventgraph.analyst import by_profile
from eventgraph.db import SessionLocal, get_session
from eventgraph.deps import require_auth
from eventgraph.models import AIChat, AISummary, CompanyProfile, Connector, Item
from profile.contract import load_profile, render_for_prompt

router = APIRouter(prefix="/ai", tags=["AI"], dependencies=[Depends(require_auth)])


class SummaryRequest(BaseModel):
    min_importance: int = Field(ge=1, le=5)
    direction: Literal["", "risk", "opportunity"] = ""


class ChatMessage(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class GraphChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=12)


async def _profile(session: AsyncSession, profile_id: str) -> CompanyProfile:
    profile = await session.get(CompanyProfile, profile_id)
    if profile is None or not profile.active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Активный профиль не найден")
    return profile


def _feed_query(profile_id: str, min_importance: int, direction: str, limit: int = 30):
    own = Item.analysis["profiles"][profile_id]["importance"].astext
    query = (
        select(Item, Connector.name)
        .join(Connector, Connector.id == Item.connector_id)
        .where(own.is_not(None), cast(own, Integer) >= min_importance)
    )
    own_direction = Item.analysis["profiles"][profile_id]["direction"].astext
    if direction == "risk":
        query = query.where(own_direction.in_(["risk", "mixed"]))
    elif direction == "opportunity":
        query = query.where(own_direction.in_(["opportunity", "mixed"]))
    return query.order_by(Item.collected_at.desc(), Item.uid.desc()).limit(limit)


def _chat_title(item: Item, profile_id: str) -> str:
    analysis = by_profile(item.analysis or {}).get(profile_id, {})
    outcome = item.outcome if isinstance(item.outcome, dict) else {}
    data = item.data or {}
    title = str(analysis.get("headline") or outcome.get("event") or data.get("title") or "Обсуждение новости")
    title = re.sub(r"\s*[—–-]\s*(?:стр(?:аница)?\.?\s*)\d+\s*$", "", title, flags=re.IGNORECASE)
    if not analysis.get("headline") and not outcome.get("event") and "_" in title:
        title = re.sub(r"_+", " ", title).strip()
    return title


@router.get("/summaries/{profile_id}")
async def get_summary(
    profile_id: str,
    min_importance: int = Query(default=3, ge=1, le=5),
    direction: Literal["", "risk", "opportunity"] = "",
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any] | None:
    await _profile(session, profile_id)
    row = await session.scalar(select(AISummary).where(
        AISummary.profile_id == profile_id,
        AISummary.min_importance == min_importance,
        AISummary.direction == direction,
    ))
    if row is None:
        return None
    return {
        "profile_id": row.profile_id,
        "min_importance": row.min_importance,
        "direction": row.direction,
        "content": row.content,
        "source_uids": row.source_uids,
        "updated_at": row.updated_at,
    }


@router.post("/graph-chat")
async def graph_chat(body: GraphChatRequest) -> dict[str, str]:
    run = await runner.run(
        "graph_chat",
        body.message,
        history=[turn.model_dump() for turn in body.history[-8:]],
    )
    if run.status != "done" or not run.output.strip():
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, run.error or "Агент графа не ответил")
    return {"reply": run.output}


@router.post("/summaries/{profile_id}")
async def create_summary(
    profile_id: str,
    body: SummaryRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    profile = await _profile(session, profile_id)
    rows = (await session.execute(_feed_query(profile_id, body.min_importance, body.direction, limit=15))).all()
    if not rows:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Нет новостей под выбранные фильтры")

    news: list[dict[str, Any]] = []
    for item, connector_name in rows:
        analysis = by_profile(item.analysis or {}).get(profile_id, {})
        data = item.data or {}
        news.append({
            "uid": item.uid,
            "source": connector_name,
            "date": str(data.get("published") or data.get("published_at") or item.collected_at.date()),
            "title": str(data.get("title") or analysis.get("headline") or item.outcome or "Новость")[:500],
            "text": item.text[:2500],
            "analysis": {
                key: analysis.get(key)
                for key in ("importance", "direction", "impact", "actions", "areas", "related_events")
                if analysis.get(key) is not None
            },
        })
    context = {
        "company": profile.name,
        "profile": profile.prompt[:12000],
        "filters": {"minimum_importance": body.min_importance, "direction": body.direction or "all"},
        "news": news,
    }
    run = await runner.run("summarizer", json.dumps(context, ensure_ascii=False))
    if run.status != "done" or not run.output.strip():
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, run.error or "Агент не вернул сводку")

    async with SessionLocal() as write_session:
        row = await write_session.scalar(select(AISummary).where(
            AISummary.profile_id == profile_id,
            AISummary.min_importance == body.min_importance,
            AISummary.direction == body.direction,
        ).with_for_update())
        if row is None:
            row = AISummary(profile_id=profile_id, min_importance=body.min_importance,
                            direction=body.direction, content=run.output.strip(),
                            source_uids=[item["uid"] for item in news])
            write_session.add(row)
        else:
            row.content = run.output.strip()
            row.source_uids = [item["uid"] for item in news]
            row.updated_at = datetime.now(timezone.utc)
        await write_session.commit()
        await write_session.refresh(row)
    return {
        "profile_id": row.profile_id,
        "min_importance": row.min_importance,
        "direction": row.direction,
        "content": row.content,
        "source_uids": row.source_uids,
        "updated_at": row.updated_at,
    }


@router.get("/chats/{profile_id}")
async def list_chats(
    profile_id: str,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await _profile(session, profile_id)
    rows = (await session.execute(
        select(AIChat, Item, Connector.name)
        .join(Item, Item.uid == AIChat.item_uid)
        .join(Connector, Connector.id == Item.connector_id)
        .where(AIChat.profile_id == profile_id)
        .order_by(AIChat.updated_at.desc())
        .limit(100)
    )).all()
    return [{
        "id": chat.id,
        "item_uid": item.uid,
        "title": _chat_title(item, profile_id),
        "article": {
            "uid": item.uid,
            "title": _chat_title(item, profile_id),
            "url": (item.data or {}).get("url") or (item.data or {}).get("link") or "",
            "published": str((item.data or {}).get("published") or (item.data or {}).get("published_at") or ""),
            "connector": source_name,
            "event": (item.outcome or {}).get("event") if isinstance(item.outcome, dict) else item.outcome,
            "analysis": by_profile(item.analysis or {}).get(profile_id, {}),
        },
        "updated_at": chat.updated_at,
        "message_count": len(chat.messages or []),
    } for chat, item, source_name in rows]


@router.get("/chats/{profile_id}/{item_uid}")
async def get_chat(
    profile_id: str,
    item_uid: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _profile(session, profile_id)
    chat = await session.scalar(select(AIChat).where(
        AIChat.profile_id == profile_id, AIChat.item_uid == item_uid))
    item_row = (await session.execute(
        select(Item, Connector.name)
        .join(Connector, Connector.id == Item.connector_id)
        .where(Item.uid == item_uid)
    )).first()
    if item_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Новость не найдена")
    item, source_name = item_row
    article = {
        "uid": item.uid,
        "title": _chat_title(item, profile_id),
        "url": (item.data or {}).get("url") or (item.data or {}).get("link") or "",
        "published": str((item.data or {}).get("published") or (item.data or {}).get("published_at") or ""),
        "connector": source_name,
        "event": (item.outcome or {}).get("event") if isinstance(item.outcome, dict) else item.outcome,
        "analysis": by_profile(item.analysis or {}).get(profile_id, {}),
    }
    if chat is None:
        return {"id": None, "messages": [], "article": article}
    return {"id": chat.id, "messages": chat.messages, "updated_at": chat.updated_at, "article": article}


@router.post("/chats/{profile_id}/{item_uid}")
async def post_chat_message(
    profile_id: str,
    item_uid: str,
    body: ChatMessage,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    profile = await _profile(session, profile_id)
    item_row = (await session.execute(
        select(Item, Connector.name)
        .join(Connector, Connector.id == Item.connector_id)
        .where(Item.uid == item_uid)
    )).first()
    if item_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Новость не найдена")
    item, source_name = item_row

    related_news: list[dict[str, Any]] = []
    if item.story_id is not None:
        related_rows = (await session.execute(
            select(Item, Connector.name)
            .join(Connector, Connector.id == Item.connector_id)
            .where(Item.story_id == item.story_id, Item.uid != item.uid)
            .order_by(Item.collected_at.desc())
            .limit(5)
        )).all()
        for related, related_source in related_rows:
            related_analysis = by_profile(related.analysis or {}).get(profile_id, {})
            related_data = related.data or {}
            outcome = related.outcome if isinstance(related.outcome, dict) else {}
            related_news.append({
                "uid": related.uid,
                "title": str(related_data.get("title") or outcome.get("event") or "Новость"),
                "source": related_source,
                "date": str(related_data.get("published") or related_data.get("published_at") or related.collected_at),
                "url": related_data.get("url") or related_data.get("link") or "",
                "text": (related.text or "")[:2500],
                "analysis": {
                    key: related_analysis.get(key)
                    for key in ("importance", "direction", "impact", "related_events")
                    if related_analysis.get(key) is not None
                },
            })

    chat = await session.scalar(select(AIChat).where(
        AIChat.profile_id == profile_id, AIChat.item_uid == item_uid).with_for_update())
    if chat is None:
        chat = AIChat(id=str(uuid4()), profile_id=profile_id, item_uid=item_uid, messages=[])
        session.add(chat)
        await session.commit()
        chat = await session.scalar(select(AIChat).where(
            AIChat.profile_id == profile_id, AIChat.item_uid == item_uid))
    history = list(chat.messages or [])
    session.expunge(chat)
    session.expunge(item)
    await session.commit()
    outcome = item.outcome if isinstance(item.outcome, dict) else {}
    article_data = item.data or {}
    news_context = json.dumps({
        "article": {
            "uid": item.uid,
            "title": str(article_data.get("title") or outcome.get("event") or "Новость"),
            "url": article_data.get("url") or article_data.get("link") or "",
            "date": str(article_data.get("published") or article_data.get("published_at") or item.collected_at),
            "source": source_name,
            "text": (item.text or "")[:10000],
            "analysis": by_profile(item.analysis or {}).get(profile_id, {}),
            "event": outcome.get("event"),
        },
        "related_news": related_news,
    }, ensure_ascii=False)
    run = await runner.run(
        "news_chat", body.message, history=history,
        variables={
            "profile_id": profile_id,
            "company_profile": render_for_prompt(load_profile(profile.payload))[:10000],
            "news_context": news_context,
        },
    )
    if run.status != "done" or not run.output.strip():
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, run.error or "Чат-агент не ответил")

    now = datetime.now(timezone.utc).isoformat()
    async with SessionLocal() as write_session:
        saved = await write_session.scalar(select(AIChat).where(
            AIChat.profile_id == profile_id, AIChat.item_uid == item_uid).with_for_update())
        if saved is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Чат был удалён во время ответа")
        messages = list(saved.messages or [])
        messages.extend([
            {"role": "user", "content": body.message, "created_at": now},
            {"role": "assistant", "content": run.output.strip(), "created_at": now, "run_id": run.id},
        ])
        saved.messages = messages[-40:]
        saved.updated_at = datetime.now(timezone.utc)
        await write_session.commit()
    return {"id": chat.id, "messages": saved.messages}
