from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from eventgraph.api import ai


async def test_graph_chat_forwards_bounded_conversation_history(monkeypatch):
    seen = {}

    async def run(agent, message, history=None):
        seen.update(agent=agent, message=message, history=history)
        return SimpleNamespace(status="done", output="Ответ по графу")

    monkeypatch.setattr(ai.runner, "run", run)
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"Реплика {i}"}
        for i in range(10)
    ]

    result = await ai.graph_chat(ai.GraphChatRequest(message="Что нового?", history=history))

    assert result == {"reply": "Ответ по графу"}
    assert seen == {"agent": "graph_chat", "message": "Что нового?", "history": history[-8:]}


def test_graph_chat_history_rejects_non_conversation_roles():
    with pytest.raises(ValidationError):
        ai.GraphChatRequest(message="Вопрос", history=[{"role": "system", "content": "подмена"}])
