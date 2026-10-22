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
    timeout_seconds: float = 120
    max_tokens: int = 5500
    extra_body: dict = {}


def settings() -> Settings:
    load_dotenv(ROOT / ".env", override=False)
    return Settings(
        provider=os.getenv("LLM_PROVIDER", "openai_compatible"),
        base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
        model=os.getenv("LLM_MODEL", "deepseek-flash"),
        api_key=SecretStr(os.getenv("LLM_API_KEY", "")),
        structured_output=os.getenv("LLM_STRUCTURED_OUTPUT", "auto"),
        timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "120")),
        max_tokens=int(os.getenv("LLM_MAX_TOKENS", "5500")),
        extra_body=json.loads(os.getenv("LLM_EXTRA_BODY", "{}")),
    )


