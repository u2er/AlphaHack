"""Профили клиентов: для кого аналитик оценивает записи.

Профиль — подтверждённый экспорт мастера профиля (profile/contract.py,
schema_version 1). Он попадает сюда двумя путями: PUT /profiles и каталог
PROFILES_DIR, куда мастер и GUI кладут экспорт (воркер сверяет каталог перед
разбором). Аналитик видит профиль текстом `render_for_prompt` вместо
{{company_profile}}. Пока ни одного профиля нет, работает прежняя схема:
один профиль `default` из COMPANY_PROFILE (или заготовка).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from eventgraph.config import settings
from eventgraph.db import SessionLocal
from eventgraph.models import CompanyProfile
from profile.contract import ProfileExport, display_name, render_for_prompt

logger = logging.getLogger(__name__)

DEFAULT_ID = "default"


@dataclass(frozen=True)
class Active:
    id: str
    name: str
    prompt: str


def parse(payload: dict[str, Any]) -> tuple[ProfileExport, str]:
    """Экспорт и текст для промпта; ValueError — не экспорт schema_version 1."""
    export = ProfileExport.model_validate(payload)
    return export, render_for_prompt(export)


async def upsert(payload: dict[str, Any], source: str = "api") -> CompanyProfile:
    """Новый профиль или новая версия прежнего (по profile_id): снова активен."""
    export, prompt = parse(payload)
    async with SessionLocal() as session:
        row = await session.get(CompanyProfile, export.profile_id)
        if row is None:
            row = CompanyProfile(id=export.profile_id)
            session.add(row)
        row.name, row.company_id = display_name(export)[:300], export.company_id
        row.payload, row.prompt, row.source, row.active = export.model_dump(mode="json"), prompt, source, True
        await session.commit()
        await session.refresh(row)
    logger.info("профиль %s (%s): загружен из %s", row.id, row.name, source)
    return row


def fallback() -> Active:
    return Active(DEFAULT_ID, "COMPANY_PROFILE", settings.company_profile)


async def active() -> list[Active]:
    """Активные профили; без них — `default` из настроек, как до профилей."""
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(CompanyProfile).where(CompanyProfile.active.is_(True)).order_by(CompanyProfile.created_at)
        )).scalars().all()
    return [Active(r.id, r.name, r.prompt) for r in rows] or [fallback()]


# путь → mtime последней загруженной (или отвергнутой) версии файла
_seen: dict[str, float] = {}


async def sync_dir(folder: Path | None = None) -> int:
    """Загрузить новые и изменённые экспорты из PROFILES_DIR. Битый файл
    пишется в лог один раз на версию. Удалённый файл профиль не снимает —
    для этого DELETE /profiles/{id}."""
    folder = folder or settings.profiles_dir
    if not folder or not Path(folder).is_dir():
        return 0
    loaded = 0
    for path in sorted(Path(folder).glob("*.json")):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if _seen.get(str(path)) == mtime:
            continue
        _seen[str(path)] = mtime
        try:
            await upsert(json.loads(path.read_text(encoding="utf-8")), source="file")
            loaded += 1
        except (ValueError, OSError) as exc:
            logger.warning("профиль %s не загружен: %s", path.name, str(exc)[:300])
    return loaded
