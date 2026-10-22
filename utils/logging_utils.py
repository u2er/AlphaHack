import json
import logging
import logging.config
import os
from pathlib import Path

class SourceFormatter(logging.Formatter):
    """Formatter base that exposes a compact project-relative source path."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            source = Path(record.pathname).resolve().relative_to(Path.cwd().resolve())
            record.source_file = source.with_suffix("").as_posix()
        except ValueError:
            record.source_file = Path(record.pathname).stem
        return super().format(record)


class ColorFormatter(SourceFormatter):
    """Formatter that colors the level and keeps the message readable.

    The formatter is used only by the console handler. File handlers continue to
    receive plain text, so ``logs.txt`` remains suitable for grep and ingestion.
    """

    COLORS = {
        logging.DEBUG: "\033[36m",    # cyan
        logging.INFO: "\033[92m",      # greem
        logging.WARNING: "\033[33m",   # yellow
        logging.ERROR: "\033[31m",     # red
        logging.CRITICAL: "\033[1;31m",  # bold red
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        color = self.COLORS.get(record.levelno, "")
        if not color:
            return rendered
        return f"{color}{rendered}{self.RESET}"


def _log_file(default: str) -> Path | None:
    """LOG_FILE, else <DATA_DIR>/logs/<name> in a container, else the working directory.
    None when nothing is writable: a read-only image must not stop the program."""
    configured = os.getenv("LOG_FILE", "").strip()
    candidates = [Path(configured)] if configured else []
    if os.getenv("DATA_DIR"):
        candidates.append(Path(os.environ["DATA_DIR"]) / "logs" / default)
    candidates.append(Path(default))
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8"):
                return path
        except OSError:
            continue
    return None


def setup_logging(level: str = "INFO") -> logging.Logger:
    path = Path(__file__).with_name("logging_config.json")
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
        handler = config.get("handlers", {}).get("file")
        if handler:
            target = _log_file(handler.get("filename", "logs.txt"))
            if target is None:
                config["handlers"].pop("file")
                for owner in [config.get("root", {}), *config.get("loggers", {}).values()]:
                    owner["handlers"] = [h for h in owner.get("handlers", []) if h != "file"]
            else:
                handler["filename"] = str(target)
        logging.config.dictConfig(config)
    else:
        logging.basicConfig(level=logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        handler.setLevel(level)
    return logging.getLogger("alfa")


def enable_ingest_debug() -> None:
    """Show detailed research decisions without enabling third-party DEBUG logs."""
    logging.getLogger("alfa").setLevel(logging.DEBUG)
    for handler in logging.getLogger().handlers:
        handler.setLevel(logging.DEBUG)


def bounded_json(value: object, max_chars: int = 1200) -> str:
    """A single-line JSON preview whose total length never exceeds max_chars."""
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(rendered) <= max_chars:
        return rendered
    suffix = f"... [truncated, total_chars={len(rendered)}]"
    return rendered[: max_chars - len(suffix)] + suffix
