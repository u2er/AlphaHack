"""Интернет-поиск агентов: search_tool на подставных SearXNG, страницах и эмбеддере."""

from __future__ import annotations

import numpy as np
import pytest

from eventgraph.agents import web
from eventgraph.agents.specs import SPECS
from eventgraph.agents.tools import ToolContext, build_tools, system_text
from search_tool import InternetSearchTool
from search_tool.models import ParsedDocument, SearchBatch, SearchHit
from search_tool.parser import parse_content
from search_tool.ranker import EmbedderRanker

RATE = "Совет директоров Банка России сохранил ключевую ставку на уровне 16%. Решение совпало с ожиданиями."
FOOTBALL = "Футбольный клуб выиграл матч со счётом 3:1. Болельщики праздновали победу до утра."


class Search:
    def search(self, task, limit=8):
        return SearchBatch(hits=[SearchHit(url="https://a.ru/rate", title="Ставка"),
                                 SearchHit(url="https://b.ru/sport", title="Спорт")][:limit])


class Pages:
    def fetch(self, url):
        text = RATE if "rate" in url else FOOTBALL
        return ParsedDocument(url=url, title=url, text=text, content_type="text/html", raw_sha256=url)


def encode(texts, passage):
    # «Смысл» — есть ли в тексте ставка: вектор [ставка, прочее].
    return np.array([[1.0, 0.0] if ("ставк" in t.lower()) else [0.0, 1.0] for t in texts])


@pytest.fixture
def tool(monkeypatch):
    monkeypatch.setattr(web.settings, "searxng_url", "http://searxng:8080")
    instance = InternetSearchTool(search=Search(), parser=Pages(), ranker=EmbedderRanker(encode))
    monkeypatch.setattr(web, "session", lambda: instance)
    return instance


def handlers(spec):
    return {t.name: t.handler for t in build_tools(ToolContext(SPECS[spec], "news"))}


def test_only_agents_with_web_get_it_and_only_with_searxng(tool, monkeypatch):
    assert {"web_search", "web_page"} <= set(handlers("analyst")) and "web_search" in handlers("news")
    assert "web_search" not in handlers("gardener")
    assert "## Web search" in system_text(ToolContext(SPECS["analyst"], "news"))
    monkeypatch.setattr(web.settings, "searxng_url", "")
    assert "web_search" not in handlers("analyst")


async def test_search_returns_relevant_passages_and_pages_are_readable(tool):
    tools = handlers("analyst")
    result = await tools["web_search"]("ключевая ставка Банка России")
    by_url = {s["url"]: s for s in result["sources"]}
    assert result["status"] == "ok" and set(by_url) == {"https://a.ru/rate", "https://b.ru/sport"}
    assert "ставку" in by_url["https://a.ru/rate"]["fragments"][0]["text"]
    assert by_url["https://b.ru/sport"]["fragments"] == []  # нерелевантная страница — без фрагментов
    page = await tools["web_page"](by_url["https://b.ru/sport"]["source_id"])
    assert page["ok"] and page["content"] == FOOTBALL
    assert not (await tools["web_page"]("unknown"))["ok"]


def test_an_article_the_heuristics_lose_is_read_by_trafilatura():
    # Класс «related» у обёртки — эвристика шума удаляет всю статью.
    html = ("<html><head><title>Ставка</title></head><body><div class='related-wrapper'><article><p>"
            + "Банк России сохранил ключевую ставку. " * 20 + "</p></article></div></body></html>").encode()
    assert "ключевую ставку" in parse_content(html, "https://a.ru/x", "text/html").text
