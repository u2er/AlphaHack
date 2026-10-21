from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_reasoning_effort: str = ""
    llm_timeout: float = 120
    reasoning_graph_json: Path = PROJECT_ROOT / "data" / "casket-news-graph-2026-10-21.json"
    reasoning_max_steps: int = 20
    reasoning_log_file: Path = PROJECT_ROOT / "reasoning" / "agent.log"


settings = Settings()
