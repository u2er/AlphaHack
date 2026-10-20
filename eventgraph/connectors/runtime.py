from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx

from eventgraph.connectors.tls import HttpClient

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
# Хвост лога, который храним у запуска.
LOG_LIMIT = 16_000


def validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        return "Имя — латиница в нижнем регистре, цифры и _, от 2 до 64 символов, с буквы"
    return ""


class Context:
    """То, что получает `collect(ctx)`.

    - `ctx.config` — параметры коннектора (копия, менять бессмысленно);
    - `ctx.state`  — словарь, который переживает запуски: курсоры, id
      последнего сообщения. Сохраняется только после успешного запуска —
      вместе с записями, так что упавший запуск курсор не сдвигает;
    - `ctx.http`   — общий `httpx.AsyncClient` с разумными таймаутами; доверяет
      корню Минцифры и сам докачивает промежуточные сертификаты (tls.py);
    - `ctx.log()`  — строка в лог запуска, видна в его истории;
    - `ctx.last_run_at` — время прошлого успешного запуска или None.
    """

    def __init__(
        self,
        *,
        config: dict[str, Any],
        state: dict[str, Any],
        name: str = "draft",
        last_run_at: datetime | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.name = name
        self.last_run_at = last_run_at
        self._lines: list[str] = []
        self.http = HttpClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            headers={"User-Agent": "EventGraph/0.1 (+connector)"},
        )

    def log(self, *parts: Any) -> None:
        line = f"{datetime.now(timezone.utc):%H:%M:%S} " + " ".join(str(p) for p in parts)
        self._lines.append(line)

    @property
    def text_log(self) -> str:
        text = "\n".join(self._lines)
        return text if len(text) <= LOG_LIMIT else "…\n" + text[-LOG_LIMIT:]

    async def aclose(self) -> None:
        await self.http.aclose()
