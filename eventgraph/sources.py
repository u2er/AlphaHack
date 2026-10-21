from __future__ import annotations

from dataclasses import dataclass, field
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


# Источники. Новый — строка здесь; код сборщиков — в connectors/kinds/,
# их настройки описаны в начале каждого модуля.
SOURCES: tuple[Source, ...] = (
    Source(
        name="tsunews",
        collect=telegram_channel.collect,
        config={"channel": "tsunews", "backfill": 10, "max_pages": 10},
        description="Telegram-канал ТГУ",
    ),
    Source(
        name="tsu_news_site",
        collect=website.collect,
        config={"url": "https://news.tsu.ru/news/", "backfill": 10, "max_new_per_run": 30},
        description="Новости на сайте ТГУ",
    ),
    # PDF кладут в ./pdfs на хосте (в контейнере это /data/pdfs); файл — одна
    # запись: весь текст документа, картинки и таблицы. Первый запуск с движком
    # docling скачивает модели (~1 ГБ) в HF_HOME, поэтому потолок выше общего.
    Source(
        name="pdf_docs",
        collect=pdf.collect,
        config={
            "mode": "dir",
            "path": "/data/pdfs",
            "source": "Документы",
            "max_files_per_run": 3,
        },
        description="PDF-документы из каталога /data/pdfs",
        interval_seconds=3600,
        images=True,
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
