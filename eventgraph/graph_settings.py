from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from eventgraph.config import settings
from eventgraph.models import AppSetting

KEY = "graph"
# Просьбы через API и состояние разбора: {"maintain", "stop", "failures", "paused_until"}.
CONTROL = "graph_control"
MAINTENANCE_TASKS = ("duplicates", "islands", "prune")
# Пауза разбора после рана без вердикта: 30 с, дальше вдвое, но не дольше 15 минут.
PAUSE_FIRST, PAUSE_MAX = 30, 15 * 60


@dataclass
class GraphSettings:
    # Разбирать ли новые записи. Выключено — записи копятся и ждут.
    enabled: bool = True
    # Часовой пояс дней в /graph/stats.
    timezone: str = "Asia/Tomsk"
    item_images: int = field(default_factory=lambda: settings.news_item_images)
    # Порог важности для компании (analyst.py): запись с оценкой ниже
    # закрывается как below_threshold — без агента новостей и без графа.
    # 0 — порога нет; запись без оценки (аналитик выключен или сорвался)
    # разбирается как обычно.
    min_importance: int = field(default_factory=lambda: settings.analyst_min_importance)
    # Обслуживание графа садовником (gardening.py): не чаще раза в
    # `maintenance_every_days` дней, между записями.
    maintenance_enabled: bool = True
    maintenance_every_days: int = 7
    maintenance_tasks: list[str] = field(default_factory=lambda: list(MAINTENANCE_TASKS))
    # Сколько пар-кандидатов в дубли и островков разбирать за раз.
    merge_pairs: int = 30
    island_limit: int = 30
    # Обрезка: события старше стольких дней, важностью не выше и не больше чем
    # из стольких источников. Включённая обрезка удаляет; садовник лишь
    # вычёркивает из списка то, что стоит оставить.
    prune_older_days: int = 30
    prune_max_importance: int = 2
    prune_max_sources: int = 1
    prune_protect_topics: list[str] = field(default_factory=lambda: ["Томск"])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "GraphSettings":
        base = cls()
        known = {k: v for k, v in (data or {}).items() if k in base.to_dict()}
        return cls(**{**base.to_dict(), **known})

    def validate(self) -> str:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return f"Неизвестный часовой пояс: {self.timezone}"
        if not 0 <= int(self.item_images) <= 10:
            return "Картинок на запись — от 0 до 10"
        if not 0 <= int(self.min_importance) <= 5:
            return "Порог важности для компании — от 0 до 5"
        if not 1 <= int(self.maintenance_every_days) <= 90:
            return "Обслуживание — раз в 1…90 дней"
        bad = [t for t in self.maintenance_tasks if t not in MAINTENANCE_TASKS]
        if bad:
            return f"Задачи обслуживания — из: {', '.join(MAINTENANCE_TASKS)}"
        if not 1 <= int(self.merge_pairs) <= 60 or not 1 <= int(self.island_limit) <= 60:
            return "Пар и островков за раз — от 1 до 60"
        if not 1 <= int(self.prune_older_days) <= 3650:
            return "Обрезка — события старше 1…3650 дней"
        if not 1 <= int(self.prune_max_importance) <= 5:
            return "Важность для обрезки — от 1 до 5"
        if not 0 <= int(self.prune_max_sources) <= 100:
            return "Источников для обрезки — от 0 до 100"
        return ""


def pause_after(failures: int, now: datetime) -> datetime:
    """До какого момента не разбирать, если `failures` ранов подряд не дали вердикта."""
    seconds = min(PAUSE_FIRST * 2 ** max(0, failures - 1), PAUSE_MAX)
    return now + timedelta(seconds=seconds)


def paused_until(control: dict[str, Any]) -> datetime | None:
    value = control.get("paused_until")
    return datetime.fromisoformat(value) if value else None


async def load(session: AsyncSession) -> GraphSettings:
    row = await session.get(AppSetting, KEY)
    return GraphSettings.from_dict(row.value if row else None)


async def save(session: AsyncSession, cfg: GraphSettings) -> None:
    await _put(session, KEY, cfg.to_dict())


async def control(session: AsyncSession) -> dict[str, Any]:
    row = await session.get(AppSetting, CONTROL)
    return {"maintain": False, "stop": False, "failures": 0, "paused_until": None, **(row.value if row else {})}


async def set_control(session: AsyncSession, **values: Any) -> None:
    current = await control(session)
    current.update(values)
    await _put(session, CONTROL, current)


async def _put(session: AsyncSession, key: str, value: Any) -> None:
    await session.execute(
        insert(AppSetting)
        .values(key=key, value=value, updated_at=datetime.now(timezone.utc))
        .on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={"value": value, "updated_at": datetime.now(timezone.utc)},
        )
    )
    await session.commit()
