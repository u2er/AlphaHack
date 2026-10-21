"""Настройки разбора и обслуживания, пауза после сбоя — без базы."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from eventgraph.graph_settings import GraphSettings, pause_after, paused_until

NOW = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"timezone": "Mars/Olympus"}, "часовой пояс"),
        ({"item_images": 11}, "Картинок"),
        ({"maintenance_every_days": 0}, "Обслуживание"),
        ({"maintenance_tasks": ["weeding"]}, "Задачи обслуживания"),
        ({"merge_pairs": 100}, "Пар и островков"),
        ({"prune_max_importance": 6}, "Важность"),
        ({"min_importance": 6}, "Порог важности"),
    ],
)
def test_settings_validation(overrides, fragment):
    assert fragment in GraphSettings.from_dict(overrides).validate()


def test_defaults_are_valid_and_old_schedule_keys_are_ignored():
    # Ключи расписания, пачек и квоты из сохранённых настроек не мешают.
    c = GraphSettings.from_dict({"mode": "daily", "interval_minutes": 60, "batch_items": 15, "daily_limit": 5})
    assert c.validate() == "" and c.enabled is True
    assert not hasattr(c, "mode") and not hasattr(c, "batch_items")


def test_pause_grows_with_failures_and_has_a_ceiling():
    assert pause_after(1, NOW) == NOW + timedelta(seconds=30)
    assert pause_after(3, NOW) == NOW + timedelta(seconds=120)
    assert pause_after(50, NOW) == NOW + timedelta(minutes=15)
    assert paused_until({"paused_until": None}) is None
    assert paused_until({"paused_until": (NOW + timedelta(minutes=1)).isoformat()}) == NOW + timedelta(minutes=1)
