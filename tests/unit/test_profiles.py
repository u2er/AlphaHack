"""Профили клиентов: контракт экспорта, промпт аналитика, оценки по профилям — без базы."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eventgraph import analyst, profiles
from eventgraph.agents.specs import SPECS
from eventgraph.config import COMPANY_PROFILE_PLACEHOLDER, Settings, settings
from eventgraph.models import Item

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "profiles" / "vectorfood.json"


def demo() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_an_export_becomes_the_text_the_analyst_reads():
    export, prompt = profiles.parse(demo())
    assert export.profile_id == "demo-vectorfood"
    assert prompt.startswith("Клиент: ВекторФуд") and "Ответы пользователя" in prompt


def test_not_an_export_is_rejected():
    with pytest.raises(ValueError):
        profiles.parse({"profile_id": "x"})
    with pytest.raises(ValueError):
        profiles.parse(demo() | {"schema_version": "2"})


def test_the_run_profile_replaces_the_configured_one(monkeypatch):
    monkeypatch.setattr(settings, "company_profile", "Профиль из настроек")
    spec = SPECS["analyst"]
    assert "Профиль из настроек" in spec.prompt and "{{company_profile}}" not in spec.prompt
    own = spec.prompt_for({"company_profile": "Клиент: ВекторФуд"})
    assert "Клиент: ВекторФуд" in own and "Профиль из настроек" not in own


def test_without_profiles_the_configured_one_is_the_default(monkeypatch):
    monkeypatch.setattr(settings, "company_profile", COMPANY_PROFILE_PLACEHOLDER)
    fallback = profiles.fallback()
    assert (fallback.id, fallback.prompt) == ("default", COMPANY_PROFILE_PLACEHOLDER)


def test_an_empty_profiles_dir_means_none():
    assert Settings(profiles_dir="").profiles_dir is None
    assert Settings(profiles_dir="/profiles").profiles_dir == Path("/profiles")


def test_an_assessment_made_before_profiles_belongs_to_the_default():
    legacy = {"importance": 2, "impact": "Фон.", "run_id": 9}
    assert analyst.by_profile(legacy) == {"default": legacy}
    assert analyst.by_profile(None) == {}
    combined = analyst.combine({"a": {"importance": 1, "impact": "x"}, "b": {"importance": 3, "impact": "y"}})
    assert analyst.by_profile(combined).keys() == {"a", "b"}


def test_each_client_needs_its_own_assessment(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "agents", "news,analyst")
    record = Item(uid="01A", analysis=analyst.combine({"a": {"importance": 1, "impact": "x"}}))
    assert not analyst.needed(record, "a") and analyst.needed(record, "b")


def test_the_top_assessment_is_the_most_important_one():
    combined = analyst.combine({"a": {"error": "503", "run_id": 1}, "b": {"importance": 2, "impact": "y"},
                                "c": {"importance": 4, "impact": "z"}})
    assert (combined["importance"], combined["profile_id"]) == (4, "c")
    only_errors = analyst.combine({"a": {"error": "503", "run_id": 1}})
    assert (only_errors["error"], only_errors["profile_id"]) == ("503", "a")
    assert analyst.combine({}) is None


@pytest.mark.parametrize("assessments, closed", [
    ({"a": {"importance": 1}, "b": {"importance": 2}}, True),
    ({"a": {"importance": 1}, "b": {"importance": 3}}, False),
    ({"a": {"importance": 1}, "b": {"error": "503"}}, False),
    ({"a": {"importance": 1}}, False),  # клиента b ещё не оценили
])
def test_the_threshold_closes_a_record_only_below_it_for_every_client(assessments, closed):
    assert bool(analyst.below_all(assessments, ["a", "b"], 3)) is closed


async def test_the_profiles_dir_loads_new_and_changed_files_once(tmp_path, monkeypatch):
    loaded = []

    async def upsert(payload, source="api"):
        profiles.parse(payload)
        loaded.append((payload["profile_id"], source))

    monkeypatch.setattr(profiles, "upsert", upsert)
    monkeypatch.setattr(profiles, "_seen", {})
    (tmp_path / "demo.json").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")

    assert await profiles.sync_dir(tmp_path) == 1
    assert await profiles.sync_dir(tmp_path) == 0
    assert loaded == [("demo-vectorfood", "file")]
    assert await profiles.sync_dir(tmp_path / "нет") == 0
