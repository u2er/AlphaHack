"""Ран агента: цикл шагов, вызовы инструментов, повторы, картинки — без модели и базы."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from eventgraph.agents import images, runner
from eventgraph.agents.specs import SPECS, AgentSpec
from eventgraph.agents.tools import Tool
from eventgraph.config import settings


def call(name, args, call_id="c1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def answer(content=None, calls=None, reasoning=None, tokens=(10, 2)):
    return {"content": content, "reasoning": reasoning, "tool_calls": calls,
            "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]}}


@pytest.fixture
def loop(monkeypatch):
    """Подставная модель (ответы по очереди) и инструменты; лог — в список."""
    state = {"answers": [], "seen": [], "logs": [], "tools": [], "cancel": False}

    async def generate(messages, tools, params, client=None):
        state["seen"].append([dict(m) for m in messages])
        state["params"] = params
        return state["answers"].pop(0)

    async def log(run_id, step, kind, tool="", **data):
        state["logs"].append((step, kind, tool, data))

    async def cancelled(run_id):
        return state["cancel"]

    monkeypatch.setattr(runner, "generate", generate)
    monkeypatch.setattr(runner, "_log", log)
    monkeypatch.setattr(runner, "_cancelled", cancelled)
    monkeypatch.setattr(runner, "build_tools", lambda ctx: state["tools"])
    monkeypatch.setattr(runner, "system_text", lambda ctx: "## Graph tools")
    return state


def spec(**overrides) -> AgentSpec:
    base = {"name": "news", "prompt_file": "news.md", "max_steps": 5, "temperature": 0.2}
    return AgentSpec(**{**base, **overrides})


async def run_loop(state, task="задача", prior=None, **overrides):
    stats = {"steps": 0, "tool_calls": 0, "tokens_prompt": 0, "tokens_completion": 0}
    outcome = await runner._loop(1, spec(**overrides), task, prior or [], {}, stats)  # noqa: SLF001
    return outcome, stats


async def test_tool_call_then_answer(loop, monkeypatch):
    got = []

    async def lookup(name):
        got.append(name)
        return {"ok": True, "found": False, "name": name}

    loop["tools"] = [Tool("graph_node", "", {"type": "object"}, lookup)]
    loop["answers"] = [answer(calls=[call("graph_node", {"name": "ТГУ"})], reasoning="ищу"), answer("Готово.")]

    outcome, stats = await run_loop(loop)

    assert outcome == {"status": "done", "output": "Готово.", "error": ""}
    assert got == ["ТГУ"]
    assert stats == {"steps": 2, "tool_calls": 1, "tokens_prompt": 20, "tokens_completion": 4}
    second = loop["seen"][1]
    assert second[0]["role"] == "system" and "## Graph tools" in second[0]["content"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool"]
    assert json.loads(second[3]["content"]) == {"ok": True, "found": False, "name": "ТГУ"}
    assert loop["params"]["temperature"] == 0.2
    kinds = [k for _, k, _, _ in loop["logs"]]
    assert kinds == ["thinking", "tool", "final"]


async def test_bad_calls_are_answered_not_raised(loop):
    async def boom():
        raise RuntimeError("сломался")

    loop["tools"] = [Tool("boom", "", {"type": "object"}, boom)]
    bad_json = {"id": "c2", "type": "function", "function": {"name": "boom", "arguments": "{не json"}}
    loop["answers"] = [
        answer(calls=[call("nope", {}), bad_json, call("boom", {}, "c3"), call("boom", {"x": 1}, "c4")]),
        answer("ок"),
    ]
    outcome, _ = await run_loop(loop)
    replies = [m["content"] for m in loop["seen"][1] if m["role"] == "tool"]
    assert replies[0] == "No tool named 'nope'"
    assert replies[1].startswith("Invalid arguments JSON")
    assert replies[2] == "Tool error: RuntimeError: сломался"
    assert replies[3].startswith("Tool error:") and "x" in replies[3]
    assert outcome["status"] == "done"


async def test_the_same_call_for_the_same_answer_stops_the_run(loop):
    async def same(name):
        return {"found": False}

    loop["tools"] = [Tool("graph_node", "", {"type": "object"}, same)]
    loop["answers"] = [answer(calls=[call("graph_node", {"name": "X"})]) for _ in range(5)]

    outcome, stats = await run_loop(loop, max_repeated_calls=2)

    assert outcome["status"] == "error" and "повторил" in outcome["error"]
    assert stats["steps"] == 4  # первый вызов и три повтора — третий уже сверх лимита
    assert "[repeated call]" in loop["seen"][2][-1]["content"]


async def test_max_steps_without_an_answer_is_an_error(loop):
    async def tick():
        return {"n": len(loop["seen"])}

    loop["tools"] = [Tool("tick", "", {"type": "object"}, tick)]
    loop["answers"] = [answer(calls=[call("tick", {})]) for _ in range(3)]
    outcome, _ = await run_loop(loop, max_steps=3)
    assert outcome == {"status": "error", "output": "", "error": "за 3 шагов агент не дал ответа"}


async def test_model_failure_ends_the_run(loop):
    loop["answers"] = [{"error": "Provider returned 401: bad key"}]
    outcome, _ = await run_loop(loop)
    assert outcome["status"] == "error" and "401" in outcome["error"]


async def test_cancel_stops_before_the_next_step(loop):
    loop["cancel"] = True
    outcome, stats = await run_loop(loop)
    assert outcome["status"] == "stopped" and stats["steps"] == 0


async def test_pictures_follow_all_tool_replies_of_the_step(loop, monkeypatch):
    png = images.ToolImage(data=b"x", mime="image/png", name="a.png", width=1, height=1)
    monkeypatch.setattr(images, "prepare", lambda imgs: (imgs, []))

    async def view(path):
        return images.ToolResult({"path": path}, [png])

    async def other():
        return "ok"

    loop["tools"] = [Tool("view_image", "", {"type": "object"}, view), Tool("other", "", {"type": "object"}, other)]
    loop["answers"] = [answer(calls=[call("view_image", {"path": "a.png"}, "c1"), call("other", {}, "c2")]),
                       answer("видел")]

    monkeypatch.setattr(settings, "llm_vision", True)
    await run_loop(loop)
    roles = [m["role"] for m in loop["seen"][1]]
    assert roles == ["system", "user", "assistant", "tool", "tool", "user"]
    assert loop["seen"][1][-1]["content"][1]["type"] == "image_url"

    loop["seen"].clear()
    loop["answers"] = [answer(calls=[call("view_image", {"path": "a.png"})]), answer("не видел")]
    monkeypatch.setattr(settings, "llm_vision", False)
    await run_loop(loop)
    assert [m["role"] for m in loop["seen"][1]] == ["system", "user", "assistant", "tool"]
    assert "cannot see images" in loop["seen"][1][-1]["content"]


async def test_history_is_trimmed_and_tool_turns_dropped(loop):
    prior = []
    for n in range(4):
        prior += [{"role": "user", "content": f"q{n}"}, {"role": "tool", "content": "t"},
                  {"role": "assistant", "content": f"a{n}"}]
    loop["answers"] = [answer("ответ")]
    await run_loop(loop, "q4", prior, max_history=2, drop_tool_history=True)
    assert [m["content"] for m in loop["seen"][0][1:]] == ["q2", "a2", "q3", "a3", "q4"]


def test_trim_history_counts_user_turns_only():
    turns = [{"role": "user"}, {"role": "assistant"}, {"role": "user"}, {"role": "tool"}, {"role": "user"}]
    assert runner.trim_history(turns, 2) == turns[2:]
    assert runner.trim_history(turns, None) == turns


def test_parse_completion_reads_calls_reasoning_and_usage():
    message = SimpleNamespace(
        content=None,
        tool_calls=[SimpleNamespace(id="c1", function=SimpleNamespace(name="graph_node", arguments='{"name": "A"}'))],
        model_dump=lambda exclude_none=True: {"reasoning_content": "думаю"},
    )
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)],
                               usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3))
    parsed = runner.parse_completion(response)
    assert parsed["tool_calls"] == [call("graph_node", {"name": "A"})]
    assert parsed["reasoning"] == "думаю"
    assert parsed["usage"] == {"prompt_tokens": 5, "completion_tokens": 3}


def test_every_agent_has_its_prompt():
    for agent in SPECS.values():
        # Однократной сводке (один шаг, без инструментов) хватает короткого промпта.
        assert len(agent.prompt) > (0 if agent.max_steps == 1 else 1000), agent.name
        # Инструменты в промптах — те, что есть у агентов.
        assert "casket" not in agent.prompt.lower()


def test_only_agents_with_a_key_run(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "")
    assert not settings.agent_on("news")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "agents", "news, gardener,chat")
    assert settings.enabled_agents == {"news", "gardener"}
