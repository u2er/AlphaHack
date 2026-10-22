from __future__ import annotations

import asyncio
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from eventgraph.config import settings


class Base(DeclarativeBase):
    pass


# create_all не добавляет колонки в уже созданные таблицы — их добавляют здесь.
COLUMNS = (
    "ALTER TABLE items ADD COLUMN IF NOT EXISTS analysis JSONB",
    "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS variables JSONB",
)


# Движок не подключается до первого запроса — импорт модуля базы не требует.
engine = create_async_engine(settings.database_url, pool_pre_ping=True, pool_size=10, max_overflow=10)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def create_schema(attempts: int = 5) -> None:
    """Схема без миграций: таблицы создаются, если их нет.

    API и воркер поднимаются одновременно и оба создают схему; кто-то из них
    может споткнуться о чужой CREATE или о ещё не проснувшуюся базу —
    поэтому несколько попыток.
    """
    from eventgraph import models  # noqa: F401 — регистрирует таблицы в metadata

    for attempt in range(1, attempts + 1):
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
                await conn.run_sync(Base.metadata.create_all)
                for statement in COLUMNS:
                    await conn.exec_driver_sql(statement)
            return
        except Exception:  # noqa: BLE001
            if attempt == attempts:
                raise
            await asyncio.sleep(2 * attempt)
