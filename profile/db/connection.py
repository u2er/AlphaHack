"""PostgreSQL connection shared by indexing and profile reads."""
from __future__ import annotations

import os
import re
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


def database_url(dsn: str | None = None) -> str:
    """PROFILE_DATABASE_URL, else the EventGraph database (DATABASE_URL) via psycopg.

    profile_data is a schema inside the EventGraph database: one Postgres for the project."""
    if dsn:
        return dsn
    load_dotenv(ROOT / '.env', override=False)
    value = os.getenv('PROFILE_DATABASE_URL', '').strip() or os.getenv('DATABASE_URL', '').strip()
    if not value:
        from eventgraph.config import settings
        value = settings.database_url
    # SQLAlchemy driver suffixes (postgresql+asyncpg://) are not libpq URLs.
    return re.sub(r'^postgres(?:ql)?\+\w+://', 'postgresql://', value)


def connect(dsn: str | None = None, *, vector: bool = False):
    import psycopg
    from psycopg.rows import dict_row

    connection = psycopg.connect(database_url(dsn), row_factory=dict_row)
    if vector:
        from pgvector.psycopg import register_vector
        try:
            register_vector(connection)
        except Exception:
            connection.close()
            raise
    return connection


def executemany(connection, sql, rows):
    with connection.cursor() as cursor:
        cursor.executemany(sql, rows)


def copy_rows(connection, statement, rows):
    """Stream large CSV-derived tables without holding all rows in memory."""
    with connection.cursor() as cursor:
        with cursor.copy(statement) as stream:
            for row in rows:
                stream.write_row(row)
