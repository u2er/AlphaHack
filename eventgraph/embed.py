from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from eventgraph.config import settings

logger = logging.getLogger(__name__)


class EmbedGate:
    """Фоновые куски уходят только когда ни один срочный запрос не ждёт."""

    def __init__(self, background_limit: int = 1):
        self._background = asyncio.Semaphore(background_limit)
        self._urgent = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @asynccontextmanager
    async def urgent(self):
        self._urgent += 1
        self._idle.clear()
        try:
            yield
        finally:
            self._urgent -= 1
            if not self._urgent:
                self._idle.set()

    @asynccontextmanager
    async def background(self):
        async with self._background:
            await self._idle.wait()
            yield


_gate: EmbedGate | None = None


def _get_gate() -> EmbedGate:
    # Семафор и событие привязаны к циклу событий — создаём на первом вызове.
    global _gate
    if _gate is None:
        _gate = EmbedGate()
    return _gate


def remote() -> bool:
    return bool(settings.emb_base_url)


@lru_cache(maxsize=1)
def _client() -> Any:
    from openai import AsyncOpenAI

    base = settings.emb_base_url.rstrip("/")
    return AsyncOpenAI(base_url=base if base.endswith("/v1") else f"{base}/v1",
                       api_key=settings.emb_api_key or "none")


@lru_cache(maxsize=1)
def _sync_client() -> Any:
    from openai import OpenAI

    base = settings.emb_base_url.rstrip("/")
    return OpenAI(base_url=base if base.endswith("/v1") else f"{base}/v1",
                  api_key=settings.emb_api_key or "none")


def _cache_dir() -> Path:
    cache = settings.data_dir / "models"
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Запуск вне контейнера (CLI профиля, тесты): /data может быть не наш.
        cache = Path.home() / ".cache" / "eventgraph" / "models"
        cache.mkdir(parents=True, exist_ok=True)
    return cache


@lru_cache(maxsize=1)
def _local_model() -> Any:
    from fastembed import TextEmbedding  # тяжёлый импорт — только когда нужен

    return TextEmbedding(settings.emb_model, cache_dir=str(_cache_dir()), threads=2)


def _prefix(passage: bool = False) -> str:
    # E5 обучали с префиксами; для сравнения текстов между собой — query,
    # для фрагментов документов, среди которых ищут, — passage.
    if "e5" not in settings.emb_model.lower():
        return ""
    return "passage: " if passage else "query: "


def _normalize(vectors: np.ndarray) -> np.ndarray:
    return vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-9)


def _local_sync(texts: list[str], passage: bool = False) -> np.ndarray:
    prefix = _prefix(passage)
    vectors = np.array(list(_local_model().embed([prefix + t for t in texts], batch_size=16)), dtype=np.float32)
    return _normalize(vectors)


def _remote_sync(texts: list[str], passage: bool = False) -> np.ndarray:
    response = _sync_client().embeddings.create(model=settings.emb_model,
                                                input=[_prefix(passage) + t for t in texts])
    vectors = np.array([d.embedding for d in sorted(response.data, key=lambda d: d.index)], dtype=np.float32)
    return _normalize(vectors)


async def _create(texts: list[str]) -> np.ndarray:
    if not remote():
        return await asyncio.to_thread(_local_sync, texts)
    response = await _client().embeddings.create(model=settings.emb_model, input=[_prefix() + t for t in texts])
    vectors = np.array([d.embedding for d in sorted(response.data, key=lambda d: d.index)], dtype=np.float32)
    return _normalize(vectors)


def _clean(texts: list[str]) -> list[str]:
    # Пустая строка ломает часть провайдеров; пробел даёт осмысленный нуль.
    return [(t or "").strip()[:8000] or " " for t in texts]


async def embed(texts: list[str], urgent: bool = False) -> np.ndarray:
    """Нормированные векторы текстов, float32, по строке на текст."""
    texts = _clean(texts)
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    gate = _get_gate()
    if urgent:
        async with gate.urgent():
            return await _create(texts)
    parts = []
    for start in range(0, len(texts), max(1, settings.emb_batch)):
        async with gate.background():
            parts.append(await _create(texts[start:start + settings.emb_batch]))
    return np.vstack(parts)


async def embed_one(text: str, urgent: bool = True) -> np.ndarray:
    return (await embed([text], urgent=urgent))[0]


def embed_sync(texts: list[str], passage: bool = False) -> np.ndarray:
    """То же, синхронно и без очереди: для библиотеки профиля (profile.db),
    которая живёт вне цикла событий. `passage` — фрагменты документов, среди
    которых ищут; запросы и тексты для сравнения — без него."""
    texts = _clean(texts)
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    create = _remote_sync if remote() else _local_sync
    step = max(1, settings.emb_batch)
    return np.vstack([create(texts[start:start + step], passage) for start in range(0, len(texts), step)])


def model_name() -> str:
    """Чем считаны векторы: модель и где она работает — для сверки с индексом."""
    return f"{'remote' if remote() else 'local'}:{settings.emb_model}"
