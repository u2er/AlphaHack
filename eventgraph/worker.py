from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from eventgraph import gardening, graph_settings, news, stories
from eventgraph.agents import runner
from eventgraph.config import settings
from eventgraph.connectors import service
from eventgraph.db import SessionLocal, create_schema
from eventgraph.logging_config import setup_logging
from eventgraph.models import Connector, ConnectorRun
from eventgraph.sources import SOURCES

logger = logging.getLogger("eventgraph.worker")

TICK = 2.0
# Сколько коннекторов исполняется одновременно.
PARALLEL = 4


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Worker:
    def __init__(self) -> None:
        self._active: dict[int, asyncio.Task] = {}  # run_id → задача
        self._busy_connectors: set[int] = set()
        self._graph_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        await create_schema()
        await service.sync()
        await self._recover()
        await gardening.recover()
        lost = await runner.recover()
        if lost:
            logger.info("раны агентов прерваны перезапуском: %s", lost)
        agents = ", ".join(sorted(settings.enabled_agents)) or "нет (нужен LLM_API_KEY)"
        sources = ", ".join(s.name for s in SOURCES if s.enabled) or "нет"
        logger.info("воркер запущен, источники: %s, агенты: %s", sources, agents)

    async def _recover(self) -> None:
        """Всё, что было в работе у прошлого процесса, уже не доделается."""
        async with SessionLocal() as session:
            await session.execute(
                update(ConnectorRun)
                .where(ConnectorRun.status == "running")
                .values(status="error", error="Воркер перезапущен посреди запуска", finished_at=_now())
            )
            await session.commit()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        await self.start()
        while not self._stop.is_set():
            try:
                await self._schedule_due()
                await self._claim_runs()
                self._schedule_graph()
            except Exception:  # noqa: BLE001 — сбой одного тика не валит воркер
                logger.exception("сбой тика воркера")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK)
            except asyncio.TimeoutError:
                pass
        await self._drain()

    async def _drain(self) -> None:
        # Разбор записи не дожидаемся: ран идёт минутами. Запись без вердикта
        # разберётся заново — агент сверяет с графом, так что повтор даст
        # merged, а не дубль.
        if self._graph_task and not self._graph_task.done():
            self._graph_task.cancel()
        pending = [t for t in self._active.values() if not t.done()]
        if pending:
            logger.info("жду %s незаконченных задач", len(pending))
            await asyncio.gather(*pending, return_exceptions=True)

    # ── расписание ───────────────────────────────────────────────────────────

    async def _schedule_due(self) -> None:
        """Запуски по расписанию источников из sources.py."""
        scheduled = {s.name: s for s in SOURCES if s.enabled and s.interval_seconds}
        if not scheduled:
            return
        now = _now()
        async with SessionLocal() as session:
            due = (
                await session.execute(
                    select(Connector).where(
                        Connector.name.in_(list(scheduled)),
                        (Connector.next_run_at.is_(None)) | (Connector.next_run_at <= now),
                    )
                )
            ).scalars().all()
            if not due:
                return
            waiting = set(
                (
                    await session.execute(
                        select(ConnectorRun.connector_id).where(
                            ConnectorRun.connector_id.in_([c.id for c in due]),
                            ConnectorRun.status.in_(("queued", "running")),
                            ConnectorRun.kind != "test",
                        )
                    )
                ).scalars().all()
            )
            for connector in due:
                # Следующий срок считаем от «сейчас»: пропущенные за время
                # простоя запуски не догоняются пачкой.
                connector.next_run_at = now + timedelta(seconds=scheduled[connector.name].interval_seconds)
                if connector.id in waiting:
                    continue
                session.add(ConnectorRun(connector_id=connector.id, kind="scheduled"))
            await session.commit()

    # ── запуски ──────────────────────────────────────────────────────────────

    async def _claim_runs(self) -> None:
        free = PARALLEL - len(self._active)
        if free <= 0:
            return
        async with SessionLocal() as session:
            queued = (
                await session.execute(
                    select(ConnectorRun)
                    .where(ConnectorRun.status == "queued")
                    .order_by(ConnectorRun.id)
                    .limit(50)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            claimed: list[ConnectorRun] = []
            for run in queued:
                if len(claimed) >= free:
                    break
                # Один запуск коннектора за раз, иначе два запуска прочтут
                # один курсор. Пробные курсор не трогают и этим не ограничены.
                if run.kind != "test" and run.connector_id in self._busy_connectors:
                    continue
                run.status = "running"
                run.started_at = _now()
                claimed.append(run)
                if run.kind != "test":
                    self._busy_connectors.add(run.connector_id)
            await session.commit()

        for run in claimed:
            task = asyncio.create_task(self._execute(run.id, run.connector_id, run.kind))
            self._active[run.id] = task

    async def _execute(self, run_id: int, connector_id: int, kind: str) -> None:
        try:
            await service.execute(run_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("запуск %s упал вне коннектора", run_id)
            async with SessionLocal() as session:
                run = await session.get(ConnectorRun, run_id)
                if run is not None and run.status == "running":
                    run.status, run.error, run.finished_at = "error", f"Сбой воркера: {exc}", _now()
                    await session.commit()
        finally:
            self._active.pop(run_id, None)
            if kind != "test":
                self._busy_connectors.discard(connector_id)

    # ── граф: разбор записей и обслуживание ──────────────────────────────────

    def _schedule_graph(self) -> None:
        """Одна задача на граф: записи разбираются по одной, а обслуживание
        идёт между ними — слияние посреди разбора вернуло бы слитый узел его
        же записью. Новая запись подхватывается на следующем тике."""
        if not news.configured() and not gardening.configured():
            return
        if self._graph_task and not self._graph_task.done():
            return
        self._graph_task = asyncio.create_task(self._graph())

    async def _graph(self) -> None:
        try:
            if await self._maintain_if_due():
                return
            async with SessionLocal() as session:
                cfg = await graph_settings.load(session)
                control = await graph_settings.control(session)
            if not news.configured() or not cfg.enabled:
                return
            paused = graph_settings.paused_until(control)
            if paused is not None and paused > _now():
                return
            # Сюжеты для записей, которые при сборе не получили вектора.
            await stories.organize()
            while not self._stop.is_set():
                result = await news.process_next(cfg)
                if result is None:
                    return
                await self._after_record(result)
                if not result["ok"] or await self._maintain_requested():
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — сбой разбора не валит воркер
            logger.exception("разбор записей упал")

    async def _after_record(self, result: dict) -> None:
        """Ран без ответа — пауза, и чем дольше сбой, тем длиннее; первый
        удачный ран её снимает."""
        async with SessionLocal() as session:
            control = await graph_settings.control(session)
            if result["ok"]:
                if control["failures"] or control["paused_until"]:
                    await graph_settings.set_control(session, failures=0, paused_until=None)
                return
            failures = int(control["failures"] or 0) + 1
            until = graph_settings.pause_after(failures, _now())
            await graph_settings.set_control(session, failures=failures, paused_until=until.isoformat())
        logger.warning("разбор на паузе до %s: %s", until.isoformat(timespec="seconds"), result["error"])

    async def _maintain_requested(self) -> bool:
        async with SessionLocal() as session:
            return bool((await graph_settings.control(session))["maintain"])

    async def _maintain_if_due(self) -> bool:
        """Обслуживание — по просьбе или когда подошёл срок. True — оно было."""
        if not gardening.configured():
            return False
        async with SessionLocal() as session:
            cfg = await graph_settings.load(session)
            manual = (await graph_settings.control(session))["maintain"]
        if not manual and not gardening.due(cfg, await gardening.reference(), _now()):
            return False
        async with SessionLocal() as session:
            # Старая просьба остановиться не должна сорвать новое обслуживание.
            await graph_settings.set_control(session, maintain=False, stop=False)

        async def should_stop() -> bool:
            async with SessionLocal() as session:
                if (await graph_settings.control(session))["stop"]:
                    await graph_settings.set_control(session, stop=False)
                    return True
            return False

        await gardening.run_maintenance("manual" if manual else "schedule", cfg, should_stop=should_stop)
        return True


async def main() -> None:
    setup_logging()
    worker = Worker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
