"""Разбор записей агентом: сообщение, вердикт, попытки — без базы и модели."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from eventgraph import news
from eventgraph.config import settings
from eventgraph.graph_settings import GraphSettings
from eventgraph.models import AgentRun, Item

NOW = datetime(2026, 10, 8, 9, 30, tzinfo=timezone.utc)


def item(uid: str = "01A", text: str = "текст", **data) -> Item:
    return Item(id=1, uid=uid, connector_id=1, external_id=uid, text=text, data=data,
                collected_at=NOW, attempts=0, images=[])


# ── сообщение ────────────────────────────────────────────────────────────────


def test_render_gives_the_agent_what_its_prompt_describes():
    message = news.render(item("01A", "Текст новости", title="Заголовок", url="https://x/1", published="2026-10-08"),
                          "ria", NOW, 4000)
    assert message.startswith("Сегодня: 2026-10-08 09:30 UTC.\n\nВерни вердикт по записи.")
    assert message.endswith("### 01A · ria · 2026-10-08 · https://x/1\nЗаголовок\nТекст новости")
    assert "Сюжет" not in message and "картинки" not in message


def test_render_does_not_repeat_a_title_the_text_starts_with_and_cuts_long_text():
    message = news.render(item("01A", "Заголовок. " + "слово " * 1000, title="Заголовок."), "s", NOW, 100)
    assert message.count("Заголовок.") == 1 and message.endswith("…[обрезано]")


def test_render_names_the_story_events_and_the_pictures():
    message = news.render(item(), "s", NOW, 4000, ["01A-0.jpg"], ["2026-10-07 · Событие"])
    assert "Сюжет: в графе по нему уже есть «2026-10-07 · Событие»" in message
    assert "картинки: 01A-0.jpg" in message and "одним шагом" in message


# ── вердикт ──────────────────────────────────────────────────────────────────


def test_verdict_comes_from_the_last_json_block():
    output = (
        'Сводка: {"items": "не то"}\n```json\n{"items": [{"uid": "01A", "verdict": "skipped"}]}\n```\n'
        '```json\n{"items": [{"uid": "01A", "verdict": "STORED", "event": "E"}, {"uid": "чужой", "verdict": "stored"}]}\n```'
    )
    assert news.parse_verdicts(output, {"01A"}) == {"01A": {"verdict": "stored", "event": "E"}}


@pytest.mark.parametrize("output", ["", "без блока", "```json\n{битый}\n```", '```json\n{"x": 1}\n```',
                                    '```json\n{"items": [{"uid": "01A", "verdict": "filtered"}]}\n```'])
def test_no_usable_block_means_no_verdict(output):
    assert news.parse_verdicts(output, {"01A"}) == {}


def test_settle_marks_the_verdict_and_counts_attempts(monkeypatch):
    monkeypatch.setattr(settings, "news_max_attempts", 2)
    done, missing = item("01A"), item("01B")

    assert news.settle(done, {"verdict": "skipped", "reason": "реклама"}, 7, NOW) == "done"
    assert (done.processed_at, done.verdict, done.outcome, done.agent_run_id) == (NOW, "skipped", {"reason": "реклама"}, 7)

    assert news.settle(missing, None, 7, NOW) == "retry"
    assert missing.processed_at is None and missing.attempts == 1
    assert news.settle(missing, None, 8, NOW, error="таймаут") == "failed"
    assert (missing.processed_at, missing.verdict, missing.outcome, missing.agent_run_id) == (
        NOW, "failed", {"reason": "таймаут"}, 8)


def test_summary_drops_the_verdict_block():
    assert news.strip_verdicts('Итог.\n```json\n{"items": []}\n```') == "Итог."
    assert news.strip_verdicts("Просто текст") == "Просто текст"


# ── ран на запись — с подставным агентом и базой ────────────────────────────


@pytest.fixture
def world(monkeypatch):
    state = {"messages": [], "attachments": [], "replies": [], "stored": {}, "events": []}
    record = item("01A", "Мэр ушёл", title="Мэр ушёл", url="https://x/1")
    state["item"] = record

    async def create(agent, message, history=None, attachments=None):
        assert agent == "news"
        state["messages"].append(message)
        state["attachments"].append(attachments or {})
        return 40 + len(state["messages"])

    async def execute(run_id):
        reply = state["replies"].pop(0)
        return AgentRun(id=run_id, output=reply.get("output", ""), status=reply["status"], error=reply.get("error", ""),
                        steps=7, tool_calls=12, tokens_prompt=1000, tokens_completion=200, duration_ms=65000)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, model, item_id):
            return record

        async def commit(self):
            state["stored"] = {"verdict": record.verdict, "attempts": record.attempts, "run": record.agent_run_id}

    async def story_events(it):
        return state["events"]

    monkeypatch.setattr(news.runner, "create", create)
    monkeypatch.setattr(news.runner, "execute", execute)
    monkeypatch.setattr(news, "SessionLocal", Session)
    monkeypatch.setattr(news, "story_events", story_events)
    monkeypatch.setattr(settings, "news_max_attempts", 3)
    return state


def done(uid="01A", verdict="stored"):
    return {"status": "done", "output": f'Добавил.\n```json\n{{"items": [{{"uid": "{uid}", "verdict": "{verdict}", "event": "E"}}]}}\n```'}


async def test_a_record_is_one_run_with_its_verdict(world):
    world["replies"].append(done())
    world["events"] = ["2026-10-07 · Событие"]

    result = await news.process(world["item"], "ria", GraphSettings())

    assert result["verdict"] == "stored" and result["ok"] is True and result["run_id"] == 41
    assert (result["steps"], result["tool_calls"], result["tokens"]) == (7, 12, 1200)
    assert world["stored"] == {"verdict": "stored", "attempts": 0, "run": 41}
    assert world["messages"][0].count("### ") == 1 and "Сюжет: в графе по нему уже есть" in world["messages"][0]


async def test_an_answer_without_a_verdict_is_retried_but_is_not_an_outage(world):
    world["replies"].append({"status": "done", "output": "Не понял."})
    result = await news.process(world["item"], "ria", GraphSettings())
    assert result["result"] == "retry" and result["ok"] is True and "вердикт" in result["error"]
    assert world["stored"]["attempts"] == 1


async def test_a_failed_run_is_an_outage(world):
    world["replies"].append({"status": "error", "error": "Provider returned 503"})
    result = await news.process(world["item"], "ria", GraphSettings())
    assert result["ok"] is False and "503" in result["error"] and world["stored"]["attempts"] == 1


async def test_pictures_go_to_the_agent_by_name_when_it_can_see(world, monkeypatch, tmp_path):
    (tmp_path / "items").mkdir()
    for name in ("a-0.jpg", "a-1.png", "a-2.jpg"):
        (tmp_path / "items" / name).write_bytes(b"\xff\xd8\xff" + b"0" * 10)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    world["item"].images = ["items/a-0.jpg", "items/a-1.png", "items/a-2.jpg"]

    monkeypatch.setattr(settings, "llm_vision", True)
    world["replies"].append(done())
    await news.process(world["item"], "s", GraphSettings(item_images=2))
    assert world["attachments"][0] == {"01A-0.jpg": "items/a-0.jpg", "01A-1.png": "items/a-1.png"}
    assert "картинки: 01A-0.jpg, 01A-1.png" in world["messages"][0]

    # Модель без зрения картинок не получает.
    monkeypatch.setattr(settings, "llm_vision", False)
    world["replies"].append(done())
    await news.process(world["item"], "s", GraphSettings(item_images=2))
    assert world["attachments"][1] == {} and "картинки:" not in world["messages"][1]


async def test_an_empty_queue_means_nothing_to_do(monkeypatch):
    async def nothing():
        return None

    monkeypatch.setattr(news, "next_item", nothing)
    assert await news.process_next(GraphSettings()) is None
