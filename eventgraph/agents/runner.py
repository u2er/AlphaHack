from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from sqlalchemy import select

from eventgraph.agents import images as _images
from eventgraph.agents.specs import SPECS, AgentSpec
from eventgraph.agents.tools import Tool, ToolContext, build_tools, system_text
from eventgraph.config import settings
from eventgraph.db import SessionLocal
from eventgraph.models import AgentLog, AgentRun

logger = logging.getLogger(__name__)

RETRY_BACKOFF_SECONDS = 3
# Сколько ответа инструмента попадает в лог шага.
LOG_RESULT_CHARS = 4000


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RepeatGuard:
    """Ловит модель, которая повторяет тот же вызов ради того же ответа.

    Вызов всё равно исполняется (опрос состояния может законно получить новый
    ответ); считается только повтор и аргументов, и результата. О каждом
    повторе модели сообщается в ответе, а сверх `limit` повторов ран
    останавливается."""

    NOTE = (
        "[repeated call] You already called {name} with exactly these arguments in this run and got this "
        "same result. Calling it again will not change it — use what you have and move on."
    )

    def __init__(self, limit: int):
        self.limit = limit
        self.repeats = 0
        self._last: dict[str, str] = {}

    def check(self, name: str, raw_args: str, content: str) -> str:
        try:
            args = json.dumps(json.loads(raw_args or "{}"), sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            args = raw_args or ""
        key = f"{name}\0{args}"
        if self._last.get(key) != content:
            self._last[key] = content
            return content
        self.repeats += 1
        return f"{self.NOTE.format(name=name)}\n\n{content}"

    @property
    def exceeded(self) -> bool:
        return self.repeats > self.limit


def trim_history(turns: list[dict[str, Any]], max_history: int | None) -> list[dict[str, Any]]:
    """Только последние `max_history` реплик пользователя и всё после первой из
    них. Считаются реплики пользователя: болтовня инструментов окно не сужает."""
    if not max_history:
        return turns
    user_positions = [i for i, t in enumerate(turns) if t.get("role") == "user"]
    if len(user_positions) <= max_history:
        return turns
    return turns[user_positions[-max_history]:]


def stringify(value: Any) -> str:
    if isinstance(value, _images.ToolResult):
        value = value.text
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def step_messages(per_call: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Сообщения шага в порядке, который принимают провайдеры: сначала все
    ответы `role="tool"` подряд, потом сообщения с картинками."""
    replies = [m for messages in per_call for m in messages if m.get("role") == "tool"]
    extras = [m for messages in per_call for m in messages if m.get("role") != "tool"]
    return replies + extras


def image_messages(images: list[_images.ToolImage], notes: list[str], vision: bool) -> tuple[str, list[dict[str, Any]]]:
    """Картинки инструмента → (пометка в его ответ, дополнительные сообщения)."""
    lines = list(notes)
    if not images:
        return "\n".join(f"[image] {line}" for line in lines), []
    if not vision:
        lines.append(f"{len(images)} image(s) were produced but this model cannot see images: "
                     + "; ".join(image.size_note for image in images))
        return "\n".join(f"[image] {line}" for line in lines), []
    listing = "; ".join(image.size_note for image in images)
    lines.append(f"{len(images)} image(s) shown in the next message: {listing}")
    body = (f"Images returned by the tool call ({listing}). They are visible only in this turn — "
            "view them again if you need them later.")
    return "\n".join(f"[image] {line}" for line in lines), [
        {"role": "user", "content": _images.content_parts(body, images)}
    ]


# ── модель ───────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _client() -> Any:
    from openai import AsyncOpenAI

    return AsyncOpenAI(base_url=settings.llm_base_url or None, api_key=settings.llm_api_key or "none",
                       timeout=settings.llm_timeout, max_retries=0)


def _should_retry(exc: Exception) -> bool:
    """Повторять сетевые сбои, таймауты, 5xx и 429 — но не 4xx вроде неверного ключа."""
    from openai import APIStatusError

    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return True


def _format_error(exc: Exception | None) -> str:
    from openai import APIStatusError

    if exc is None:
        return "Model generation failed"
    if isinstance(exc, asyncio.TimeoutError):
        return f"Model call timed out after {settings.llm_timeout}s"
    if isinstance(exc, APIStatusError):
        detail = ""
        try:
            body = exc.response.json()
            detail = body.get("error", {}).get("message") or body.get("message") or ""
        except Exception:  # noqa: BLE001
            detail = str(exc)
        return f"Provider returned {exc.status_code}: {detail or exc.message}".strip()
    return f"{type(exc).__name__}: {exc}"


async def generate(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                   params: dict[str, Any], client: Any = None) -> dict[str, Any]:
    """Один шаг модели с повторами. Успех — {content, reasoning, tool_calls,
    usage}; провал — {"error": причина}."""
    client = client or _client()
    last: Exception | None = None
    for attempt in range(max(1, settings.llm_retries)):
        try:
            kwargs: dict[str, Any] = {"model": settings.llm_model, "messages": messages, **params}
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            response = await asyncio.wait_for(client.chat.completions.create(**kwargs),
                                               timeout=settings.llm_timeout)
            return parse_completion(response)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if not _should_retry(exc) or attempt == settings.llm_retries - 1:
                break
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    return {"error": _format_error(last)}


def parse_completion(response: Any) -> dict[str, Any]:
    choice = response.choices[0]
    message = choice.message
    extra = message.model_dump(exclude_none=True) if hasattr(message, "model_dump") else {}
    tool_calls = [
        {"id": tc.id, "type": "function",
         "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
        for tc in (message.tool_calls or [])
    ] or None
    usage = getattr(response, "usage", None)
    return {
        "content": message.content,
        "reasoning": extra.get("reasoning_content") or extra.get("reasoning") or None,
        "tool_calls": tool_calls,
        "usage": {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        },
    }


# ── учёт рана ────────────────────────────────────────────────────────────────


async def _log(run_id: int, step: int, kind: str, tool: str = "", **data: Any) -> None:
    async with SessionLocal() as session:
        session.add(AgentLog(run_id=run_id, step=step, kind=kind, tool=tool, data=data))
        await session.commit()


async def _update(run_id: int, **values: Any) -> None:
    async with SessionLocal() as session:
        run = await session.get(AgentRun, run_id)
        if run is not None:
            for key, value in values.items():
                setattr(run, key, value)
            await session.commit()


async def _cancelled(run_id: int) -> bool:
    async with SessionLocal() as session:
        return bool(await session.scalar(select(AgentRun.cancel_requested).where(AgentRun.id == run_id)))


async def create(agent: str, message: str, history: list[dict[str, Any]] | None = None,
                 attachments: dict[str, str] | None = None, variables: dict[str, str] | None = None) -> int:
    """Ран в очереди; исполняет его `execute`. `variables` — подстановки в промпт."""
    if agent not in SPECS:
        raise ValueError(f"unknown agent '{agent}'")
    async with SessionLocal() as session:
        run = AgentRun(agent=agent, status="queued", input=message, history=list(history or []),
                       attachments=dict(attachments or {}), variables=dict(variables) if variables else None)
        session.add(run)
        await session.commit()
        return run.id


async def run(agent: str, message: str, history: list[dict[str, Any]] | None = None,
              attachments: dict[str, str] | None = None) -> AgentRun:
    """Поставить ран и дождаться его. Возвращает строку рана с итогом."""
    run_id = await create(agent, message, history, attachments)
    return await execute(run_id)


async def cancel(run_id: int) -> bool:
    async with SessionLocal() as session:
        run_row = await session.get(AgentRun, run_id)
        if run_row is None or run_row.status not in ("queued", "running"):
            return False
        run_row.cancel_requested = True
        if run_row.status == "queued":
            run_row.status, run_row.finished_at = "stopped", _now()
        await session.commit()
        return True


async def recover() -> int:
    """Раны, которые шли у прошлого процесса, уже не доделаются."""
    from sqlalchemy import update

    async with SessionLocal() as session:
        result = await session.execute(
            update(AgentRun).where(AgentRun.status == "running")
            .values(status="error", error="воркер перезапущен посреди рана", finished_at=_now())
            .returning(AgentRun.id)
        )
        count = len(result.scalars().all())
        await session.commit()
    return count


async def execute(run_id: int) -> AgentRun:
    """Исполняет ран целиком и пишет итог; потолок — RUN_TIMEOUT."""
    async with SessionLocal() as session:
        row = await session.get(AgentRun, run_id)
        if row is None:
            raise LookupError(f"agent run {run_id} not found")
        if row.status != "queued":
            return row
        row.status, row.started_at = "running", _now()
        await session.commit()
        agent, task, history, attachments = row.agent, row.input, list(row.history or []), dict(row.attachments or {})
        variables = dict(row.variables or {})

    spec = SPECS[agent]
    stats = {"steps": 0, "tool_calls": 0, "tokens_prompt": 0, "tokens_completion": 0}
    started = time.perf_counter()
    try:
        outcome = await asyncio.wait_for(
            _loop(run_id, spec, task, history, attachments, stats, variables), timeout=settings.run_timeout
        )
    except asyncio.TimeoutError:
        outcome = {"status": "error", "error": f"ран не уложился в {settings.run_timeout} с", "output": ""}
    except asyncio.CancelledError:
        await _update(run_id, status="stopped", error="воркер остановлен", finished_at=_now(),
                      duration_ms=int((time.perf_counter() - started) * 1000), **stats)
        raise
    except Exception as exc:  # noqa: BLE001 — ран не должен застрять в running
        logger.exception("ран %s упал", run_id)
        outcome = {"status": "error", "error": f"{type(exc).__name__}: {exc}", "output": ""}
    await _update(run_id, finished_at=_now(), duration_ms=int((time.perf_counter() - started) * 1000),
                  **outcome, **stats)
    logger.info("ран %s (%s): %s, шагов %s, вызовов %s%s", run_id, agent, outcome["status"], stats["steps"],
                stats["tool_calls"], f" — {outcome['error']}" if outcome.get("error") else "")
    async with SessionLocal() as session:
        return await session.get(AgentRun, run_id)


async def _loop(run_id: int, spec: AgentSpec, task: str, prior: list[dict[str, Any]],
                attachments: dict[str, str], stats: dict[str, int],
                variables: dict[str, str] | None = None) -> dict[str, Any]:
    ctx = ToolContext(spec=spec, graph=settings.graph_name, attachments=attachments)
    tools: dict[str, Tool] = {tool.name: tool for tool in build_tools(ctx)}
    schemas = [tool.schema() for tool in tools.values()] or None
    params = {"temperature": spec.temperature, **settings.llm_extra}
    guard = RepeatGuard(spec.max_repeated_calls)

    if spec.drop_tool_history:
        prior = [t for t in prior if t.get("role") != "tool"]
    prior = trim_history(prior, spec.max_history)
    messages: list[dict[str, Any]] = [{"role": "system", "content": f"{spec.prompt_for(variables)}\n\n{system_text(ctx)}"}]
    messages += [{"role": t["role"], "content": t["content"]} for t in prior
                 if t.get("role") in ("user", "assistant") and t.get("content")]
    messages.append({"role": "user", "content": task})

    for _ in range(spec.max_steps):
        if await _cancelled(run_id):
            return {"status": "stopped", "output": "", "error": ""}
        stats["steps"] += 1
        step = stats["steps"]
        response = await generate(messages, schemas, params)
        if response.get("error"):
            await _log(run_id, step, "error", error=response["error"])
            return {"status": "error", "error": response["error"], "output": ""}
        # Сумма по шагам: каждый шаг заново отправляет весь разговор, и это и
        # есть то, за что платят провайдеру.
        stats["tokens_prompt"] += response["usage"]["prompt_tokens"]
        stats["tokens_completion"] += response["usage"]["completion_tokens"]

        content, calls = response["content"], response["tool_calls"]
        assistant: dict[str, Any] = {"role": "assistant", "content": content}
        if calls:
            assistant["tool_calls"] = calls
        messages.append(assistant)
        thinking = "\n\n".join(t for t in (response["reasoning"], content if calls else None) if t and t.strip())
        if thinking:
            await _log(run_id, step, "thinking", text=thinking)
        if not calls:
            await _log(run_id, step, "final", text=content or "")
            return {"status": "done", "output": content or "", "error": ""}

        per_call = []
        for call in calls:
            replies = await _call_tool(run_id, step, tools, call)
            replies[0]["content"] = guard.check(call["function"]["name"], call["function"]["arguments"],
                                                replies[0]["content"])
            per_call.append(replies)
        messages.extend(step_messages(per_call))
        stats["tool_calls"] += len(calls)
        if guard.exceeded:
            return {"status": "error", "output": "",
                    "error": f"агент повторил тот же вызов с тем же ответом {guard.repeats} раз"}
    return {"status": "error", "output": "", "error": f"за {spec.max_steps} шагов агент не дал ответа"}


async def _call_tool(run_id: int, step: int, tools: dict[str, Tool], call: dict[str, Any]) -> list[dict[str, Any]]:
    name, raw_args, call_id = call["function"]["name"], call["function"]["arguments"] or "{}", call["id"]

    def reply(content: str) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": call_id, "content": content}

    tool = tools.get(name)
    if tool is None:
        await _log(run_id, step, "error", name, error=f"No tool named '{name}'")
        return [reply(f"No tool named '{name}'")]
    try:
        args = json.loads(raw_args)
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except ValueError as exc:
        await _log(run_id, step, "error", name, error=f"Invalid arguments JSON: {exc}", args=raw_args[:2000])
        return [reply(f"Invalid arguments JSON: {exc}")]

    extra: list[dict[str, Any]] = []
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(tool.handler(**args), timeout=settings.tool_timeout)
        content = stringify(result)
        if isinstance(result, _images.ToolResult) and result.images:
            ready, notes = _images.prepare(list(result.images))
            note, extra = image_messages(ready, notes, settings.llm_vision)
            content = f"{content}\n{note}".strip()
        await _log(run_id, step, "tool", name, args=args, result=content[:LOG_RESULT_CHARS],
                   ms=int((time.perf_counter() - started) * 1000))
    except asyncio.TimeoutError:
        content = f"Tool error: {name} did not finish in {settings.tool_timeout}s"
        await _log(run_id, step, "error", name, args=args, error=content)
    except TypeError as exc:
        # Модель передала аргумент, которого у инструмента нет.
        content = f"Tool error: {exc}"
        await _log(run_id, step, "error", name, args=args, error=content)
    except Exception as exc:  # noqa: BLE001
        content = f"Tool error: {type(exc).__name__}: {exc}"
        await _log(run_id, step, "error", name, args=args, error=content)
    return [reply(content), *extra]


async def logs(run_id: int) -> list[AgentLog]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(AgentLog).where(AgentLog.run_id == run_id).order_by(AgentLog.id)
        )).scalars().all())
