from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AGENTS = ("news", "gardener")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Ключ API — заголовок X-Api-Key. Пустой ключ — API закрыт целиком,
    # кроме /health.
    api_key: str = ""

    # ── База ─────────────────────────────────────────────────────────────────
    # Postgres с pgvector: и коннекторы с записями, и граф с векторами.
    database_url: str = "postgresql+asyncpg://eventgraph:eventgraph@db:5432/eventgraph"

    # Картинки записей и кеш локального эмбеддера.
    data_dir: Path = Path("/data")

    # ── Коннекторы ───────────────────────────────────────────────────────────
    connector_timeout: int = 300
    connector_preview: int = 20
    item_image_max_bytes: int = 20 * 1024 * 1024

    # ── LLM агентов ──────────────────────────────────────────────────────────
    # OpenAI-совместимый API с вызовом инструментов. Без ключа агенты не
    # работают, а записи просто копятся.
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    # Видит ли модель картинки: без этого записи уходят агенту только текстом.
    llm_vision: bool = False
    # Дополнительные параметры запроса JSON-объектом, например
    # {"reasoning_effort": "none"} — у части моделей инструменты без него
    # отвечают 400.
    llm_params: str = ""
    # Потолок одного вызова модели и число повторов на 429 / 5xx / сетевых сбоях.
    llm_timeout: int = 300
    llm_retries: int = 3
    # Каких агентов запускать: news, gardener.
    agents: str = ",".join(AGENTS)
    # Потолок одного вызова инструмента. Запись в граф идёт в этом же
    # процессе, без очереди, — ей нужно время на эмбеддинги.
    tool_timeout: int = 600
    # Потолок рана агента целиком.
    run_timeout: int = 1800

    # ── Эмбеддинги графа и сюжетов ───────────────────────────────────────────
    # OpenAI-совместимый /v1/embeddings. Пустой адрес — локальная модель
    # (fastembed, ONNX на CPU) прямо в процессе.
    emb_base_url: str = ""
    emb_api_key: str = ""
    emb_model: str = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
    # Сколько текстов уходит в эмбеддер за раз.
    emb_batch: int = 32

    # ── Граф ─────────────────────────────────────────────────────────────────
    graph_name: str = "news"
    # Сколько находок отдаёт поиск по графу и с какого сходства они считаются.
    graph_search_limit: int = 8
    graph_score_threshold: float = 0.35

    # ── Разбор новостей ──────────────────────────────────────────────────────
    news_item_chars: int = 4000
    news_item_images: int = 2
    news_image_edge: int = 512
    news_max_attempts: int = 3

    # ── Сюжеты ───────────────────────────────────────────────────────────────
    # Записи об одном событии из разных источников — один сюжет: агент видит,
    # в какие события графа уже разобраны другие его записи. Вектор — от того
    # же эмбеддера, что и у графа.
    stories_enabled: bool = True
    embed_chars: int = 400
    story_threshold: float = 0.85
    story_window_hours: int = 24
    # Повтор уже разобранного (почти дословный) закрывается без агента.
    # 0 — не закрывать: агент увидит и повтор.
    duplicate_threshold: float = 0.97
    duplicate_window_hours: int = 48

    log_level: str = "INFO"

    @field_validator("llm_params")
    @classmethod
    def _params_json(cls, value: str) -> str:
        if value.strip():
            data = json.loads(value)
            if not isinstance(data, dict):
                raise ValueError("LLM_PARAMS — JSON-объект")
        return value

    @property
    def llm_extra(self) -> dict[str, Any]:
        return json.loads(self.llm_params) if self.llm_params.strip() else {}

    @property
    def enabled_agents(self) -> set[str]:
        """Агенты, которых можно запускать: названы в AGENTS и есть ключ LLM."""
        if not self.llm_api_key:
            return set()
        return {a.strip() for a in self.agents.split(",") if a.strip() in AGENTS}

    def agent_on(self, name: str) -> bool:
        return name in self.enabled_agents

    @property
    def items_dir(self) -> Path:
        """Оригиналы картинок, принесённых коннекторами."""
        return self.data_dir / "items"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
