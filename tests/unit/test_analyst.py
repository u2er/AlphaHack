"""Оценка записи для компании: ответ аналитика и его промпт — без базы и модели."""

from __future__ import annotations

import pytest

from eventgraph import analyst
from eventgraph.agents.specs import SPECS
from eventgraph.config import COMPANY_PROFILE_PLACEHOLDER, Settings, settings
from eventgraph.models import Item


def block(**fields) -> str:
    import json

    return f"Итог.\n```json\n{json.dumps({'analysis': {'uid': '01A', **fields}}, ensure_ascii=False)}\n```"


def test_the_assessment_keeps_known_fields_and_drops_the_rest():
    output = block(importance="4", impact="Конкурент снизил цены.", relevance="DIRECT", direction="угроза",
                   horizon="months", trend="escalates", areas=["Клиенты", "погода", "клиенты"],
                   entities=[{"name": "Банк Б", "role": "competitor"}, {"name": "ЦБ", "role": "boss"}, {"role": "x"}],
                   related_events=["2026-10-01 · Банк Б снизил ставки"], actions=["a", "b", "c", "d"],
                   confidence=1.7, headline="Ценовая война", extra="мусор")
    assert analyst.parse_analysis(output, "01A") == {
        "importance": 4, "impact": "Конкурент снизил цены.", "relevance": "direct", "horizon": "months",
        "trend": "escalates", "areas": ["клиенты"],
        "entities": [{"name": "Банк Б", "role": "competitor"}, {"name": "ЦБ", "role": "other"}],
        "related_events": ["2026-10-01 · Банк Б снизил ставки"], "actions": ["a", "b", "c"],
        "confidence": 1.0, "headline": "Ценовая война",
    }


def test_the_last_analysis_block_wins_and_a_bare_one_is_read_too():
    output = block(importance=1, impact="старое") + "\n" + block(importance=0, impact="Не про компанию.")
    assert analyst.parse_analysis(output, "01A") == {"importance": 0, "impact": "Не про компанию."}
    bare = 'Итог. {"analysis": {"uid": "01A", "importance": 2, "impact": "Фон."}}'
    assert analyst.parse_analysis(bare, "01A") == {"importance": 2, "impact": "Фон."}


@pytest.mark.parametrize("output", [
    "", "без блока", "```json\n{битый}\n```", '```json\n{"items": []}\n```',
    block(importance=6, impact="x"), block(importance="много", impact="x"), block(importance=3),
    block(importance=3, impact="x").replace("01A", "чужой"),
])
def test_no_usable_assessment(output):
    assert analyst.parse_analysis(output, "01A") is None


def test_the_profile_goes_into_the_prompt(monkeypatch):
    prompt = SPECS["analyst"].prompt
    assert "{{company_profile}}" not in prompt and "[ПРОФИЛЬ КОМПАНИИ НЕ ЗАДАН" in prompt
    monkeypatch.setattr(settings, "company_profile", "Название: Альфа")
    assert "## The company\n\nНазвание: Альфа\n" in SPECS["analyst"].prompt
    # Для остальных агентов профиль не нужен.
    assert "Альфа" not in SPECS["news"].prompt


def test_an_empty_profile_falls_back_to_the_placeholder():
    assert Settings(company_profile="  ").company_profile == COMPANY_PROFILE_PLACEHOLDER


def test_the_analyst_reads_the_graph_and_never_writes():
    spec = SPECS["analyst"]
    assert not (spec.write or spec.delete or spec.maintenance)


def test_only_a_record_without_an_assessment_needs_one(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "agents", "news,analyst")
    fresh = Item(uid="01A", analysis=None)
    failed = Item(uid="01B", analysis={"error": "503", "run_id": 1})
    done = Item(uid="01C", analysis={"importance": 0, "impact": "нет"})
    assert [analyst.needed(i) for i in (fresh, failed, done)] == [True, True, False]
    monkeypatch.setattr(settings, "agents", "news")
    assert not analyst.needed(fresh)


@pytest.mark.parametrize("analysis, threshold, closed", [
    ({"importance": 1}, 3, True),
    ({"importance": 3}, 3, False),
    ({"importance": 0}, 0, False),
    ({"error": "503"}, 5, False),
    (None, 5, False),
])
def test_the_threshold_closes_only_assessed_records_below_it(analysis, threshold, closed):
    assert bool(analyst.below(analysis, threshold)) is closed
