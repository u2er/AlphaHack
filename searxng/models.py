from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


def now():
    return datetime.now(timezone.utc).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Profile(StrictModel):
    company: str = Field(min_length=1)
    industry: str | None = None
    markets: list[str] = Field(default_factory=list)
    exporter: bool | None = None
    # Verbatim excerpts of user input. Inferences belong to tasks, not facts.
    user_evidence: list[str] = Field(min_length=1)
    unknowns: list[str] = Field(default_factory=list)


class SearchTask(StrictModel):
    id: str = Field(min_length=1)
    query: str = Field(min_length=3, max_length=400)
    purpose: str
    category: Literal["general", "news"] = "news"
    time_range: Literal["day", "month", "year"] | None = "month"
    domains: list[str] = Field(default_factory=list)
    coverage_ids: list[str] = Field(min_length=1)


class CoverageRequirement(StrictModel):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    required: bool
    kind: Literal["business", "bank_analysis"]
    acceptance: str = Field(min_length=1)


class CoverageAssessment(StrictModel):
    requirement_id: str
    status: Literal["covered", "partial", "missing"]
    explanation: str = Field(min_length=1)
    evidence_ids: list[str]
    gap: str
    # Exact excerpt supporting attribution and analytical content, not just a domain.
    bank_analysis_quote: str


class Plan(StrictModel):
    profile: Profile
    coverage_requirements: list[CoverageRequirement] = Field(min_length=2, max_length=12)
    tasks: list[SearchTask] = Field(min_length=1, max_length=6)


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
    fetched_at: str = Field(default_factory=now)
    raw_sha256: str


class Review(StrictModel):
    decision: Literal["ready", "refine", "insufficient"]
    reason: str
    gaps: list[str]
    coverage: list[CoverageAssessment] = Field(min_length=1, max_length=12)
    tasks: list[SearchTask] = Field(default_factory=list, max_length=6)


class E5Action(StrictModel):
    action: Literal["search", "review"]
    reason: str = Field(min_length=1)
    queries: list[str] = Field(default_factory=list, max_length=2)


class BankAnalysis(StrictModel):
    is_bank_analysis: bool
    attribution_quote: str = Field(max_length=1500)
    analysis_quote: str = Field(max_length=2000)
    conclusion: str = Field(min_length=1, max_length=2000)
