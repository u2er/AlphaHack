from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import PrivateAttr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AGENTS = ("news", "gardener", "analyst")

# Пока профиль не задан, аналитик видит в промпте эту заготовку.
COMPANY_PROFILE_PLACEHOLDER = """\
[ПРОФИЛЬ КОМПАНИИ НЕ ЗАДАН — задайте COMPANY_PROFILE]
Название: …
Отрасль и рынки: …
Продукты и услуги: …
Клиенты: …
География: …
Конкуренты: …
Партнёры и поставщики: …
Регуляторы: …
Ключевые риски: …
Стратегические цели: …"""


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
    llm_model: str = "deepseek-flash"
    # Видит ли модель картинки: без этого записи уходят агенту только текстом.
    llm_vision: bool = False
    # Дополнительные параметры запроса JSON-объектом, например
    # {"reasoning_effort": "none"} — у части моделей инструменты без него
    # отвечают 400.
    llm_params: str = ""
    # Потолок одного вызова модели и число повторов на 429 / 5xx / сетевых сбоях.
    llm_timeout: int = 300
    llm_retries: int = 3
    # Каких агентов запускать: news, gardener, analyst.
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
    # Готовые векторы базового графа и профилей-примеров (eventgraph/embed_cache.py):
    # builtin — eventgraph/seed/embeddings.npz, путь — свой файл, off — считать всё.
    emb_cache: str = "builtin"

    # ── Граф ─────────────────────────────────────────────────────────────────
    graph_name: str = "news"
    # Базовый граф при пустом: builtin — снимок eventgraph/seed/, путь — свой
    # снимок (выгрузка GET /graphs/{graph}), off — не загружать.
    graph_seed: str = "builtin"
    # Сколько находок отдаёт поиск по графу и с какого сходства они считаются.
    graph_search_limit: int = 8
    graph_score_threshold: float = 0.35

    # ── Интернет-поиск агентов ───────────────────────────────────────────────
    # SearXNG с JSON-выдачей (сервис searxng в docker-compose.yml); пусто —
    # web_search у агентов выключен. Сколько страниц читать за поиск и сколько
    # фрагментов с каждой отдавать агенту.
    searxng_url: str = ""
    web_search_results: int = 5
    web_search_fragments: int = 2

    # ── Разбор новостей ──────────────────────────────────────────────────────
    news_item_chars: int = 4000
    news_item_images: int = 2
    news_image_edge: int = 512
    news_max_attempts: int = 3

    # ── Аналитик ─────────────────────────────────────────────────────────────
    # Перед разбором каждой записи аналитик оценивает её для компании:
    # важность и как она может повлиять на бизнес (analyst.py). Профиль
    # подставляется в его промпт вместо {{company_profile}}.
    # Действует, пока не загружен ни один профиль клиента (eventgraph/profiles.py).
    company_profile: str = COMPANY_PROFILE_PLACEHOLDER
    # Каталог экспортов мастера профиля (profile/contract.py): воркер загружает
    # новые и изменённые файлы перед разбором. Пусто — только PUT /profiles.
    profiles_dir: Path | None = None
    # Профили-примеры при старте воркера: builtin — eventgraph/seed/profiles/
    # (компании набора банка и демо-компании; уже загруженные не трогаются),
    # путь — свой каталог экспортов, off — не загружать.
    profile_examples: str = "builtin"
    # Порог важности по умолчанию (PUT /graph/settings — min_importance):
    # запись, которая ниже порога для всех клиентов, не идёт агенту новостей и
    # в граф. 0 — без порога.
    analyst_min_importance: int = 0
    # Сколько оценок аналитика (по одной на клиента) идёт одновременно для
    # одной записи. Записи по-прежнему разбираются по одной.
    analyst_concurrency: int = 4

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

    # Значения LLM_* из .env — основа, поверх которой ложатся настройки из интерфейса.
    _llm_env: dict[str, Any] | None = PrivateAttr(default=None)
    _llm_applied: dict[str, str] = PrivateAttr(default_factory=dict)

    @field_validator("llm_params")
    @classmethod
    def _params_json(cls, value: str) -> str:
        if value.strip():
            data = json.loads(value)
            if not isinstance(data, dict):
                raise ValueError("LLM_PARAMS — JSON-объект")
        return value

    @field_validator("profiles_dir", mode="before")
    @classmethod
    def _empty_dir_is_none(cls, value: Any) -> Any:
        # PROFILES_DIR= в .env — «каталога нет», а не текущий каталог.
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("company_profile")
    @classmethod
    def _profile_or_placeholder(cls, value: str) -> str:
        return value.strip() or COMPANY_PROFILE_PLACEHOLDER

    @property
    def llm_extra(self) -> dict[str, Any]:
        return json.loads(self.llm_params) if self.llm_params.strip() else {}

    def refresh_llm(self) -> bool:
        """Подхватить настройки LLM из интерфейса (utils/llm_overrides.py) поверх
        .env. Пока файл не менялся, поля не трогаются. True — что-то поменялось."""
        from utils import llm_overrides

        found = llm_overrides.load()
        if found == self._llm_applied:
            return False
        if self._llm_env is None:
            self._llm_env = {"base_url": self.llm_base_url, "model": self.llm_model, "api_key": self.llm_api_key,
                             "vision": self.llm_vision, "params": self.llm_params, "timeout": self.llm_timeout}
        base = self._llm_env
        values = {**base, **{k: v for k, v in found.items() if k in base}}
        if "vision" in found:
            values["vision"] = found["vision"].strip().lower() in {"1", "true", "yes", "on"}
        if "timeout" in found:
            try:
                values["timeout"] = int(float(found["timeout"]))
            except ValueError:
                values["timeout"] = base["timeout"]
        if "params" in found:
            try:
                values["params"] = found["params"] if isinstance(json.loads(found["params"]), dict) else base["params"]
            except ValueError:
                values["params"] = base["params"]
        for key, value in values.items():
            setattr(self, f"llm_{key}", value)
        self._llm_applied = found
        return True

    @property
    def enabled_agents(self) -> set[str]:
        """Агенты, которых можно запускать: названы в AGENTS и есть ключ LLM."""
        self.refresh_llm()
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
