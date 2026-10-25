"""Langfuse tracing of LLM calls, agent runs and tools.

Off unless LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_HOST are set: then
every helper is a no-op and the langfuse package is not even imported. Tracing never
breaks the caller — a failure to start a span only loses that span.

    with tracing.observe("analyst", as_type="agent", input=task) as span:
        span.update(output=result)
    with tracing.attributes(session_id=item_uid, user_id=profile_id):
        ...  # spans created here carry the session and the user

The SDK exports finished spans in a background thread (OpenTelemetry batching);
`flush()` pushes what is left, e.g. at the end of a CLI run."""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager, nullcontext
from typing import Any, Iterator

log = logging.getLogger("alfa.tracing")
_client: Any = None
_tried = False
# One field of a span keeps at most this many characters: graph dumps and pages are long.
MAX_FIELD_CHARS = 20_000


class _Noop:
    """Stands in for a span when tracing is off."""

    def update(self, **_: Any) -> "_Noop":
        return self

    def end(self, **_: Any) -> "_Noop":
        return self


NOOP = _Noop()


def enabled() -> bool:
    return all(os.getenv(name, "").strip() for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"))


def client() -> Any:
    """The Langfuse client, or None when tracing is off or the SDK cannot start."""
    global _client, _tried
    if _tried:
        return _client
    _tried = True
    if not enabled():
        return None
    try:
        from langfuse import Langfuse

        _client = Langfuse(public_key=os.environ["LANGFUSE_PUBLIC_KEY"].strip(),
                           secret_key=os.environ["LANGFUSE_SECRET_KEY"].strip(),
                           base_url=os.environ["LANGFUSE_HOST"].strip(),
                           environment=os.getenv("LANGFUSE_TRACING_ENVIRONMENT", "").strip() or None)
        log.info("Langfuse: трассировка включена (%s)", os.environ["LANGFUSE_HOST"].strip())
    except Exception as exc:  # noqa: BLE001
        log.warning("Langfuse: трассировка выключена — %s: %s", type(exc).__name__, exc)
        _client = None
    return _client


def clip(value: Any) -> Any:
    """Long strings shortened, nested lists and dicts walked; the rest as is."""
    if isinstance(value, str):
        return value if len(value) <= MAX_FIELD_CHARS else value[:MAX_FIELD_CHARS] + f"… [+{len(value) - MAX_FIELD_CHARS}]"
    if isinstance(value, dict):
        return {k: clip(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clip(v) for v in value]
    return value


@contextmanager
def observe(name: str, *, as_type: str = "span", **fields: Any) -> Iterator[Any]:
    """A span (or generation, agent, tool…) around the block; yields it for `update()`.
    An exception from the block marks the span as ERROR and goes on."""
    lf = client()
    if lf is None:
        yield NOOP
        return
    for key in ("input", "output", "metadata"):
        if key in fields:
            fields[key] = clip(fields[key])
    try:
        manager = lf.start_as_current_observation(name=name, as_type=as_type, **fields)
        span = manager.__enter__()
    except Exception as exc:  # noqa: BLE001
        log.debug("Langfuse: не удалось открыть %s — %s", name, exc)
        yield NOOP
        return
    try:
        yield span
    except BaseException as exc:
        _safe(span.update, level="ERROR", status_message=f"{type(exc).__name__}: {exc}"[:500])
        _safe(manager.__exit__, type(exc), exc, exc.__traceback__)
        raise
    else:
        _safe(manager.__exit__, None, None, None)


def update(span: Any, **fields: Any) -> None:
    """span.update with long fields clipped; errors are swallowed."""
    for key in ("input", "output", "metadata"):
        if key in fields:
            fields[key] = clip(fields[key])
    _safe(span.update, **fields)


def attributes(**values: Any):
    """Trace attributes (session_id, user_id, tags, metadata) for spans opened inside."""
    values = {k: v for k, v in values.items() if v}
    if client() is None or not values:
        return nullcontext()
    try:
        from langfuse import propagate_attributes

        return propagate_attributes(**values)
    except Exception as exc:  # noqa: BLE001
        log.debug("Langfuse: атрибуты не переданы — %s", exc)
        return nullcontext()


def flush() -> None:
    if _client is not None:
        _safe(_client.flush)


def _safe(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        log.debug("Langfuse: %s", exc)
