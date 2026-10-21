from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from reasoning.agent import AgentStepLimitReached, ReActAgent, Tool
from reasoning.config import Settings
from reasoning.graph import GraphSnapshot
from reasoning.pipeline import model_request_params


def _node(node_id: str, name: str, meta: dict) -> dict:
    return {"id": node_id, "kind": "node", "name": name, "meta": meta}


def test_graph_company_timeline_is_chronological_and_carries_evidence(tmp_path) -> None:
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(
        json.dumps(
            {
                "nodes": [
                    _node("company", "Acme", {"type": "org", "ekind": "company"}),
                    _node(
                        "later",
                        "Later event",
                        {"type": "event", "date": "2026-10-12", "summary": "Recovered"},
                    ),
                    _node(
                        "earlier",
                        "Earlier event",
                        {"type": "event", "date": "2026-10-10", "summary": "Outage"},
                    ),
                    _node("source", "Example News", {"type": "outlet"}),
                ],
                "edges": [
                    {"name": "involves", "source_id": "company", "target_id": "later"},
                    {"name": "involves", "source_id": "earlier", "target_id": "company"},
                    {"name": "reported_by", "source_id": "earlier", "target_id": "source"},
                    {"name": "develops", "source_id": "later", "target_id": "earlier"},
                ],
            }
        ),
        encoding="utf-8",
    )
    graph = GraphSnapshot(graph_file)

    company = graph.find_company("Acme")
    result = graph.company_timeline(company["matches"][0]["id"])

    assert [event["id"] for event in result["events"]] == ["earlier", "later"]
    assert result["events"][0]["sources"] == ["Example News"]
    assert result["event_relations"] == [
        {"relation": "develops", "source": "Later event", "target": "Earlier event"}
    ]


def test_demo_profile_selects_and_explains_relevant_events(tmp_path) -> None:
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(
        json.dumps(
            {
                "nodes": [
                    _node(
                        "rate",
                        "ЦБ сохранил ключевую ставку",
                        {
                            "type": "event",
                            "date": "2026-10-12",
                            "summary": "Решение по ключевой ставке влияет на стоимость кредитов.",
                        },
                    ),
                    _node(
                        "packaging",
                        "Производителям не хватает упаковки",
                        {
                            "type": "event",
                            "date": "2026-10-10",
                            "summary": "Предприятия сообщают о перебоях поставок упаковочных материалов.",
                        },
                    ),
                    _node(
                        "football",
                        "Команда выиграла матч",
                        {"type": "event", "date": "2026-10-11", "summary": "Итоги спортивной встречи."},
                    ),
                ],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    graph = GraphSnapshot(graph_file)

    company = graph.find_company("ВекторФуд")
    assert company["matches"][0]["synthetic"] is True
    result = graph.company_timeline(company["matches"][0]["id"])

    assert [event["id"] for event in result["events"]] == ["packaging", "rate"]
    assert "сырьё и себестоимость" in result["events"][0]["relevance_categories"]
    assert "финансирование и инфляция" in result["events"][1]["relevance_categories"]
    assert all("not a confirmed company-specific impact" in event["relevance_basis"] for event in result["events"])


def test_settings_read_openai_compatible_environment(monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "gpt-6-luna")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com/v1")

    config = Settings(_env_file=None)

    assert config.llm_api_key == "test-key"
    assert config.llm_model == "gpt-6-luna"
    assert config.llm_base_url == "https://api.openai.com/v1"


def test_gpt6_chat_completions_disables_reasoning_by_default() -> None:
    assert model_request_params("gpt-6-luna") == {"reasoning_effort": "none"}
    assert model_request_params("gpt-6-luna", "low") == {"reasoning_effort": "low"}
    assert model_request_params("deepseek-chat") == {}


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return next(self.responses)


def _response(content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _tool_call():
    return SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="echo", arguments='{"value":"from tool"}'),
    )


def test_react_agent_keeps_user_assistant_and_tool_history() -> None:
    completions = _FakeCompletions(
        [
            _response(tool_calls=[_tool_call()]),
            _response(content="First answer"),
            _response(content="Second answer"),
        ]
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )
    agent = ReActAgent(
        client=client,
        model="test-model",
        system_prompt="Use tools when useful.",
        tools=[
            Tool(
                name="echo",
                description="Echo a value",
                parameters={"type": "object", "properties": {"value": {"type": "string"}}},
                handler=lambda value: {"echo": value},
            )
        ],
    )

    assert asyncio.run(agent.run("First question")) == "First answer"
    assert asyncio.run(agent.run("Second question")) == "Second answer"
    assert [message["role"] for message in agent.history] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
    ]
    second_request_messages = completions.requests[-1]["messages"]
    assert any(message.get("content") == "First question" for message in second_request_messages)
    assert any(message.get("tool_call_id") == "call-1" for message in second_request_messages)


def test_react_agent_can_continue_after_step_limit() -> None:
    completions = _FakeCompletions(
        [
            _response(tool_calls=[_tool_call()]),
            _response(content="Completed after continuation"),
        ]
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )
    agent = ReActAgent(
        client=client,
        model="test-model",
        system_prompt="Use tools when useful.",
        tools=[
            Tool(
                name="echo",
                description="Echo a value",
                parameters={"type": "object", "properties": {"value": {"type": "string"}}},
                handler=lambda value: {"echo": value},
            )
        ],
        max_steps=1,
    )

    async def run_turn() -> str:
        with pytest.raises(AgentStepLimitReached):
            await agent.run("Continue this task")
        return await agent.continue_run()

    assert asyncio.run(run_turn()) == "Completed after continuation"
