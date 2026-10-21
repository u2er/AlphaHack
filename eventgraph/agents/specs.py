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

    @property
    def prompt(self) -> str:
        # Профиль компании — из настроек, чтобы его правка не трогала промпт.
        return load_prompt(self.prompt_file).replace("{{company_profile}}", settings.company_profile.strip())


SPECS: dict[str, AgentSpec] = {
    # Разбирает каждую новую запись и пишет события в граф.
    "news": AgentSpec("news", "news.md", max_steps=100, temperature=0.2, write=True),
    # Обслуживает граф: дубли, островки, обрезка.
    "gardener": AgentSpec("gardener", "gardener.md", max_steps=60, temperature=0.2,
                          write=True, delete=True, maintenance=True),
    # Перед разбором оценивает запись для компании; граф только читает.
    "analyst": AgentSpec("analyst", "analyst.md", max_steps=30, temperature=0.2),
}


@lru_cache(maxsize=None)
def load_prompt(file_name: str) -> str:
    return (PROMPTS_DIR / file_name).read_text(encoding="utf-8").strip()
