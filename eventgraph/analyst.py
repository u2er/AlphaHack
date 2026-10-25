"""Аналитик: перед разбором записи оценивает её для каждого клиента.

Ран агента `analyst` на запись и профиль клиента (eventgraph/profiles.py) — до
агента новостей, так что в графе лежат только прошлые новости, и аналитик
сверяет запись с ними. Итог — `Item.analysis["profiles"][profile_id]`: важность
для клиента, как описанное может повлиять на его бизнес и сопутствующие поля
(схема — в конце `prompts/analyst.md`); верхние поля `Item.analysis` — самая
важная из оценок. Запись ниже порога (`GraphSettings.min_importance`) для всех
клиентов закрывается как `below_threshold` — агент новостей её не видит, в граф
она не попадает; важная хоть одному клиенту идёт в общий граф. Сбой аналитика
разбор не останавливает: запись уходит агенту новостей без оценки, а оценка
повторится на следующей попытке разбора, если она будет.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from eventgraph.agents import runner
from eventgraph.config import settings
from eventgraph.models import Item
from eventgraph.profiles import DEFAULT_ID, Active
from utils import tracing

logger = logging.getLogger(__name__)

_FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)

RELEVANCE = ("direct", "indirect", "none")
DIRECTION = ("risk", "opportunity", "mixed", "neutral")
HORIZON = ("immediate", "weeks", "months", "years")
TREND = ("new", "continues", "escalates", "eases")
AREAS = ("продукты", "клиенты", "конкуренты", "партнёры", "регуляторика", "финансы", "операции", "технологии",
         "персонал", "репутация")
ROLES = ("competitor", "partner", "client", "supplier", "regulator", "other")
TASK = "Оцени запись для компании."
BELOW = "below_threshold"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def configured() -> bool:
    return settings.agent_on("analyst")


def by_profile(analysis: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Оценки записи по профилям. Оценка, сделанная до профилей клиентов
    (без `profiles`), — оценка профиля `default`."""
    analysis = analysis or {}
    if isinstance(analysis.get("profiles"), dict):
        return dict(analysis["profiles"])
    return {DEFAULT_ID: dict(analysis)} if analysis else {}


def needed(item: Item, profile_id: str = DEFAULT_ID) -> bool:
    """Оценка нужна, если аналитик включён и для этого клиента её ещё нет:
    повторная попытка разбора не гоняет аналитика заново."""
    return configured() and not by_profile(item.analysis).get(profile_id, {}).get("impact")


def combine(assessments: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """`Item.analysis`: все оценки в `profiles`, сверху — самая важная с её
    `profile_id` (без удачных оценок — первая, то есть ошибка)."""
    if not assessments:
        return None
    scored = {pid: a for pid, a in assessments.items() if isinstance(a.get("importance"), int)}
    pid, top = max(scored.items(), key=lambda kv: kv[1]["importance"]) if scored else next(iter(assessments.items()))
    return {**top, "profile_id": pid, "profiles": assessments}


def below(analysis: dict[str, Any] | None, threshold: int) -> str:
    """Причина закрыть запись без разбора, если её важность ниже порога; иначе "".
    Без оценки порог не действует: сбой аналитика не должен терять новости."""
    importance = (analysis or {}).get("importance")
    if threshold <= 0 or not isinstance(importance, int) or importance >= threshold:
        return ""
    return f"важность для компании {importance} ниже порога {threshold}"


def below_all(assessments: dict[str, dict[str, Any]], profile_ids: list[str], threshold: int) -> str:
    """Причина закрыть запись: она ниже порога для каждого клиента. Хоть один
    клиент без оценки или с важностью не ниже порога — запись идёт в граф."""
    reasons = [below(assessments.get(pid), threshold) for pid in profile_ids]
    if not reasons or not all(reasons):
        return ""
    if len(reasons) == 1:
        return reasons[0]
    top = max(assessments[pid]["importance"] for pid in profile_ids)
    return f"важность ниже порога {threshold} для всех клиентов ({len(reasons)}), наибольшая — {top}"


def _choice(value: Any, allowed: tuple[str, ...]) -> str | None:
    value = str(value or "").strip().lower()
    return value if value in allowed else None


def _strings(value: Any, limit: int, chars: int = 300) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip()[:chars] for v in value if str(v or "").strip()][:limit]


def normalize(data: dict[str, Any]) -> dict[str, Any] | None:
    """Оценка из ответа модели: обязательны `importance` 0–5 и `impact`;
    необязательные поля с чужими значениями отбрасываются молча."""
    try:
        importance = int(data.get("importance"))
    except (TypeError, ValueError):
        return None
    impact = str(data.get("impact") or "").strip()
    if not 0 <= importance <= 5 or not impact:
        return None
    out: dict[str, Any] = {"importance": importance, "impact": impact[:2000]}
    for key, allowed in (("relevance", RELEVANCE), ("direction", DIRECTION), ("horizon", HORIZON), ("trend", TREND)):
        if (value := _choice(data.get(key), allowed)) is not None:
            out[key] = value
    if areas := [a for a in _strings(data.get("areas"), 10) if a.lower() in AREAS]:
        out["areas"] = list(dict.fromkeys(a.lower() for a in areas))
    entities = []
    for entity in data.get("entities") or []:
        if isinstance(entity, dict) and str(entity.get("name") or "").strip():
            entities.append({"name": str(entity["name"]).strip()[:200],
                             "role": _choice(entity.get("role"), ROLES) or "other"})
    if entities:
        out["entities"] = entities[:15]
    for key, limit in (("related_events", 10), ("actions", 3)):
        if values := _strings(data.get(key), limit):
            out[key] = values
    try:
        out["confidence"] = round(max(0.0, min(1.0, float(data["confidence"]))), 2)
    except (KeyError, TypeError, ValueError):
        pass
    if headline := str(data.get("headline") or "").strip():
        out["headline"] = headline[:300]
    return out


def parse_analysis(output: str, uid: str) -> dict[str, Any] | None:
    """Оценка записи `uid` из последнего JSON-блока ответа с `analysis`."""
    candidates = _FENCED.findall(output or "")
    if not candidates:
        start = (output or "").rfind('{"analysis"')
        if start >= 0:
            candidates = [output[start:]]
    for raw in reversed(candidates):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        found = data.get("analysis") if isinstance(data, dict) else None
        if not isinstance(found, dict):
            continue
        if str(found.get("uid", "")).strip() != uid:
            return None
        return normalize(found)
    return None


async def assess(message: str, uid: str, attachments: dict[str, str],
                 profile: Active | None = None) -> tuple[dict[str, Any], Any]:
    """Ран аналитика на запись для клиента `profile` (без него — профиль из
    настроек): (оценка с номером рана — или `error` с ним же, строка рана)."""
    variables = None
    if profile is not None:
        variables = {"company_profile": profile.prompt}
        if profile.paged:
            variables["profile_id"] = profile.id  # страницы профиля — инструментами profile_*
    run_id = await runner.create("analyst", message, attachments=attachments, variables=variables)
    # Клиент — «пользователь» Langfuse: расход и ошибки видны по каждой компании.
    with tracing.attributes(user_id=profile.id if profile else DEFAULT_ID):
        row = await runner.execute(run_id)
    output = (row.output or "") if row.status == "done" else ""
    found = parse_analysis(output, uid) if output else None
    if found is not None:
        logger.info("запись %s: важность для %s — %s, ран %s", uid, profile.id if profile else DEFAULT_ID,
                    found["importance"], run_id)
        return {**found, "run_id": run_id, "assessed_at": _now().isoformat(timespec="seconds")}, row
    error = (row.error or f"ран завершился: {row.status}") if not output else "аналитик не вернул оценку"
    logger.warning("запись %s: без оценки для компании, ран %s — %s", uid, run_id, error)
    return {"error": error[:500], "run_id": run_id}, row
