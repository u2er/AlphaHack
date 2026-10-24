from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from eventgraph.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── сбор ─────────────────────────────────────────────────────────────────────


class Connector(Base):
    """Состояние коннектора. Сам коннектор — код и настройки — задан в
    `sources.py` или, для заведённых из интерфейса, в `spec`; здесь — то, что
    меняется от запуска к запуску. Строка заводится при старте по имени
    источника."""

    __tablename__ = "connectors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    # Курсор и всё, что коннектор хочет помнить между запусками (ctx.state).
    # Сохраняется только после успешного запуска — вместе с записями.
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str] = mapped_column(String(20), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    # Пауза из интерфейса: по расписанию не запускается, вручную — можно.
    paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Источник, заведённый из интерфейса (sources.custom_spec): вид, настройки,
    # период. У источников из sources.py — None.
    spec: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ConnectorRun(Base):
    """Один запуск коннектора. Очередь воркера — строки в статусе queued.
    `test` — пробный запуск: показывает, что вернул источник, но ничего не
    пишет и курсор не двигает."""

    __tablename__ = "connector_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("connectors.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # scheduled | manual | test
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    items_total: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    items_dup: Mapped[int] = mapped_column(Integer, default=0)
    preview: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    log: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Item(Base):
    """Запись, принесённая коннектором: новость, пост, сообщение.

    Агент новостей разбирает каждую запись отдельным раном, как только она
    собрана (news.py): `agent_run_id` — его ран, `verdict` — что он решил
    (stored | merged | skipped | failed; below_threshold — аналитик оценил
    запись ниже порога важности для всех клиентов, и агент её не разбирал),
    `outcome` — событие графа или причина отказа, `analysis` — оценки аналитика
    по профилям клиентов, сделанные до разбора. Фильтров до агента
    нет: в разбор идёт всё собранное. Запись получает сюжет (stories.py), и
    почти дословный повтор уже разобранного закрывается как duplicate.
    """

    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("connector_id", "external_id", name="uq_items_connector_external"),
        Index("ix_items_unprocessed", "processed_at", "collected_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    uid: Mapped[str] = mapped_column(String(32), unique=True)
    connector_id: Mapped[int] = mapped_column(
        ForeignKey("connectors.id", ondelete="CASCADE"), index=True
    )
    # Ключ `id` из результата коннектора, иначе хеш текста и картинок.
    external_id: Mapped[str] = mapped_column(String(512))
    text: Mapped[str] = mapped_column(Text, default="")
    # Пути оригиналов относительно каталога данных: "items/2026/09/26/<uid>-0.jpg".
    images: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # Всё остальное, что вернул коннектор.
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Ран агента, разобравший запись (последний, если попыток было несколько).
    agent_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Сколько ранов агента не дали по записи вердикта.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    verdict: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    # Вектор записи (float32) — по нему записи собираются в сюжеты.
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # Сюжет — id первой записи сюжета; записи одного сюжета идут агенту вместе.
    story_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    outcome: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Оценки аналитика (analyst.py) — по профилю клиента в `profiles`:
    # {profile_id: {importance 0–5, impact, …, run_id}} или {error, run_id}.
    # Верхние поля — самая важная из оценок с её `profile_id`: по ним идут
    # порог и сводки, а лента клиента (GET /profiles/{id}/feed) — по его оценке.
    analysis: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class CompanyProfile(Base):
    """Профиль клиента — подтверждённый экспорт мастера профиля (profile/contract.py,
    schema_version 1). Аналитик оценивает каждую запись для каждого активного
    профиля; `prompt` — текст профиля, который он видит вместо {{company_profile}}.
    `source`: api — загружен PUT /profiles, file — из PROFILES_DIR, env — COMPANY_PROFILE."""

    __tablename__ = "company_profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    company_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    prompt: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    source: Mapped[str] = mapped_column(String(20), default="api")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProfileChunk(Base):
    """Фрагмент страницы профиля (кроме основной — её агент видит целиком):
    по `vec` идёт поиск по смыслу, по `tsv` — по словам (profile_search)."""

    __tablename__ = "profile_chunks"
    __table_args__ = (Index("ix_profile_chunks_tsv", "tsv", postgresql_using="gin"),)

    id: Mapped[str] = mapped_column(String(200), primary_key=True)  # profile:page:n
    profile_id: Mapped[str] = mapped_column(ForeignKey("company_profiles.id", ondelete="CASCADE"), index=True)
    page_id: Mapped[str] = mapped_column(String(64))
    page_title: Mapped[str] = mapped_column(String(200), default="")
    n: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, default="")
    vec: Mapped[list[float] | None] = mapped_column(Vector(), nullable=True)
    tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed("to_tsvector('russian', coalesce(text, ''))", persisted=True))


# ── обслуживание графа ──────────────────────────────────────────────────────


class MaintenanceRun(Base):
    """Прогон обслуживания графа: задачи садовника одна за другой (gardening.py)."""

    __tablename__ = "maintenance_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20), default="schedule")  # schedule | manual
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)  # running | done | stopped | error
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tasks: Mapped[int] = mapped_column(Integer, default=0)
    merged: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Итог: счётчики по задачам и слитые узлы.
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")


class MaintenanceTask(Base):
    """Одна задача обслуживания — один ран садовника."""

    __tablename__ = "maintenance_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("maintenance_runs.id", ondelete="CASCADE"), index=True)
    agent_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    task: Mapped[str] = mapped_column(String(20))  # duplicates | islands | prune
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | done | error
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    merged: Mapped[int] = mapped_column(Integer, default=0)
    steps: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    # Ответ садовника без JSON-блока с итогом.
    summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")


class AppSetting(Base):
    """Настройки, которые меняются через API. Главнее значений из env."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ── агенты ───────────────────────────────────────────────────────────────────


class AgentRun(Base):
    """Один ран агента (agents/runner.py): разбор записи или задача садовника.
    Ран заводится в `queued` и исполняется тут же, по месту."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)  # queued | running | done | error | stopped
    input: Mapped[str] = mapped_column(Text, default="")
    # Прошлые реплики разговора: [{role, content}]; у пачек и задач — пусто.
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # Картинки рана: имя, под которым их видит агент, → путь от каталога данных.
    attachments: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    # Подстановки в промпт агента: {"company_profile": "…"} — профиль, для
    # которого аналитик оценивает запись. Пусто — из настроек.
    variables: Mapped[dict[str, str] | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    steps: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens_prompt: Mapped[int] = mapped_column(Integer, default=0)
    tokens_completion: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentLog(Base):
    """Шаг рана: рассуждение, вызов инструмента с аргументами и ответом, ошибка."""

    __tablename__ = "agent_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    step: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(20))  # thinking | tool | error | final
    tool: Mapped[str] = mapped_column(String(80), default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


# ── граф ─────────────────────────────────────────────────────────────────────


class GraphElement(Base):
    """Узел или ребро графа (graph/store.py).

    Имя узла уникально в графе без учёта регистра и лишних пробелов (`key`);
    ребро задано четвёркой (откуда, куда, имя, направленность). Вектор `label`
    — эмбеддинг метки: имени узла или «A — имя → B» у ребра; `brief` — метки
    вместе с метаданными и содержимым элементов. По ним идёт смысловой поиск.
    """

    __tablename__ = "graph_elements"
    __table_args__ = (
        Index("uq_graph_node_key", "graph", "key", unique=True, postgresql_where=text("kind = 'node'")),
        Index("ix_graph_edge_ends", "graph", "source_id", "target_id"),
        Index("ix_graph_edge_target", "target_id"),
        Index("ix_graph_kind", "graph", "kind"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    graph: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(10))  # node | edge
    name: Mapped[str] = mapped_column(String(300))
    key: Mapped[str] = mapped_column(String(300))
    # Метаданные элемента со своими JSON-типами: список псевдонимов остаётся списком.
    attrs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Ссылки на другие элементы — ничего не копируют.
    refs: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # Удаление узла уносит его рёбра.
    source_id: Mapped[str | None] = mapped_column(
        ForeignKey("graph_elements.id", ondelete="CASCADE"), nullable=True
    )
    target_id: Mapped[str | None] = mapped_column(
        ForeignKey("graph_elements.id", ondelete="CASCADE"), nullable=True
    )
    directed: Mapped[bool] = mapped_column(Boolean, default=True)
    label: Mapped[str] = mapped_column(Text, default="")
    brief: Mapped[str] = mapped_column(Text, default="")
    label_vec: Mapped[list[float] | None] = mapped_column(Vector(), nullable=True)
    brief_vec: Mapped[list[float] | None] = mapped_column(Vector(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GraphItem(Base):
    """Элемент узла или ребра: текст или публикация (source). Ищется по своему
    вектору, а находка поднимается до узла или ребра."""

    __tablename__ = "graph_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    element_id: Mapped[str] = mapped_column(
        ForeignKey("graph_elements.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(20))  # text | source
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # То, что эмбеддится и показывается: текст или цитата с заголовком.
    content: Mapped[str] = mapped_column(Text, default="")
    vec: Mapped[list[float] | None] = mapped_column(Vector(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GraphMergeDecision(Base):
    """Решение по паре кандидатов в дубли: слить или «не дубли». Отклонённые
    пары больше не предлагаются; признаки пары — обучающая выборка."""

    __tablename__ = "graph_merge_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    graph: Mapped[str] = mapped_column(String(100), index=True)
    decision: Mapped[str] = mapped_column(String(10))  # merge | reject
    a_id: Mapped[str] = mapped_column(String(32))
    b_id: Mapped[str] = mapped_column(String(32))
    kept: Mapped[str] = mapped_column(String(1), default="")  # a | b
    name_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    brief_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Обе стороны до решения: имя, тип, метаданные, степень, сводка.
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
