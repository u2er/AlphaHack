from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from eventgraph.config import settings


async def require_auth(x_api_key: str | None = Header(default=None)) -> None:
    if not settings.api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API закрыт: задайте API_KEY")
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный ключ (заголовок X-Api-Key)")
