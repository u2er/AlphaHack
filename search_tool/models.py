"""Input, extracted documents and evidence returned by the internet search tool."""
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchTask(StrictModel):
    query: str = Field(min_length=3, max_length=400)
    category: Literal["general", "news"] = "general"
    time_range: Literal["day", "month", "year"] | None = None
    domains: list[str] = Field(default_factory=list)


class SearchRequest(SearchTask):
    max_results: int = Field(default=6, ge=1, le=10)
    fragments_per_source: int = Field(default=3, ge=1, le=5)


class SearchHit(StrictModel):
    url: str
    title: str
    snippet: str = ""
    published_hint: str | None = None
    engines: list[str] = Field(default_factory=list)


class SearchBatch(StrictModel):
    hits: list[SearchHit]
    warnings: list[str] = Field(default_factory=list)


class ParsedDocument(StrictModel):
    url: str
    title: str
    text: str
    published_at: str | None = None
    author: str | None = None
    content_type: str
    fetched_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    raw_sha256: str


class Chunk(StrictModel):
    id: str
    ordinal: int
    start: int
    end: int
    text: str
