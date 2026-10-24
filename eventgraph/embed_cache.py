"""Готовые векторы: базовый граф и профили-примеры не пересчитываются на
каждой свежей базе.

Файл eventgraph/seed/embeddings.npz — векторы по ключу sha1(модель + текст):
эмбеддер (eventgraph/embed.py) сначала ищет текст здесь и считает только
то, чего нет. Векторы лежат в int8 с масштабом на строку (≈0,1% к косинусу
против float32) — так файл вчетверо меньше. Файл другой модели (EMB_MODEL)
не используется. EMB_CACHE: builtin (по умолчанию), путь к своему файлу или off.

Пересобрать после смены снимка графа, профилей-примеров или модели:

    python -m eventgraph.embed_cache [куда.npz]

Векторы берутся из базы, где граф уже загружен (недостающие считаются):
снимок вливается во временный граф и удаляется, записывается ровно то,
что эмбеддер спросит на пустой базе.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np

from eventgraph.config import settings

logger = logging.getLogger(__name__)

BUILTIN = Path(__file__).resolve().parent / "seed" / "embeddings.npz"
TEMP_GRAPH = "__embed_cache__"

# Сборка: что эмбеддер спросил (ключ → вектор) и что уже известно из базы.
recording: dict[bytes, np.ndarray] | None = None
_known: dict[bytes, np.ndarray] = {}


def key(text: str) -> bytes:
    return hashlib.sha1(f"{settings.emb_model}\n{text}".encode("utf-8")).digest()


def source() -> Path | None:
    value = (settings.emb_cache or "").strip()
    if value.lower() in ("", "off", "none", "false", "0"):
        return None
    return BUILTIN if value.lower() == "builtin" else Path(value)


@lru_cache(maxsize=1)
def _table() -> tuple[dict[bytes, int], np.ndarray, np.ndarray] | None:
    path = source()
    if path is None or not path.is_file():
        return None
    try:
        data = np.load(path)
        if str(data["model"]) != settings.emb_model:
            logger.info("готовые векторы %s посчитаны моделью %s, а эмбеддер — %s: не использую",
                        path.name, data["model"], settings.emb_model)
            return None
        keys = data["keys"]
        index = {keys[i].tobytes(): i for i in range(len(keys))}
        logger.info("готовые векторы %s: %s", path.name, len(index))
        return index, data["vecs"], data["scale"]
    except Exception as exc:  # noqa: BLE001 — битый файл значит только «считать всё»
        logger.warning("готовые векторы %s не прочитаны: %s", path, exc)
        return None


def lookup(texts: list[str]) -> list[np.ndarray | None]:
    """Вектор на каждый текст или None, если его надо считать."""
    table = _table()
    out: list[np.ndarray | None] = []
    for text in texts:
        k = key(text)
        vector = _known.get(k)
        if vector is None and table is not None and (row := table[0].get(k)) is not None:
            vector = table[1][row].astype(np.float32) * table[2][row]
            vector /= max(float(np.linalg.norm(vector)), 1e-9)
        if vector is not None and recording is not None:
            recording[k] = vector
        out.append(vector)
    return out


def remember(texts: list[str], vectors: np.ndarray) -> None:
    if recording is not None:
        for text, vector in zip(texts, vectors):
            recording[key(text)] = np.asarray(vector, dtype=np.float32)


def save(path: Path, vectors: dict[bytes, np.ndarray]) -> None:
    keys = sorted(vectors)
    matrix = np.stack([vectors[k] for k in keys]).astype(np.float32)
    scale = np.maximum(np.abs(matrix).max(axis=1), 1e-9) / 127
    quantized = np.round(matrix / scale[:, None]).astype(np.int8)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as file:
        np.savez_compressed(file, model=np.array(settings.emb_model),
                            keys=np.frombuffer(b"".join(keys), dtype=np.uint8).reshape(len(keys), 20),
                            vecs=quantized, scale=scale.astype(np.float32))


async def _harvest() -> dict[bytes, np.ndarray]:
    """Векторы, которые уже лежат в базе, — по тем же текстам, что спрашивал эмбеддер."""
    from sqlalchemy import select

    from eventgraph.db import SessionLocal
    from eventgraph.embed import _clean, _prefix
    from eventgraph.graph.store import BRIEF_TEXT_LIMIT
    from eventgraph.models import GraphElement, GraphItem, ProfileChunk

    pairs: list[tuple[str, object]] = []
    async with SessionLocal() as session:
        for label, brief, label_vec, brief_vec in await session.execute(
                select(GraphElement.label, GraphElement.brief, GraphElement.label_vec, GraphElement.brief_vec)):
            pairs += [(label, label_vec), (f"{label}\n{brief}"[:BRIEF_TEXT_LIMIT], brief_vec)]
        pairs += list(await session.execute(select(GraphItem.content, GraphItem.vec)))
        pairs += list(await session.execute(select(ProfileChunk.text, ProfileChunk.vec)))
    texts = _clean([text for text, _ in pairs])
    return {key(_prefix() + text): np.asarray(vec, dtype=np.float32)
            for text, (_, vec) in zip(texts, pairs) if vec is not None}


async def build(path: Path) -> int:
    """Векторы базового графа и профилей-примеров — в файл. Число векторов."""
    global recording
    from sqlalchemy import delete

    from eventgraph import embed, profiles, seed
    from eventgraph.db import SessionLocal
    from eventgraph.models import AppSetting, GraphElement
    from profile.contract import chunk_pages, load_profile

    _known.update(await _harvest())
    logger.info("векторов в базе: %s", len(_known))
    recording = {}
    try:
        await seed.seed_graph(TEMP_GRAPH, seed.BUILTIN, force=True)
        folder = profiles.examples_dir()
        for example in sorted(Path(folder).glob("*.json")) if folder and folder.is_dir() else []:
            chunks = chunk_pages(load_profile(json.loads(example.read_text(encoding="utf-8"))))
            if chunks:
                await embed.embed([c["text"] for c in chunks])
        vectors = recording
    finally:
        recording = None
        _known.clear()
        async with SessionLocal() as session:
            await session.execute(delete(GraphElement).where(GraphElement.graph == TEMP_GRAPH))
            await session.execute(delete(AppSetting).where(AppSetting.key == f"graph_seed:{TEMP_GRAPH}"))
            await session.commit()
    if not vectors:
        raise RuntimeError("эмбеддер не спросил ни одного вектора — сохранять нечего")
    save(path, vectors)
    return len(vectors)


async def main() -> None:
    from eventgraph.db import create_schema
    from eventgraph.logging_config import setup_logging

    setup_logging()
    await create_schema()
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else BUILTIN
    # Под `python -m` этот файл — __main__, а эмбеддер видит eventgraph.embed_cache:
    # запись и известные векторы должны жить в том модуле, что видит он.
    from eventgraph import embed_cache

    print(await embed_cache.build(path), path)


if __name__ == "__main__":
    asyncio.run(main())
