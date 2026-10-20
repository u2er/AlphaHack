from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from sqlalchemy import String, func, literal, select, update

from eventgraph import news
from eventgraph.agents import runner
from eventgraph.config import settings
from eventgraph.db import SessionLocal
from eventgraph.models import Item, MaintenanceRun, MaintenanceTask

if TYPE_CHECKING:
    from eventgraph.graph_settings import GraphSettings

logger = logging.getLogger(__name__)

# Задача — как она называется в сообщении садовнику (см. prompts/gardener.md).
TASK_NAMES = {"duplicates": "дубли", "islands": "островки", "prune": "обрезка"}
COUNTS = ("rejected", "left", "linked", "checked", "pruned")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def configured() -> bool:
    return settings.agent_on("gardener")


def due(cfg: "GraphSettings", since: datetime | None, now: datetime) -> bool:
    """Пора ли обслуживать граф. `since` — начало прошлого обслуживания, а без
    него — первая разобранная запись: пустой граф обслуживать незачем."""
    if not configured() or not cfg.maintenance_enabled or not cfg.maintenance_tasks or since is None:
        return False
    return now - since >= timedelta(days=int(cfg.maintenance_every_days))


def task_message(task: str, cfg: "GraphSettings") -> str:
    """Первая строка — задача и её лимиты; правила задачи — в промпте садовника."""
    if task == "duplicates":
        return f"Задача: дубли. Лимит: {cfg.merge_pairs} пар (graph_duplicates limit={cfg.merge_pairs})."
    if task == "islands":
        return f"Задача: островки. Лимит: {cfg.island_limit} (graph_islands limit={cfg.island_limit})."
    params = (
        f"older_than_days={cfg.prune_older_days}, max_importance={cfg.prune_max_importance}, "
        f"max_sources={cfg.prune_max_sources}, "
        f"protect_topics={json.dumps(list(cfg.prune_protect_topics), ensure_ascii=False)}"
    )
    return f"Задача: обрезка. Параметры graph_prune: {params}."


def _int(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def parse_report(output: str) -> dict[str, Any] | None:
    """Итог садовника из последнего JSON-блока ответа; None — блока нет.

    Берётся последний блок с `task` или `merged`: выше агент может
    процитировать что-нибудь в фигурных скобках."""
    for raw in reversed(news._FENCED.findall(output or "")):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(data, dict) or not ({"task", "merged"} & data.keys()):
            continue
        merged = [
            {"kept": str(m["kept"]).strip()[:500], "dropped": str(m["dropped"]).strip()[:500]}
            for m in data.get("merged") or []
            if isinstance(m, dict) and m.get("kept") and m.get("dropped")
        ]
        deleted = [str(d).strip()[:500] for d in data.get("deleted") or [] if str(d).strip()]
        return {
            "merged": merged,
            "deleted": deleted,
            **{key: _int(data.get(key)) for key in COUNTS},
        }
    return None


def strip_report(output: str) -> str:
    """Ответ садовника без JSON-блока — то, что читает человек."""
    text = output or ""
    blocks = list(news._FENCED.finditer(text))
    if blocks:
        try:
            data = json.loads(blocks[-1].group(1))
        except ValueError:
            data = None
        if isinstance(data, dict) and {"task", "merged"} & data.keys():
            text = text[: blocks[-1].start()]
    return text.strip()


async def rename_events(merged: list[dict[str, str]]) -> int:
    """Записи, разобранные в слитое событие, теперь указывают на оставшееся.

    Цепочки (A слито в B, B — в C) проходятся по порядку слияний."""
    renamed = 0
    async with SessionLocal() as session:
        for pair in merged:
            if pair["kept"] == pair["dropped"]:
                continue
            result = await session.execute(
                update(Item)
                .where(Item.outcome["event"].astext == pair["dropped"])
                .values(outcome=Item.outcome.op("||")(func.jsonb_build_object("event", literal(pair["kept"], String))))
                .returning(Item.id)
            )
            renamed += len(result.scalars().all())
        await session.commit()
    return renamed


async def last_start() -> datetime | None:
    async with SessionLocal() as session:
        return await session.scalar(select(func.max(MaintenanceRun.started_at)))


async def reference() -> datetime | None:
    """От чего считать срок обслуживания: прошлое обслуживание, иначе первая
    разобранная агентом запись."""
    last = await last_start()
    if last is not None:
        return last
    async with SessionLocal() as session:
        return await session.scalar(select(func.min(Item.processed_at)).where(Item.agent_run_id.is_not(None)))


async def _update_task(task_pk: int, **values: Any) -> None:
    async with SessionLocal() as session:
        row = await session.get(MaintenanceTask, task_pk)
        if row is not None:
            for key, value in values.items():
                setattr(row, key, value)
            await session.commit()


async def _update_run(run_pk: int, **values: Any) -> None:
    async with SessionLocal() as session:
        row = await session.get(MaintenanceRun, run_pk)
        if row is not None:
            for key, value in values.items():
                setattr(row, key, value)
            await session.commit()


async def recover() -> None:
    """Обслуживание, которое шло у прошлого процесса воркера, уже не доделается."""
    async with SessionLocal() as session:
        for model in (MaintenanceRun, MaintenanceTask):
            await session.execute(
                update(model).where(model.status == "running")
                .values(status="stopped", finished_at=_now(), error="воркер перезапущен посреди обслуживания")
            )
        await session.commit()


def _empty_report() -> dict[str, Any]:
    return {"tasks": {}, "merged": [], "deleted": [], "renamed": 0, **dict.fromkeys(COUNTS, 0)}


async def run_task(task: str, cfg: "GraphSettings", run_pk: int) -> dict[str, Any]:
    """Одна задача — один ран садовника. Итог: отчёт задачи (или None),
    статистика рана и `error`."""
    async with SessionLocal() as session:
        row = MaintenanceTask(run_id=run_pk, task=task, started_at=_now())
        session.add(row)
        await session.commit()
        task_pk = row.id
    run_id = await runner.create("gardener", task_message(task, cfg))
    await _update_task(task_pk, agent_run_id=run_id)
    row = await runner.execute(run_id)
    output, error = news.run_result(row)
    report = parse_report(output)
    if output and report is None:
        error = "садовник не вернул итог JSON-блоком"
    stats = news.run_stats(row)
    await _update_task(
        task_pk,
        status="done" if report is not None else "error",
        finished_at=_now(),
        summary=strip_report(output)[:20_000],
        merged=len(report["merged"]) if report else 0,
        error=error[:4000],
        **stats,
    )
    return {"report": report, "error": error, **stats}


async def run_maintenance(trigger: str, cfg: "GraphSettings",
                          should_stop: Callable[[], Awaitable[bool]] | None = None) -> int | None:
    """Прогон обслуживания: задачи по очереди. Возвращает id прогона (None —
    садовник не настроен)."""
    if not configured():
        return None
    async with SessionLocal() as session:
        run = MaintenanceRun(trigger=trigger, status="running", started_at=_now())
        session.add(run)
        await session.commit()
        run_pk = run.id

    report = _empty_report()
    totals = {"tasks": 0, "tool_calls": 0, "tokens": 0}
    status, errors = "done", []
    try:
        for task in [t for t in TASK_NAMES if t in cfg.maintenance_tasks]:
            if should_stop is not None and await should_stop():
                status = "stopped"
                break
            result = await run_task(task, cfg, run_pk)
            totals["tasks"] += 1
            totals["tool_calls"] += int(result.get("tool_calls") or 0)
            totals["tokens"] += int(result.get("tokens") or 0)
            if result["error"]:
                errors.append(f"{TASK_NAMES[task]}: {result['error']}")
            got = result["report"]
            if got is not None:
                report["tasks"][task] = {k: v for k, v in got.items() if k not in ("merged", "deleted")} | {
                    "merged": len(got["merged"]), "deleted": len(got["deleted"]),
                }
                report["merged"] += got["merged"]
                report["deleted"] += got["deleted"]
                for key in COUNTS:
                    report[key] += got[key]
                if got["merged"]:
                    report["renamed"] += await rename_events(got["merged"])
            await _save(run_pk, report, merged=len(report["merged"]), **totals)
    except Exception as exc:  # noqa: BLE001 — итог прогона пишем в любом случае
        logger.exception("обслуживание графа %s упало", run_pk)
        status = "error"
        errors.append(f"{type(exc).__name__}: {exc}")
    if errors and status == "done" and not report["tasks"]:
        status = "error"
    await _save(run_pk, report, merged=len(report["merged"]), status=status, finished_at=_now(),
                error="; ".join(errors)[:4000], **totals)
    logger.info(
        "обслуживание графа %s: %s, слито %s, отклонено %s, прикреплено %s, удалено %s, обрезано %s",
        run_pk, status, len(report["merged"]), report["rejected"], report["linked"],
        len(report["deleted"]), report["pruned"],
    )
    return run_pk


async def _save(run_pk: int, report: dict[str, Any], **values: Any) -> None:
    # JSONB-поле меняется целиком: копия, иначе SQLAlchemy не заметит правки.
    await _update_run(run_pk, report=json.loads(json.dumps(report)), **values)
