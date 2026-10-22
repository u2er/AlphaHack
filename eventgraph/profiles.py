"""Профили клиентов: для кого аналитик оценивает записи.

Профиль — подтверждённый страничный профиль (profile/contract.py, schema_version
2; экспорт мастера v1 конвертируется в страницы). Он попадает сюда двумя путями:
PUT /profiles и каталог PROFILES_DIR, куда чат онбординга и мастер кладут
экспорт (воркер сверяет каталог перед разбором).

Аналитик всегда видит основную страницу и оглавление остальных
(`render_for_prompt` вместо {{company_profile}}), а остальные страницы читает
инструментами: `page` — страница по id, `search` — фрагменты по смыслу
(вектор), по словам (полнотекстовый индекс) или гибридно. Пока ни одного
профиля нет, работает прежняя схема: один профиль `default` из COMPANY_PROFILE.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

from eventgraph import embed
from eventgraph.config import settings
from eventgraph.db import SessionLocal
from eventgraph.models import CompanyProfile, ProfileChunk
from profile.contract import ProfileDocument, chunk_pages, display_title, load_profile, render_for_prompt

logger = logging.getLogger(__name__)

DEFAULT_ID = "default"
SEARCH_MODES = ("hybrid", "vector", "keyword")
PAGE_WINDOW = 6000
RRF_K = 60


@dataclass(frozen=True)
class Active:
    id: str
    name: str
    prompt: str
    # Есть ли у профиля страницы для profile_page / profile_search (у default — нет).
    paged: bool = False
    # Источники клиента; пусто — все.
    sources: tuple[str, ...] = ()

    def wants(self, connector: str) -> bool:
        return not self.sources or connector in self.sources


def parse(payload: dict[str, Any]) -> tuple[ProfileDocument, str]:
    """Страничный профиль и текст для промпта; ValueError — не экспорт v1/v2."""
    doc = load_profile(payload)
    return doc, render_for_prompt(doc)


async def upsert(payload: dict[str, Any], source: str = "api") -> CompanyProfile:
    """Новый профиль или новая версия прежнего (по profile_id): снова активен.
    Фрагменты страниц пересчитываются целиком — вместе с векторами."""
    doc, prompt = parse(payload)
    chunks = chunk_pages(doc)
    vectors = await embed.embed([c["text"] for c in chunks]) if chunks else []
    async with SessionLocal() as session:
        row = await session.get(CompanyProfile, doc.profile_id)
        if row is None:
            row = CompanyProfile(id=doc.profile_id)
            session.add(row)
        row.name, row.company_id = display_title(doc)[:300], doc.company_id
        row.payload, row.prompt, row.source, row.active = doc.model_dump(mode="json"), prompt, source, True
        await session.flush()
        await session.execute(delete(ProfileChunk).where(ProfileChunk.profile_id == doc.profile_id))
        session.add_all(
            ProfileChunk(id=f"{doc.profile_id}:{c['page_id']}:{c['n']}"[:200], profile_id=doc.profile_id,
                         page_id=c["page_id"], page_title=c["page_title"][:200], n=c["n"], text=c["text"],
                         vec=[float(x) for x in vector])
            for c, vector in zip(chunks, vectors)
        )
        await session.commit()
        await session.refresh(row)
    logger.info("профиль %s (%s): загружен из %s, страниц %s, фрагментов %s",
                row.id, row.name, source, len(doc.pages), len(chunks))
    return row


def fallback() -> Active:
    return Active(DEFAULT_ID, "COMPANY_PROFILE", settings.company_profile)


async def active() -> list[Active]:
    """Активные профили; без них — `default` из настроек, как до профилей."""
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(CompanyProfile).where(CompanyProfile.active.is_(True)).order_by(CompanyProfile.created_at)
        )).scalars().all()
    return [Active(r.id, r.name, r.prompt, paged=True, sources=tuple((r.payload or {}).get("news_sources") or ()))
            for r in rows] or [fallback()]


async def set_sources(profile_id: str, sources: list[str]) -> CompanyProfile | None:
    """Источники клиента: правится только экспорт, страницы и векторы не трогаются."""
    async with SessionLocal() as session:
        row = await session.get(CompanyProfile, profile_id)
        if row is None:
            return None
        row.payload = {**(row.payload or {}), "news_sources": list(dict.fromkeys(sources))}
        await session.commit()
        await session.refresh(row)
        return row


async def document(profile_id: str) -> ProfileDocument | None:
    async with SessionLocal() as session:
        row = await session.get(CompanyProfile, profile_id)
    return load_profile(row.payload) if row is not None else None


async def page(profile_id: str, page_id: str, offset: int = 0, limit: int = PAGE_WINDOW) -> dict[str, Any]:
    """Страница профиля кусками по `limit` символов: длинный годовой отчёт не
    уходит в разговор целиком. Неизвестная страница — с оглавлением."""
    doc = await document(profile_id)
    if doc is None:
        return {"ok": False, "error": f"Профиль {profile_id} не найден"}
    found = doc.page(str(page_id or ""))
    if found is None:
        return {"ok": False, "error": f"Нет страницы '{page_id}'",
                "pages": [{"id": p.id, "title": p.title} for p in doc.pages]}
    offset = max(0, int(offset or 0))
    limit = max(500, min(PAGE_WINDOW * 2, int(limit or PAGE_WINDOW)))
    body = found.content[offset:offset + limit]
    out: dict[str, Any] = {"ok": True, "id": found.id, "title": found.title, "summary": found.summary,
                           "offset": offset, "total_chars": len(found.content), "content": body,
                           "sources": [s.ref for s in found.sources][:40]}
    if offset + limit < len(found.content):
        out["next_offset"] = offset + limit
    return out


def _or_query(query: str) -> str:
    """Слова запроса через ИЛИ для to_tsquery: «ставка кредит» → «ставка | кредит»."""
    words = re.findall(r"[\wёЁ]{2,}", query.lower())
    return " | ".join(dict.fromkeys(words))


async def search(profile_id: str, query: str, mode: str = "hybrid", limit: int = 6,
                 pages: list[str] | None = None) -> dict[str, Any]:
    """Фрагменты страниц профиля: vector — по смыслу, keyword — по словам
    (русская морфология Postgres), hybrid — оба списка, слитые по рангам (RRF)."""
    query, mode = str(query or "").strip(), mode if mode in SEARCH_MODES else "hybrid"
    limit = max(1, min(20, int(limit or 6)))
    if not query:
        return {"ok": False, "error": "Пустой запрос"}
    found: dict[str, dict[str, Any]] = {}
    ranks: dict[str, float] = {}
    where = [ProfileChunk.profile_id == profile_id]
    if pages:
        where.append(ProfileChunk.page_id.in_([str(p) for p in pages]))
    async with SessionLocal() as session:
        if mode in ("vector", "hybrid"):
            vector = [float(x) for x in await embed.embed_one(query)]
            distance = ProfileChunk.vec.cosine_distance(vector)
            rows = (await session.execute(
                select(ProfileChunk, distance.label("distance")).where(*where, ProfileChunk.vec.is_not(None))
                .order_by(distance).limit(limit * 2)
            )).all()
            for rank, (chunk, dist) in enumerate(rows):
                found.setdefault(chunk.id, _hit(chunk))["vector_score"] = round(1 - float(dist), 3)
                ranks[chunk.id] = ranks.get(chunk.id, 0.0) + 1 / (RRF_K + rank)
        if mode in ("keyword", "hybrid") and (terms := _or_query(query)):
            tsquery = func.to_tsquery("russian", terms)
            score = func.ts_rank_cd(ProfileChunk.tsv, tsquery)
            rows = (await session.execute(
                select(ProfileChunk, score.label("score")).where(*where, ProfileChunk.tsv.op("@@")(tsquery))
                .order_by(score.desc()).limit(limit * 2)
            )).all()
            for rank, (chunk, value) in enumerate(rows):
                found.setdefault(chunk.id, _hit(chunk))["keyword_score"] = round(float(value), 3)
                ranks[chunk.id] = ranks.get(chunk.id, 0.0) + 1 / (RRF_K + rank)
    ordered = sorted(found, key=lambda key: ranks[key], reverse=True)[:limit]
    return {"ok": True, "mode": mode, "query": query, "results": [found[key] for key in ordered]}


def _hit(chunk: ProfileChunk) -> dict[str, Any]:
    return {"page_id": chunk.page_id, "page_title": chunk.page_title, "fragment": chunk.n, "text": chunk.text}


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



EXAMPLES = Path(__file__).resolve().parent / "seed" / "profiles"


def examples_dir() -> Path | None:
    """PROFILE_EXAMPLES: builtin (по умолчанию), путь к своему каталогу или off."""
    value = (settings.profile_examples or "").strip()
    if value.lower() in ("", "off", "none", "false", "0"):
        return None
    return EXAMPLES if value.lower() == "builtin" else Path(value)


async def _known_ids() -> set[str]:
    async with SessionLocal() as session:
        return set((await session.execute(select(CompanyProfile.id))).scalars())


async def seed_examples(folder: Path | None = None) -> int:
    """Профили-примеры, которых ещё нет в базе (по profile_id). Уже загруженные
    не перезаписываются — правки пользователя и выбор источников сохраняются;
    удалённый пример вернётся при следующем старте."""
    folder = folder or examples_dir()
    if not folder or not Path(folder).is_dir():
        return 0
    known = await _known_ids()
    loaded = 0
    for path in sorted(Path(folder).glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if load_profile(payload).profile_id in known:
                continue
            await upsert(payload, source="example")
            loaded += 1
        except Exception as exc:  # noqa: BLE001 — битый пример не мешает старту воркера
            logger.warning("профиль-пример %s не загружен: %s", path.name, str(exc)[:300])
    return loaded
