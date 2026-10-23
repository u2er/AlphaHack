import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from . import InternetSearchTool


def main():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    parser = argparse.ArgumentParser(description="Search web pages and return relevant passages")
    parser.add_argument("query")
    parser.add_argument("--base-url")
    parser.add_argument("--e5-path", type=Path)
    parser.add_argument("--max-results", type=int, default=6)
    parser.add_argument("--fragments-per-source", type=int, default=3)
    parser.add_argument("--category", choices=["general", "news"], default="general")
    parser.add_argument("--time-range", choices=["day", "month", "year"])
    parser.add_argument("--domain", action="append", default=[])
    args = parser.parse_args()
    with InternetSearchTool(base_url=args.base_url, e5_path=args.e5_path) as tool:
        result = tool(args.query, max_results=args.max_results,
                      fragments_per_source=args.fragments_per_source,
                      category=args.category, time_range=args.time_range, domains=args.domain)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
