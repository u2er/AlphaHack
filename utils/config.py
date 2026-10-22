from pathlib import Path
import json
import os
from typing import Literal
from dotenv import load_dotenv
from pydantic import SecretStr
from .models import StrictModel

ROOT = Path(__file__).resolve().parents[1]


class Settings(StrictModel):
    provider: str = "openai_compatible"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key: SecretStr
    structured_output: Literal["auto", "responses", "json_schema"] = "auto"
    timeout_seconds: float = 300
    # Reasoning models spend most of the budget thinking: deepseek-flash used up to ~4.5k
    # reasoning tokens on the profile overview, so 5500 cut answers off.
    max_tokens: int = 16000
    extra_body: dict = {}


def _env(*names: str, default: str = "") -> str:
    """First non-empty variable: the shared EventGraph name, then the legacy profile one."""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def settings() -> Settings:
    # Same variables as eventgraph.config, so one .env configures every agent.
    load_dotenv(ROOT / ".env", override=False)
    extra = json.loads(_env("LLM_PARAMS", "LLM_EXTRA_BODY", default="{}"))
    if not isinstance(extra, dict):
        raise ValueError("LLM_PARAMS must be a JSON object")
    return Settings(
        provider=_env("LLM_PROVIDER", default="openai_compatible"),
        base_url=_env("LLM_BASE_URL", default="https://api.deepseek.com"),
        model=_env("LLM_MODEL", default="deepseek-flash"),
        api_key=SecretStr(_env("LLM_API_KEY")),
        structured_output=_env("LLM_STRUCTURED_OUTPUT", default="auto"),
        timeout_seconds=float(_env("LLM_TIMEOUT", "LLM_TIMEOUT_SECONDS", default="300")),
        max_tokens=int(_env("LLM_MAX_TOKENS", default="16000")),
        extra_body=extra,
    )


