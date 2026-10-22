from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from eventgraph.connectors.kinds import pdf, telegram_channel, website
from eventgraph.connectors.runtime import Context, validate_name

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
