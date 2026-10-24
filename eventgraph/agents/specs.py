from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from eventgraph.config import settings

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


@dataclass(frozen=True)
class AgentSpec:
    name: str
    prompt_file: str
    max_steps: int
    temperature: float
    # Сколько прошлых реплик пользователя брать в ран; None — все.
    max_history: int | None = None
    # Убирать ответы инструментов из прошлых реплик.
    drop_tool_history: bool = False
    # Сколько раз подряд можно повторить тот же вызов с тем же ответом.
    max_repeated_calls: int = 5
    # Права в графе.
    write: bool = False
    delete: bool = False
    maintenance: bool = False
    graph_overview: bool = False
    # Интернет-поиск (eventgraph/agents/web.py), если задан SEARXNG_URL.
    web: bool = False
    # Графовые инструменты доступны по умолчанию, кроме однократных сводок.
    graph_read: bool = True

    @property
    def prompt(self) -> str:
        return self.prompt_for()

    def prompt_for(self, variables: dict[str, str] | None = None) -> str:
        """Промпт с подстановками рана; профиль по умолчанию берётся из настроек."""
        values = {"company_profile": settings.company_profile, **(variables or {})}
        prompt = load_prompt(self.prompt_file)
        for name, value in values.items():
            prompt = prompt.replace("{{" + name + "}}", str(value).strip())
        return prompt


SPECS: dict[str, AgentSpec] = {
    # Разбирает каждую новую запись и пишет события в граф.
    "news": AgentSpec("news", "news.md", max_steps=100, temperature=0.2, write=True, web=True),
    # Обслуживает граф: дубли, островки, обрезка.
    "gardener": AgentSpec("gardener", "gardener.md", max_steps=60, temperature=0.2,
                          write=True, delete=True, maintenance=True),
    # Перед разбором оценивает запись для компании; граф только читает.
    "analyst": AgentSpec("analyst", "analyst.md", max_steps=30, temperature=0.2, web=True),
    "summarizer": AgentSpec("summarizer", "news-summary.md", max_steps=1, temperature=0.1, graph_read=False),
    "news_chat": AgentSpec("news_chat", "news-chat.md", max_steps=5, temperature=0.2,
                           max_history=6, drop_tool_history=True),
    "graph_chat": AgentSpec("graph_chat", "graph-chat.md", max_steps=8, temperature=0.2,
                            max_history=8, drop_tool_history=True, graph_overview=True),
}


@lru_cache(maxsize=None)
def load_prompt(file_name: str) -> str:
    return (PROMPTS_DIR / file_name).read_text(encoding="utf-8").strip()
