from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import urlsplit

from eventgraph.connectors.kinds import pdf, telegram_channel, website
from eventgraph.connectors.runtime import Context, validate_name

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from eventgraph.models import Connector

Collect = Callable[[Context], Awaitable[Any]]


@dataclass(frozen=True)
class Source:
    """Коннектор: откуда брать записи и как часто.

    `collect` — async-функция `collect(ctx)` из `connectors/kinds/`;
    `config` приходит в неё как `ctx.config`. Курсор (`ctx.state`) и история
    запусков лежат в базе, по `name`: переименовать источник — начать его с
    начала."""

    name: str
    collect: Collect
    config: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    # Период опроса; None — только вручную (POST /connectors/{name}/run).
    interval_seconds: int | None = 3600
    enabled: bool = True
    # Брать ли картинки записей. У источников, где картинка — тот же текст
    # (карточка с заголовком), она только забивает контекст агента.
    images: bool = True
    # Потолок на один запуск, сек; None — CONNECTOR_TIMEOUT. Длинный первый
    # парсинг PDF (модели docling) не влезает в общий лимит.
    timeout: int | None = None

    @property
    def kind(self) -> str:
        return self.collect.__module__.rsplit(".", 1)[-1]


# Каталог аналитики Альфа-Банка в репозитории (приложение 2 кейса) — и в образе.
BANK_DOCS = Path(__file__).resolve().parent.parent / "формулировка и данные хакатона" / "приложение 2"

# Источники. Новый — строка здесь; код сборщиков — в connectors/kinds/,
# их настройки описаны в начале каждого модуля. Клиент выбирает, какие из них
# смотреть (профиль: news_sources), — запись оценивается только для подписанных.
SOURCES: tuple[Source, ...] = (
    Source(
        name="lenta_economics",
        collect=website.collect,
        config={"url": "https://lenta.ru/rubrics/economics/", "mode": "rss",
                "feed_url": "https://lenta.ru/rss/news/economics", "backfill": 10, "max_new_per_run": 15},
        description="Lenta.ru — новости экономики (RSS рубрики)",
        interval_seconds=1800,
    ),
    Source(
        name="alfa_investments",
        collect=telegram_channel.collect,
        config={"channel": "alfa_investments", "backfill": 10, "max_pages": 5},
        description="Альфа-Инвестиции — аналитика и идеи (Telegram-канал @alfa_investments)",
        interval_seconds=3600,
    ),
    # Сайт закрыт защитой от ботов (JS-проверка servicepipe): без договорённости
    # с банком о доступе коннектор получает 403. Обходить её мы не будем —
    # те же материалы идут через канал alfa_investments.
    Source(
        name="alfa_investor_site",
        collect=website.collect,
        config={"url": "https://alfabank.ru/alfa-investor/posts/", "backfill": 10, "max_new_per_run": 15},
        description="Альфа-Инвестор — статьи на alfabank.ru (выключен: сайт закрыт защитой от ботов)",
        enabled=False,
    ),
    # Отчёты аналитиков банка: приложение 2 и всё, что кладут в ./pdfs (в
    # контейнере /data/pdfs). Запись — страница отчёта. Разбор — docling; кэш
    # общий с библиотекой профиля, поэтому повторно отчёты не распознаются.
    Source(
        name="alfa_analytics",
        collect=pdf.collect,
        config={"mode": "dir", "path": str(BANK_DOCS), "paths": ["/data/pdfs"], "split": "page",
                "date_from_text": True, "source": "Аналитика Альфа-Банка", "max_files_per_run": 3, "max_images": 0},
        description="Аналитика Альфа-Банка — PDF-отчёты (приложение 2 и каталог pdfs), по странице на запись",
        interval_seconds=3600,
        images=False,
        timeout=3600,
    ),
)


def check(sources: tuple[Source, ...]) -> dict[str, Source]:
    """Источники по имени; ошибка — плохое или повторённое имя, интервал меньше минуты."""
    out: dict[str, Source] = {}
    for source in sources:
        problem = validate_name(source.name)
        if problem:
            raise ValueError(f"источник {source.name!r}: {problem}")
        if source.name in out:
            raise ValueError(f"источник {source.name!r} задан дважды")
        # Реже раза в минуту опрашивать источник незачем, а чаще — невежливо.
        if source.interval_seconds is not None and source.interval_seconds < 60:
            raise ValueError(f"источник {source.name!r}: интервал — не меньше 60 секунд")
        out[source.name] = source
    return out


BY_NAME: dict[str, Source] = check(SOURCES)


# ── источники из интерфейса ──────────────────────────────────────────────────
# Клиент заводит свои источники существующих видов; настройки — в
# connectors.spec. PDF читает каталоги сервера — такие источники только в коде.
CUSTOM_KINDS: dict[str, Collect] = {"telegram_channel": telegram_channel.collect, "website": website.collect}
CUSTOM_INTERVALS = (1800, 3600, 3 * 3600, 6 * 3600, 24 * 3600)
TME_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?:s/)?", re.I)


def custom_spec(kind: str, title: str, target: str, interval_seconds: int) -> dict[str, Any]:
    """Настройки источника из формы; ValueError — с понятной человеку причиной."""
    title = " ".join(str(title or "").split())[:80]
    target = str(target or "").strip()
    if kind not in CUSTOM_KINDS:
        raise ValueError("Вид источника — telegram_channel или website")
    if interval_seconds not in CUSTOM_INTERVALS:
        raise ValueError("Период опроса — 30 минут, 1, 3, 6 или 24 часа")
    if kind == "telegram_channel":
        channel = telegram_channel.channel_name(TME_RE.sub("", target).strip("/"))
        config = {"channel": channel, "backfill": 10, "max_pages": 5}
        where, short = f"Telegram-канал @{channel}", f"@{channel}"
    else:
        url = target if "://" in target else f"https://{target}"
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
            raise ValueError("Адрес сайта — ссылка на раздел новостей, например https://news.example.ru/news/")
        config = {"url": url, "backfill": 10, "max_new_per_run": 15}
        short = parts.hostname.removeprefix("www.")
        where = f"сайт {short}"
    return {"kind": kind, "config": config, "interval_seconds": interval_seconds,
            "description": f"{title or short} — {where}"}


def custom_name(spec: dict[str, Any], taken: set[str]) -> str:
    """Имя коннектора из адреса: tg_<канал> или site_<хост>, свободное и допустимое."""
    config = spec["config"]
    if spec["kind"] == "telegram_channel":
        base = "tg_" + config["channel"].lower()
    else:
        base = "site_" + re.sub(r"[^a-z0-9]+", "_", urlsplit(config["url"]).hostname.removeprefix("www.").lower())
    base = re.sub(r"_+", "_", base).strip("_")[:58]
    name, n = base, 1
    while name in taken or validate_name(name):
        n += 1
        name = f"{base}_{n}"
    return name


def target_of(source: Source) -> str:
    """Что источник читает — для поиска повторов: канал или адрес без схемы."""
    if source.kind == "telegram_channel":
        return "tg:" + str(source.config.get("channel", "")).lower()
    url = str(source.config.get("url", ""))
    return "url:" + url.split("://", 1)[-1].removeprefix("www.").rstrip("/").lower() if url else ""


def from_row(connector: "Connector") -> Source | None:
    """Источник по строке коннектора: из sources.py или из его spec."""
    source = BY_NAME.get(connector.name)
    spec = connector.spec
    if source is not None or not spec or spec.get("removed"):
        return source
    collect = CUSTOM_KINDS.get(spec.get("kind"))
    if collect is None:
        return None
    return Source(name=connector.name, collect=collect, config=dict(spec.get("config") or {}),
                  description=spec.get("description", ""), interval_seconds=spec.get("interval_seconds"),
                  enabled=bool(spec.get("enabled", True)))


async def everything(session: "AsyncSession") -> dict[str, Source]:
    """Все действующие источники: из sources.py и заведённые из интерфейса."""
    from sqlalchemy import select

    from eventgraph.models import Connector

    rows = (await session.execute(select(Connector).where(Connector.spec.is_not(None)))).scalars().all()
    custom = {row.name: source for row in rows if row.name not in BY_NAME and (source := from_row(row))}
    return {**BY_NAME, **custom}
