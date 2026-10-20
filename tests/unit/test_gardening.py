"""Садовник: когда обслуживать граф, что ему сказать и как прочесть итог."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from eventgraph import gardening
from eventgraph.config import settings
from eventgraph.graph_settings import GraphSettings

NOW = datetime(2026, 10, 15, 3, 30, tzinfo=timezone.utc)


@pytest.fixture
def gardener(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "test")
    monkeypatch.setattr(settings, "agents", "news,gardener")


def test_maintenance_waits_its_days(gardener):
    cfg = GraphSettings(maintenance_every_days=7)
    # Нечего обслуживать: ни прошлого обслуживания, ни разобранных записей.
    assert not gardening.due(cfg, None, NOW)
    assert not gardening.due(cfg, NOW - timedelta(days=6, hours=23), NOW)
    assert gardening.due(cfg, NOW - timedelta(days=7), NOW)


def test_no_maintenance_without_gardener_or_tasks(monkeypatch, gardener):
    long_ago = NOW - timedelta(days=30)
    assert not gardening.due(GraphSettings(maintenance_enabled=False), long_ago, NOW)
    assert not gardening.due(GraphSettings(maintenance_tasks=[]), long_ago, NOW)
    monkeypatch.setattr(settings, "agents", "news")
    assert not gardening.due(GraphSettings(), long_ago, NOW)


def test_task_message_names_the_task_and_its_limits():
    cfg = GraphSettings(merge_pairs=12, island_limit=7, prune_older_days=45)
    assert gardening.task_message("duplicates", cfg).startswith("Задача: дубли. Лимит: 12 пар")
    assert "limit=7" in gardening.task_message("islands", cfg)
    prune = gardening.task_message("prune", cfg)
    assert prune.startswith("Задача: обрезка. Параметры")
    assert 'older_than_days=45' in prune and 'protect_topics=["Томск"]' in prune


REPLY = """Слил два написания Минобороны, ФНС и ФТС — разные.

Пример фигурных скобок: {"task": "цитата"} в тексте не мешает.

```json
{"task": "дубли",
 "merged": [{"kept": "Министерство обороны Российской Федерации", "dropped": "Минобороны России"},
            {"kept": "", "dropped": "без пары"}],
 "rejected": 4, "left": "2", "linked": 0, "checked": 0, "deleted": ["Пустой узел"], "pruned": -3}
```"""


def test_report_is_read_from_the_last_block():
    report = gardening.parse_report(REPLY)
    assert report == {
        "merged": [{"kept": "Министерство обороны Российской Федерации", "dropped": "Минобороны России"}],
        "deleted": ["Пустой узел"],
        "rejected": 4, "left": 2, "linked": 0, "checked": 0, "pruned": 0,
    }
    assert gardening.strip_report(REPLY).endswith("в тексте не мешает.")


def test_no_report_without_a_block():
    assert gardening.parse_report("Ничего не сделал.") is None
    assert gardening.parse_report('```json\n{"items": []}\n```') is None
    assert gardening.strip_report("Ничего не сделал.") == "Ничего не сделал."


def test_settings_validate_maintenance():
    assert GraphSettings().validate() == ""
    assert "Задачи обслуживания" in GraphSettings(maintenance_tasks=["weeding"]).validate()
    assert GraphSettings(maintenance_every_days=0).validate()
    assert GraphSettings(merge_pairs=100).validate()
