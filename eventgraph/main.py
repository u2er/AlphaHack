from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from sqlalchemy import text

from eventgraph import embed
from eventgraph.api import agents, connectors, graph, graph_data, items, profiles
from eventgraph.config import settings
from eventgraph.connectors import service
from eventgraph.db import create_schema, engine
from eventgraph.logging_config import setup_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    try:
        await create_schema()
        await service.sync()
    except Exception as exc:  # noqa: BLE001
        # Поднимаемся всё равно: /health честно покажет, что базы нет.
        logger.error("база недоступна: %s", exc)
    if not settings.api_key:
        logger.warning("API_KEY не задан — API закрыт")
    if not settings.llm_api_key:
        logger.warning("LLM_API_KEY не задан — агенты не работают, записи просто копятся")
    yield


app = FastAPI(
    title="EventGraph",
    description=(
        "Граф событий из новостей: коннекторы собирают записи, агенты разбирают их в граф "
        "и обслуживают его. Всё в одном проекте, без интерфейса."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


async def _db_ok() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


@app.get("/health", tags=["health"])
async def health() -> dict[str, Any]:
    db = await _db_ok()
    return {
        "status": "ok" if db else "degraded",
        "db": db,
        "llm": bool(settings.llm_api_key),
        "model": settings.llm_model,
        "embedder": settings.emb_base_url or f"local:{settings.emb_model}",
        "embedder_remote": embed.remote(),
        "agents": sorted(settings.enabled_agents),
        "graph": settings.graph_name,
    }


app.include_router(connectors.router)
app.include_router(items.router)
app.include_router(graph.router)
app.include_router(graph_data.router)
app.include_router(agents.router)
app.include_router(profiles.router)
