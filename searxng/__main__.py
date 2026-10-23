"""Explicit CLI only. Nothing starts on import."""
import argparse
import json
import os
from pathlib import Path
from utils.config import ROOT, settings
from utils.llm import make_client
from .search import SearXNGProvider
from .parser import DocumentParser
from .indexer import Indexer, E5Embedder
from .agent import ResearchAgent, Limits
from utils.logging_utils import setup_logging


def main():
    p = argparse.ArgumentParser(description="Company-context document search; no business analysis")
    p.add_argument("--input", required=True, help="User's company description and research request")
    p.add_argument("--db", type=Path, default=ROOT / "data/research.sqlite")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--e5-path", type=Path, help="Explicit local multilingual-e5-large folder; never downloads")
    p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    args = p.parse_args()
    log = setup_logging(args.log_level)
    config = settings()
    log.info("Запуск: модель LLM=%s, режим поиска=%s, раундов=%d, БД=%s",
             config.model, "E5" if args.e5_path else "FTS5", args.rounds, args.db)
    indexer = Indexer(args.db, E5Embedder(args.e5_path) if args.e5_path else None)
    llm = make_client(config)
    search = SearXNGProvider(os.getenv("SEARXNG_BASE_URL", "http://127.0.0.1:8082"))
    parser = DocumentParser()
    try:
        result = ResearchAgent(llm, search, parser, indexer, Limits(rounds=args.rounds)).run(args.input)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        llm.close()
        search.close()
        parser.close()


if __name__ == "__main__":
    main()
