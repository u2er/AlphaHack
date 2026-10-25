"""LLM settings changed from the GUI: a JSON file on top of .env, shared by every process.

api, worker and profile-gui mount the same DATA_DIR volume, so the GUI writes
`DATA_DIR/llm-settings.json` and the others pick it up on the next model call.
A key missing from the file means "as in .env"; deleting the file resets everything."""
from __future__ import annotations
import json
import os
from pathlib import Path
import threading
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
# File key → .env variable. Values are kept as strings, like the environment.
FIELDS = {
    'base_url': 'LLM_BASE_URL',
    'model': 'LLM_MODEL',
    'api_key': 'LLM_API_KEY',
    'vision': 'LLM_VISION',
    'params': 'LLM_PARAMS',
    'max_tokens': 'LLM_MAX_TOKENS',
    'timeout': 'LLM_TIMEOUT',
}
_lock = threading.Lock()
_cache: tuple[int, dict] | None = None


def path() -> Path:
    load_dotenv(ROOT/'.env', override=False)
    explicit = os.getenv('LLM_SETTINGS_FILE', '').strip()
    if explicit:
        return Path(explicit)
    return Path(os.getenv('DATA_DIR', '').strip() or ROOT/'.data')/'llm-settings.json'


def env_defaults() -> dict[str, str]:
    """What .env gives; a variable that is not set is an empty string."""
    load_dotenv(ROOT/'.env', override=False)
    return {key: os.getenv(var, '').strip() for key, var in FIELDS.items()}


def load() -> dict[str, str]:
    """Non-empty overrides only. Re-read when the file changes; a broken file counts as empty."""
    global _cache
    file = path()
    try:
        mtime = file.stat().st_mtime_ns
    except OSError:
        return {}
    with _lock:
        if _cache and _cache[0] == mtime:
            return dict(_cache[1])
        try:
            data = json.loads(file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = {}
        # An empty value means "as in .env", same as a missing key.
        data = {k: str(v) for k, v in data.items() if k in FIELDS and str(v).strip()} if isinstance(data, dict) else {}
        _cache = (mtime, data)
        return dict(data)


def effective() -> dict[str, str]:
    return {**env_defaults(), **load()}


def save(values: dict[str, str]) -> None:
    """Replace the overrides with what differs from .env; the file holds the API key,
    so it is readable by its owner only."""
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)
    env = env_defaults()
    data = {k: str(v).strip() for k, v in values.items() if k in FIELDS and str(v).strip() and str(v).strip() != env[k]}
    tmp = file.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        json.dump(data, out, ensure_ascii=False, indent=2)
    os.replace(tmp, file)


def clear() -> None:
    try:
        path().unlink()
    except FileNotFoundError:
        pass
