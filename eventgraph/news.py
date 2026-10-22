from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from eventgraph import analyst, profiles, stories
from eventgraph.agents import runner
from eventgraph.config import settings
from eventgraph.db import SessionLocal
from eventgraph.models import Connector, Item
from eventgraph.sources import BY_NAME

if TYPE_CHECKING:
    from eventgraph.graph_settings import GraphSettings

logger = logging.getLogger(__name__)

VERDICTS = ("stored", "merged", "skipped")
_FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def configured() -> bool:
    return settings.agent_on("news")


def _field(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value:
            return str(value).strip()
    return ""


def render(item: Item, connector: str, now: datetime, item_chars: int,
           pictures: list[str] | None = None, events: list[str] | None = None,
           task: str = "Верни вердикт по записи.") -> str:
    """Сообщение агенту: дата и запись в формате, который описан в его промпте.

    `pictures` — имена картинок записи во вложениях рана; `events` — события
    графа, в которые уже разобраны другие записи её сюжета; `task` — что
    сделать с записью (аналитику — оценить, а не разобрать)."""
    parts = [f"Сегодня: {now:%Y-%m-%d %H:%M} UTC.", task]
    if pictures:
        # Каждый шаг заново отправляет модели весь разговор: картинки по одной
        # за шаг однажды превратили 8 шагов в 30 и утроили токены.
        parts.append("Картинки, которые нужны, открой все сразу — одним шагом, параллельными "
                     "вызовами view_image, а не по одной.")
    if events:
        parts.append("Сюжет: в графе по нему уже есть " + "; ".join(f"«{e}»" for e in events[:5]))
    data = item.data or {}
    published = _field(data, "published", "published_at", "date") or f"собрано {item.collected_at:%Y-%m-%d %H:%M}"
    url = _field(data, "url", "link")
    title = _field(data, "title")
    text = (item.text or "").strip()
    if len(text) > item_chars:
        text = text[:item_chars].rstrip() + " …[обрезано]"
    head = f"### {item.uid} · {connector} · {published}" + (f" · {url}" if url else "")
    source = _field(data, "source", "site")
    body = "\n".join(p for p in (title if title and not text.startswith(title) else "", text) if p)
    attached = f"картинки: {', '.join(pictures)}\n" if pictures else ""
    parts.append(f"{head}\n" + attached + (f"[{source}]\n" if source else "") + body)
    return "\n\n".join(parts)


def parse_verdicts(output: str, uids: set[str]) -> dict[str, dict[str, str]]:
    """uid → {verdict, event, reason} из последнего JSON-блока ответа.

    Берётся последний блок, в котором есть `items`: в сводке над ним агент
    может процитировать что-нибудь в фигурных скобках. Чужие uid, неизвестные
    вердикты и битый JSON отбрасываются молча — такая запись просто разберётся
    ещё раз."""
    candidates = _FENCED.findall(output or "")
    if not candidates:
        start = (output or "").rfind('{"items"')
        if start >= 0:
            candidates = [output[start:]]
    for raw in reversed(candidates):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            continue
        out: dict[str, dict[str, str]] = {}
        for entry in data["items"]:
            if not isinstance(entry, dict):
                continue
            uid = str(entry.get("uid", "")).strip()
            verdict = str(entry.get("verdict", "")).strip().lower()
            if uid not in uids or verdict not in VERDICTS:
                continue
            out[uid] = {
                "verdict": verdict,
                **{k: str(entry[k])[:500] for k in ("event", "reason") if entry.get(k)},
            }
        return out
    return {}


def strip_verdicts(output: str) -> str:
    """Ответ агента без JSON-блока с вердиктом — то, что читает человек."""
    text = output or ""
    blocks = list(_FENCED.finditer(text))
    if blocks and '"items"' in blocks[-1].group(1):
        text = text[: blocks[-1].start()]
    else:
        cut = text.rfind('{"items"')
        if cut >= 0:
            text = text[:cut]
    return text.strip()


def settle(item: Item, found: dict[str, str] | None, run_id: int, now: datetime, error: str = "") -> str:
    """Итог записи после рана: done | retry | failed."""
    item.agent_run_id = run_id
    if found is not None:
        item.processed_at = now
        item.verdict = found["verdict"]
        item.outcome = {k: v for k, v in found.items() if k != "verdict"} or None
        return "done"
    item.attempts = (item.attempts or 0) + 1
    if item.attempts >= settings.news_max_attempts:
        item.processed_at = now
        item.verdict = "failed"
        item.outcome = {"reason": error or "агент не вернул вердикт"}
        return "failed"
    return "retry"


def image_files(item: Item, per_item: int) -> list[tuple[str, str]]:
    """(имя для агента, путь от каталога данных) первых `per_item` картинок
    записи — только тех, что лежат на диске."""
    out = []
    for n, rel in enumerate((item.images or [])[:per_item]):
        path = settings.data_dir / rel
        if path.is_file():
            out.append((f"{item.uid}-{n}{path.suffix}", rel))
    return out


def run_stats(row: Any) -> dict[str, int]:
    """Шаги, вызовы, токены и время рана агента."""
    return {
        "steps": int(row.steps or 0),
        "tool_calls": int(row.tool_calls or 0),
        "tokens": int(row.tokens_prompt or 0) + int(row.tokens_completion or 0),
        "duration_ms": int(row.duration_ms or 0),
    }


def run_result(row: Any) -> tuple[str, str]:
    """(ответ, ошибка) рана: ответ есть только у законченного рана."""
    output = (row.output or "") if row.status == "done" else ""
    error = "" if output else (row.error or f"ран завершился: {row.status}")
    return output, error


def _pending() -> list[Any]:
    return [Item.processed_at.is_(None), Item.attempts < settings.news_max_attempts]


async def queue_length() -> int:
    """Сколько записей ждут разбора."""
    async with SessionLocal() as session:
        return int(await session.scalar(select(func.count()).select_from(Item).where(*_pending())) or 0)


async def next_item() -> tuple[Item, str] | None:
    """Самая старая неразобранная запись и имя её источника. Записи одного
    сюжета так идут подряд, и каждая следующая видит события предыдущих."""
    async with SessionLocal() as session:
        row = (await session.execute(
            select(Item, Connector.name)
            .join(Connector, Connector.id == Item.connector_id)
            .where(*_pending())
            .order_by(Item.collected_at, Item.id)
            .limit(1)
        )).first()
    if row is None:
        return None
    item, name = row
    # Источник с выключенными картинками (sources.py): и накопленные раньше
    # уходят агенту без них. Сессия закрыта — в базе картинки остаются.
    source = BY_NAME.get(name)
    if source is not None and not source.images:
        item.images = []
    return item, name


async def story_events(item: Item) -> list[str]:
    """События графа, в которые уже разобраны другие записи сюжета записи."""
    if not item.story_id:
        return []
    async with SessionLocal() as session:
        outcomes = (await session.execute(
            select(Item.outcome).where(
                Item.story_id == item.story_id, Item.id != item.id, Item.verdict.in_(stories.IN_GRAPH)
            ).order_by(Item.processed_at)
        )).scalars().all()
    return list(dict.fromkeys(str(o["event"]) for o in outcomes if o and o.get("event")))


async def process(item: Item, connector: str, cfg: "GraphSettings") -> dict[str, Any]:
    """Одна запись — один ран агента. Итог: вердикт (или retry / failed),
    ошибка и `ok` — дошёл ли ран до ответа. Ран без ответа (модель недоступна,
    ран упал) — повод для паузы: следующая запись, скорее всего, упадёт так же.

    До агента новостей запись оценивает аналитик (analyst.py) — для каждого
    активного профиля клиента (profiles.py): в графе ещё нет этой записи,
    только прошлые новости. Уже сделанные оценки не повторяются."""
    files = image_files(item, cfg.item_images) if cfg.item_images > 0 and settings.llm_vision else []
    pictures, events = [name for name, _ in files], await story_events(item)
    clients = [c for c in await profiles.active() if c.wants(connector)]
    if not clients:
        return await _below_threshold(item, connector, None, "ни один клиент не выбрал этот источник", None)
    assessments, fresh, assessed = analyst.by_profile(item.analysis), False, None
    if analyst.configured():
        message = None
        for client in clients:
            if (assessments.get(client.id) or {}).get("impact"):
                continue
            message = message or render(item, connector, _now(), settings.news_item_chars, pictures, events,
                                        task=analyst.TASK)
            assessments[client.id], assessed = await analyst.assess(message, item.uid, dict(files), client)
            fresh = True
    analysis = analyst.combine(assessments) if fresh else None
    if reason := analyst.below_all(assessments, [c.id for c in clients], cfg.min_importance):
        return await _below_threshold(item, connector, analysis, reason, assessed)
    message = render(item, connector, _now(), settings.news_item_chars, pictures, events)
    run_id = await runner.create("news", message, attachments=dict(files))
    row = await runner.execute(run_id)
    output, error = run_result(row)
    found = parse_verdicts(output, {item.uid}).get(item.uid)
    if output and found is None:
        error = "агент не вернул вердикт по записи"
    async with SessionLocal() as session:
        stored = await session.get(Item, item.id)
        if analysis is not None:
            stored.analysis = analysis
        result = settle(stored, found, run_id, _now(), error)
        verdict = stored.verdict
        await session.commit()
    logger.info("запись %s (%s): %s, ран %s%s", item.uid, connector, verdict or result, run_id,
                f" — {error}" if error else "")
    return {"uid": item.uid, "run_id": run_id, "result": result, "verdict": verdict, "error": error,
            "ok": bool(output), "importance": (analysis or item.analysis or {}).get("importance"),
            **run_stats(row)}


async def _below_threshold(item: Item, connector: str, analysis: dict[str, Any] | None, reason: str,
                           row: Any) -> dict[str, Any]:
    """Запись ниже порога важности: закрыта без агента новостей и графа."""
    async with SessionLocal() as session:
        stored = await session.get(Item, item.id)
        if analysis is not None:
            stored.analysis = analysis
        stored.processed_at = _now()
        stored.verdict = analyst.BELOW
        stored.outcome = {"reason": reason}
        stored.agent_run_id = (stored.analysis or {}).get("run_id")
        await session.commit()
    logger.info("запись %s (%s): %s — %s", item.uid, connector, analyst.BELOW, reason)
    stats = run_stats(row) if row is not None else {"steps": 0, "tool_calls": 0, "tokens": 0, "duration_ms": 0}
    return {"uid": item.uid, "run_id": stored.agent_run_id, "result": "done", "verdict": analyst.BELOW, "error": "",
            "ok": True, "importance": (stored.analysis or {}).get("importance"), **stats}


async def process_next(cfg: "GraphSettings") -> dict[str, Any] | None:
    """Разобрать следующую запись очереди. None — очередь пуста."""
    found = await next_item()
    if found is None:
        return None
    return await process(*found, cfg)
