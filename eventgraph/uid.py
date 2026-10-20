from __future__ import annotations

import os
import time

# Алфавит Crockford: без I, L, O и U — чтобы не путались при чтении вслух.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        out.append(_ALPHABET[rem])
    return "".join(reversed(out))


def new_uid(when: float | None = None) -> str:
    millis = int((when if when is not None else time.time()) * 1000)
    return _encode(millis, 10) + _encode(int.from_bytes(os.urandom(10), "big"), 16)


def is_uid(value: str) -> bool:
    """Проверка формы — нужна там, где `uid` приходит из пути запроса."""
    return (
        isinstance(value, str)
        and len(value) == 26
        and all(char in _ALPHABET for char in value)
    )
