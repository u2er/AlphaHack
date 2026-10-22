"""Разбор записей агентом: сообщение, вердикт, попытки — без базы и модели."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from eventgraph import news, profiles
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
    state = {"messages": [], "attachments": [], "replies": [], "stored": {}, "events": [], "agents": [],
             "variables": [], "clients": [profiles.fallback()]}
    record = item("01A", "Мэр ушёл", title="Мэр ушёл", url="https://x/1")
    state["item"] = record

    async def create(agent, message, history=None, attachments=None, variables=None):
        state["agents"].append(agent)
        state["messages"].append(message)
        state["attachments"].append(attachments or {})
        state["variables"].append(variables or {})
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
            state["stored"] = {"verdict": record.verdict, "attempts": record.attempts, "run": record.agent_run_id,
                               "analysis": record.analysis}

    async def story_events(it):
        return state["events"]

    async def active():
        return state["clients"]

    monkeypatch.setattr(news.runner, "create", create)
    monkeypatch.setattr(news.runner, "execute", execute)
    monkeypatch.setattr(news, "SessionLocal", Session)
    monkeypatch.setattr(news, "story_events", story_events)
    monkeypatch.setattr(news.profiles, "active", active)
    monkeypatch.setattr(settings, "news_max_attempts", 3)
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "agents", "news")
    return state


def done(uid="01A", verdict="stored"):
    return {"status": "done", "output": f'Добавил.\n```json\n{{"items": [{{"uid": "{uid}", "verdict": "{verdict}", "event": "E"}}]}}\n```'}


async def test_a_record_is_one_run_with_its_verdict(world):
    world["replies"].append(done())
    world["events"] = ["2026-10-07 · Событие"]

    result = await news.process(world["item"], "ria", GraphSettings())

    assert result["verdict"] == "stored" and result["ok"] is True and result["run_id"] == 41
    assert (result["steps"], result["tool_calls"], result["tokens"]) == (7, 12, 1200)
    assert world["stored"] == {"verdict": "stored", "attempts": 0, "run": 41, "analysis": None}
    assert world["agents"] == ["news"]
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


# ── аналитик перед разбором ─────────────────────────────────────────────────


def assessed(uid="01A", importance=4):
    return {"status": "done", "output": f'Риск.\n```json\n{{"analysis": {{"uid": "{uid}", "importance": {importance}, '
                                        '"impact": "Ставка дорожает — спрос на кредиты падает.", "direction": "risk"}}\n```'}


async def test_the_analyst_assesses_the_record_before_the_news_agent(world, monkeypatch):
    monkeypatch.setattr(settings, "agents", "news,analyst")
    world["replies"] += [assessed(), done()]
    world["events"] = ["2026-10-07 · Событие"]

    result = await news.process(world["item"], "ria", GraphSettings())

    assert world["agents"] == ["analyst", "news"] and result["importance"] == 4
    analysis_message, news_message = world["messages"]
    assert "Оцени запись для компании." in analysis_message and "Верни вердикт" not in analysis_message
    assert "Сюжет: в графе по нему уже есть" in analysis_message and analysis_message.endswith("Мэр ушёл")
    stored = world["stored"]["analysis"]
    assert (stored["importance"], stored["direction"], stored["run_id"]) == (4, "risk", 41)
    assert world["stored"]["verdict"] == "stored"


async def test_a_retry_keeps_the_assessment_and_does_not_ask_again(world, monkeypatch):
    monkeypatch.setattr(settings, "agents", "news,analyst")
    world["replies"] += [assessed(), {"status": "done", "output": "Не понял."}]
    await news.process(world["item"], "ria", GraphSettings())
    assert world["stored"]["attempts"] == 1 and world["stored"]["analysis"]["importance"] == 4

    world["replies"].append(done())
    await news.process(world["item"], "ria", GraphSettings())
    assert world["agents"] == ["analyst", "news", "news"]
    assert world["stored"]["analysis"]["importance"] == 4


async def test_a_failed_assessment_does_not_stop_the_record(world, monkeypatch):
    monkeypatch.setattr(settings, "agents", "news,analyst")
    world["replies"] += [{"status": "error", "error": "Provider returned 503"}, done()]

    result = await news.process(world["item"], "ria", GraphSettings())

    assert result["verdict"] == "stored" and result["importance"] is None
    failed = {"error": "Provider returned 503", "run_id": 41}
    assert world["stored"]["analysis"] == {**failed, "profile_id": "default", "profiles": {"default": failed}}


async def test_a_record_below_the_threshold_never_reaches_the_news_agent(world, monkeypatch):
    monkeypatch.setattr(settings, "agents", "news,analyst")
    world["replies"].append(assessed(importance=1))

    result = await news.process(world["item"], "ria", GraphSettings(min_importance=3))

    assert world["agents"] == ["analyst"]
    assert (result["verdict"], result["result"], result["ok"], result["importance"], result["run_id"]) == (
        "below_threshold", "done", True, 1, 41)
    record = world["item"]
    assert record.processed_at is not None and record.outcome == {"reason": "важность для компании 1 ниже порога 3"}
    assert record.analysis["importance"] == 1 and record.agent_run_id == 41


async def test_the_threshold_lets_important_and_unassessed_records_through(world, monkeypatch):
    monkeypatch.setattr(settings, "agents", "news,analyst")
    world["replies"] += [assessed(importance=3), done()]
    assert (await news.process(world["item"], "ria", GraphSettings(min_importance=3)))["verdict"] == "stored"

    # Аналитик сорвался — запись без оценки идёт в разбор, а не теряется.
    world["item"].analysis = None
    world["replies"] += [{"status": "error", "error": "503"}, done()]
    assert (await news.process(world["item"], "ria", GraphSettings(min_importance=5)))["verdict"] == "stored"
    assert world["agents"] == ["analyst", "news", "analyst", "news"]


async def test_a_requeued_record_is_checked_against_the_new_threshold_without_a_new_assessment(world, monkeypatch):
    monkeypatch.setattr(settings, "agents", "news,analyst")
    world["item"].analysis = {"importance": 2, "impact": "Фон.", "run_id": 9}
    world["replies"].append(done())
    assert (await news.process(world["item"], "ria", GraphSettings(min_importance=2)))["verdict"] == "stored"
    assert world["agents"] == ["news"]


# ── несколько клиентов ──────────────────────────────────────────────────────


def clients(*ids):
    return [profiles.Active(i, f"Клиент {i}", f"Профиль клиента {i}") for i in ids]


async def test_each_client_gets_its_own_assessment_with_its_own_profile(world, monkeypatch):
    monkeypatch.setattr(settings, "agents", "news,analyst")
    world["clients"] = clients("steel", "food")
    world["replies"] += [assessed(importance=1), assessed(importance=4), done()]

    result = await news.process(world["item"], "ria", GraphSettings())

    assert world["agents"] == ["analyst", "analyst", "news"]
    assert [v["company_profile"] for v in world["variables"][:2]] == ["Профиль клиента steel", "Профиль клиента food"]
    analysis = world["stored"]["analysis"]
    assert (analysis["importance"], analysis["profile_id"], result["importance"]) == (4, "food", 4)
    assert {pid: a["importance"] for pid, a in analysis["profiles"].items()} == {"steel": 1, "food": 4}


async def test_a_record_important_to_one_client_reaches_the_shared_graph(world, monkeypatch):
    monkeypatch.setattr(settings, "agents", "news,analyst")
    world["clients"] = clients("steel", "food")
    world["replies"] += [assessed(importance=1), assessed(importance=3), done()]
    assert (await news.process(world["item"], "ria", GraphSettings(min_importance=3)))["verdict"] == "stored"


async def test_a_record_below_the_threshold_for_every_client_is_closed(world, monkeypatch):
    monkeypatch.setattr(settings, "agents", "news,analyst")
    world["clients"] = clients("steel", "food")
    world["replies"] += [assessed(importance=1), assessed(importance=2)]

    result = await news.process(world["item"], "ria", GraphSettings(min_importance=3))

    assert world["agents"] == ["analyst", "analyst"] and result["verdict"] == "below_threshold"
    assert world["item"].outcome == {"reason": "важность ниже порога 3 для всех клиентов (2), наибольшая — 2"}


async def test_a_new_client_is_assessed_without_repeating_the_others(world, monkeypatch):
    monkeypatch.setattr(settings, "agents", "news,analyst")
    world["item"].analysis = {"importance": 2, "impact": "Фон.", "run_id": 9}  # оценка до профилей — default
    world["clients"] = [profiles.fallback(), *clients("food")]
    world["replies"] += [assessed(importance=5), done()]

    await news.process(world["item"], "ria", GraphSettings())

    assert world["agents"] == ["analyst", "news"]
    assert set(world["stored"]["analysis"]["profiles"]) == {"default", "food"}


async def test_a_client_gets_records_only_from_the_sources_it_chose(world, monkeypatch):
    monkeypatch.setattr(settings, "agents", "news,analyst")
    world["clients"] = [profiles.Active("steel", "Сталь", "Профиль", sources=("lenta_economics",)),
                        profiles.Active("food", "Еда", "Профиль", sources=("alfa_analytics",))]
    world["replies"] += [assessed(importance=3), done()]

    await news.process(world["item"], "alfa_analytics", GraphSettings())

    assert [v.get("company_profile") for v in world["variables"]][:1] == ["Профиль"]
    assert set(world["stored"]["analysis"]["profiles"]) == {"food"}


async def test_a_record_nobody_subscribed_to_is_closed_without_agents(world, monkeypatch):
    monkeypatch.setattr(settings, "agents", "news,analyst")
    world["clients"] = [profiles.Active("steel", "Сталь", "Профиль", sources=("lenta_economics",))]

    result = await news.process(world["item"], "alfa_investments", GraphSettings())

    assert world["agents"] == [] and result["verdict"] == "below_threshold"
    assert world["item"].outcome == {"reason": "ни один клиент не выбрал этот источник"}
