from __future__ import annotations

import asyncio
import copy
import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from eventgraph import stories
from eventgraph.config import settings
from eventgraph.connectors.images import write_images
from eventgraph.connectors.normalize import Draft, ResultError, finalize_ids, jsonable, parse
from eventgraph.connectors.runtime import Context
from eventgraph.db import SessionLocal
from eventgraph.models import Connector, ConnectorRun, Item
from eventgraph.sources import BY_NAME, Source
from eventgraph.uid import new_uid

logger = logging.getLogger(__name__)

TRACE_LIMIT = 6000


@dataclass
class Outcome:
    status: str = "ok"  # ok | error | timeout
    error: str = ""
    log: str = ""
    total: int = 0
    drafts: list[Draft] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _known_ids(connector_id: int, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    async with SessionLocal() as session:
        rows = await session.execute(
            select(Item.external_id).where(
                Item.connector_id == connector_id, Item.external_id.in_(ids)
            )
        )
        return set(rows.scalars().all())


def _collect_in_thread(
    *,
    source: Source,
    state: dict[str, Any],
    last_run_at: datetime | None,
    connector_id: int,
    test: bool,
    main_loop: asyncio.AbstractEventLoop,
) -> Outcome:
    async def body() -> Outcome:
        ctx = Context(config=copy.deepcopy(source.config), state=state, name=source.name, last_run_at=last_run_at)
        outcome = Outcome()
        try:
            result = await source.collect(ctx)
            drafts = await finalize_ids(parse(result), ctx.http, settings.item_image_max_bytes)
            if not source.images:
                # После ключей: записи без id хешируются вместе с картинками,
                # и выключенный флаг не должен делать из старых записей новые.
                for draft in drafts:
                    draft.drop_images()
            outcome.total = len(drafts)
            if test:
                keep = drafts[: settings.connector_preview]
            else:
                ids = [d.external_id for d in drafts if d.external_id]
                known = await asyncio.wrap_future(
                    asyncio.run_coroutine_threadsafe(_known_ids(connector_id, ids), main_loop)
                )
                keep = [d for d in drafts if d.external_id not in known]
            # Картинки качаем только тем, кого сохраним или покажем.
            for draft in keep:
                await draft.load_images(ctx.http, settings.item_image_max_bytes)
                for problem in draft.image_errors:
                    ctx.log(f"картинка записи {draft.external_id}: {problem}")
            outcome.drafts = keep
            outcome.state = jsonable(ctx.state)
        except ResultError as exc:
            outcome.status, outcome.error = "error", f"Неверный результат: {exc}"
        except Exception:  # noqa: BLE001 — ошибка коннектора и есть результат запуска
            outcome.status, outcome.error = "error", traceback.format_exc()[-TRACE_LIMIT:]
        finally:
            outcome.log = ctx.text_log
            await ctx.aclose()
        return outcome

    return asyncio.run(body())


async def sync() -> dict[str, int]:
    """Строки состояния для источников из sources.py — у кого их ещё нет.
    Возвращает имя → id коннектора. Источник, убранный из кода, оставляет
    свою строку и записи, но больше не запускается."""
    async with SessionLocal() as session:
        if BY_NAME:
            await session.execute(
                insert(Connector)
                .values([{"name": name, "state": {}} for name in BY_NAME])
                .on_conflict_do_nothing(index_elements=[Connector.name])
            )
            await session.commit()
        rows = (await session.execute(
            select(Connector.name, Connector.id).where(Connector.name.in_(list(BY_NAME)))
        )).all()
    return {name: connector_id for name, connector_id in rows}


async def execute(run_id: int) -> None:
    """Исполняет запуск, уже переведённый воркером в running."""
    async with SessionLocal() as session:
        run = await session.get(ConnectorRun, run_id)
        if run is None:
            return
        connector = await session.get(Connector, run.connector_id)
        source = BY_NAME.get(connector.name) if connector is not None else None
        if source is None:
            run.status, run.error, run.finished_at = "error", "Источника нет в sources.py", _now()
            await session.commit()
            return
        state, last_run_at = connector.state or {}, connector.last_run_at

    test = run.kind == "test"
    try:
        outcome = await asyncio.wait_for(
            asyncio.to_thread(
                _collect_in_thread,
                source=source,
                state=copy.deepcopy(state),
                last_run_at=last_run_at,
                connector_id=run.connector_id,
                test=test,
                main_loop=asyncio.get_running_loop(),
            ),
            timeout=settings.connector_timeout,
        )
    except asyncio.TimeoutError:
        # Поток прервать нельзя — он доработает вхолостую, результат выброшен.
        outcome = Outcome(status="timeout", error=f"collect не уложилась в {settings.connector_timeout} с")

    new = 0
    if outcome.status == "ok" and not test:
        new = await _store(run.connector_id, outcome.drafts)
        if new:
            # Сюжеты — сразу, до разбора: агент увидит события сюжета, а повтор
            # разобранного закроется, не дойдя до агента.
            try:
                await stories.organize()
            except Exception:  # noqa: BLE001 — неразмещённое доберёт воркер перед разбором
                logger.exception("сюжеты после запуска коннектора %s", source.name)

    async with SessionLocal() as session:
        run = await session.get(ConnectorRun, run_id)
        if run is None:
            return
        run.status = outcome.status
        run.error = outcome.error
        run.log = outcome.log
        run.items_total = outcome.total
        run.finished_at = _now()
        if test:
            # Пробный запуск ничего не пишет и курсор не двигает.
            run.preview = [d.preview() for d in outcome.drafts]
        else:
            run.items_new = new
            run.items_dup = outcome.total - new
            connector = await session.get(Connector, run.connector_id)
            if connector is not None:
                connector.last_run_at = run.finished_at
                connector.last_status = outcome.status
                connector.last_error = outcome.error[-2000:]
                # Курсор сдвигается только вместе с сохранёнными записями.
                if outcome.status == "ok":
                    connector.state = outcome.state
        await session.commit()
    logger.info(
        "коннектор %s, запуск %s: %s, всего %s, новых %s",
        source.name, run_id, outcome.status, outcome.total, new,
    )


async def _store(connector_id: int, drafts: list[Draft]) -> int:
    """Пишет картинки и записи; возвращает число реально новых."""
    if not drafts:
        return 0
    when = _now()
    rows: list[dict[str, Any]] = []
    for draft in drafts:
        uid = new_uid(when.timestamp())
        paths = await asyncio.to_thread(write_images, settings.data_dir, uid, when, draft.images)
        data = dict(draft.data)
        if draft.image_errors:
            data["_image_errors"] = draft.image_errors
        rows.append(
            {
                "uid": uid,
                "connector_id": connector_id,
                "external_id": draft.external_id,
                "text": draft.text,
                "images": paths,
                "data": data,
                "collected_at": when,
            }
        )
    async with SessionLocal() as session:
        # Дубли уже отсеяны, но между проверкой и вставкой мог пройти другой
        # запуск — уникальный ключ досчитывает честно.
        result = await session.execute(
            insert(Item).values(rows).on_conflict_do_nothing(
                constraint="uq_items_connector_external"
            ).returning(Item.id)
        )
        new = len(result.scalars().all())
        await session.commit()
    return new
