from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger("reasoning.agent")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class AgentStepLimitReached(RuntimeError):
    def __init__(self, steps: int) -> None:
        self.steps = steps
        super().__init__(f"Agent reached the limit of {steps} model steps")


class ReActAgent:
    """A small tool-calling agent whose full conversation lives in memory."""

    def __init__(
        self,
        client: Any,
        model: str,
        system_prompt: str,
        tools: list[Tool],
        max_steps: int = 12,
        request_params: dict[str, Any] | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.tools = {tool.name: tool for tool in tools}
        self.max_steps = max_steps
        self.request_params = dict(request_params or {})
        self.history: list[dict[str, Any]] = []

    async def run(self, user_message: str) -> str:
        if not user_message.strip():
            raise ValueError("user_message must not be empty")
        self.history.append({"role": "user", "content": user_message})
        logger.info("user.message %s", json.dumps({"content": user_message}, ensure_ascii=False))
        return await self._run_steps()

    async def continue_run(self) -> str:
        return await self._run_steps()

    async def _run_steps(self) -> str:
        for step in range(1, self.max_steps + 1):
            messages = [
                {"role": "system", "content": self.system_prompt},
                *self.history,
            ]
            logger.info(
                "llm.request %s",
                json.dumps(
                    {"step": step, "model": self.model, "messages": messages},
                    ensure_ascii=False,
                    default=str,
                ),
            )
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=[tool.schema() for tool in self.tools.values()],
                tool_choice="auto",
                **self.request_params,
            )
            message = response.choices[0].message
            calls = list(message.tool_calls or [])
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": message.content,
            }
            if calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments or "{}",
                        },
                    }
                    for call in calls
                ]
            logger.info(
                "llm.response %s",
                json.dumps(
                    {
                        "step": step,
                        "content": message.content,
                        "tool_calls": assistant_message.get("tool_calls", []),
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
            self.history.append(assistant_message)

            if not calls:
                if message.content is None:
                    raise RuntimeError("LLM returned neither text nor tool calls")
                return message.content

            for call in calls:
                result = await self._execute(call.function.name, call.function.arguments or "{}")
                self.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )

        raise AgentStepLimitReached(self.max_steps)

    async def _execute(self, name: str, raw_arguments: str) -> Any:
        tool = self.tools.get(name)
        if tool is None:
            logger.warning("tool.error %s", json.dumps({"tool": name, "error": "Unknown tool"}, ensure_ascii=False))
            return {"ok": False, "error": f"Unknown tool: {name}"}
        logger.info("tool.call %s", json.dumps({"tool": name, "arguments": raw_arguments}, ensure_ascii=False))
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            logger.warning("tool.error %s", json.dumps({"tool": name, "error": str(exc)}, ensure_ascii=False))
            return {"ok": False, "error": f"Invalid JSON arguments: {exc}"}
        if not isinstance(arguments, dict):
            logger.warning(
                "tool.error %s",
                json.dumps({"tool": name, "error": "Tool arguments must be a JSON object"}, ensure_ascii=False),
            )
            return {"ok": False, "error": "Tool arguments must be a JSON object"}
        try:
            result = tool.handler(**arguments)
            if inspect.isawaitable(result):
                result = await result
            logger.info(
                "tool.result %s",
                json.dumps({"tool": name, "result": result}, ensure_ascii=False, default=str),
            )
            return result
        except Exception as exc:
            logger.exception("tool.failure %s", json.dumps({"tool": name}, ensure_ascii=False))
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
