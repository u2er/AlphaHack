"""Трассировка Langfuse: выключена без ключей, пишет генерации и инструменты — без сервера Langfuse."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from eventgraph.agents import runner
from eventgraph.config import settings
from utils import llm, tracing
from utils.config import Settings as LLMSettings
from utils.models import StrictModel


class Span:
    def __init__(self, name, as_type, fields):
        self.name, self.as_type, self.fields = name, as_type, dict(fields)

    def update(self, **fields):
        self.fields.update(fields)
        return self


class Langfuse:
    """Подставной клиент: открытые наблюдения — в список, по порядку."""

    def __init__(self):
        self.spans = []

    @contextmanager
    def start_as_current_observation(self, *, name, as_type="span", **fields):
        span = Span(name, as_type, fields)
        self.spans.append(span)
        yield span

    def by_type(self, as_type):
        return [s for s in self.spans if s.as_type == as_type]


@pytest.fixture
def langfuse(monkeypatch):
    fake = Langfuse()
    monkeypatch.setattr(tracing, "_client", fake)
    monkeypatch.setattr(tracing, "_tried", True)
    return fake


def test_off_without_keys(monkeypatch):
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tracing, "_client", None)
    monkeypatch.setattr(tracing, "_tried", False)
    assert not tracing.enabled()
    with tracing.observe("x", input="y") as span:
        span.update(output="z")
    assert span is tracing.NOOP and tracing.client() is None
    with tracing.attributes(session_id="s"):
        pass


def test_long_fields_are_clipped(langfuse):
    with tracing.observe("x", input={"text": "я" * (tracing.MAX_FIELD_CHARS + 50)}):
        pass
    text = langfuse.spans[0].fields["input"]["text"]
    assert len(text) < tracing.MAX_FIELD_CHARS + 20 and text.endswith("[+50]")


def test_block_error_marks_span_and_propagates(langfuse):
    with pytest.raises(ValueError):
        with tracing.observe("x"):
            raise ValueError("boom")
    assert langfuse.spans[0].fields["level"] == "ERROR"


async def test_agent_step_becomes_generation_with_usage(langfuse, monkeypatch):
    monkeypatch.setattr(settings, "llm_model", "m1")
    message = SimpleNamespace(content="ответ", tool_calls=None, model_dump=lambda exclude_none=True: {})
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)],
                               usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3))

    async def create(**kwargs):
        return response

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    image = {"role": "user", "content": [{"type": "text", "text": "кадр"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}
    result = await runner.generate([{"role": "system", "content": "s"}, image], None, {"temperature": 0.2}, client)
    assert result["content"] == "ответ"
    [gen] = langfuse.by_type("generation")
    assert gen.fields["model"] == "m1"
    assert gen.fields["usage_details"] == {"input": 7, "output": 3}
    assert gen.fields["output"]["content"] == "ответ"
    # Картинки в трейс не уходят.
    assert "base64" not in json.dumps(gen.fields["input"])


async def test_agent_step_error_is_marked(langfuse, monkeypatch):
    monkeypatch.setattr(settings, "llm_retries", 1)

    async def create(**kwargs):
        raise RuntimeError("недоступна")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = await runner.generate([{"role": "user", "content": "x"}], None, {}, client)
    assert "недоступна" in result["error"]
    assert langfuse.by_type("generation")[0].fields["level"] == "ERROR"


class Answer(StrictModel):
    text: str


def structured_client(handler):
    config = LLMSettings(base_url="https://llm.example/v1", model="m2", api_key=SecretStr("k"),
                         structured_output="json_schema")
    return llm.OpenAICompatibleClient(config, transport=httpx.MockTransport(handler))


def test_structured_call_is_a_chain_with_a_generation(langfuse):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"text": "да"}'}}],
                                         "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13}})

    result = llm.generate_structured(structured_client(handler), "system", {"q": 1}, Answer)
    assert result.text == "да"
    [chain] = langfuse.by_type("chain")
    [gen] = langfuse.by_type("generation")
    assert chain.name == "structured:Answer" and chain.fields["output"] == {"text": "да"}
    assert chain.fields["metadata"]["attempts"][0]["valid"] is True
    assert gen.fields["usage_details"] == {"input": 11, "output": 2}
    assert gen.fields["metadata"]["endpoint"] == "/chat/completions"


def test_structured_http_error_is_marked(langfuse):
    with pytest.raises(llm.LLMError):
        llm.generate_structured(structured_client(lambda request: httpx.Response(400, json={})), "s", {}, Answer)
    assert langfuse.by_type("generation")[0].fields["level"] == "ERROR"
    assert langfuse.by_type("chain")[0].fields["level"] in ("ERROR", "WARNING")
