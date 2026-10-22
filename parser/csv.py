"""Read the semicolon-delimited, UTF-8 appendix 1 tables."""
from __future__ import annotations

import csv
from pathlib import Path


def csv_rows(path: Path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        yield from csv.DictReader(stream, delimiter=';')
