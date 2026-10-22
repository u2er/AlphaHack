from typing import Literal, Any
from pydantic import Field
from utils.models import StrictModel, now

class Fact(StrictModel):
    value: Any
    source: str
    confirmed: bool = False
    updated_at: str = Field(default_factory=now)

class Topic(StrictModel):
    title: str
    rationale: str
    profile_fields: list[str]
    source_ids: list[str]

class Assessment(StrictModel):
    topics: list[Topic]
    limitations: list[str]

class OverviewClaim(StrictModel):
    kind: Literal['fact', 'signal', 'question']
    text: str
    source_keys: list[str]

class Overview(StrictModel):
    summary: str
    claims: list[OverviewClaim]

class EditProposal(StrictModel):
    scope: Literal['facts', 'answers', 'clarify']
    key: str
    value: str
    explanation: str

class Profile(StrictModel):
    schema_version: str = '1'
    id: str
    company_id: str | None = None
    facts: dict[str, Fact] = Field(default_factory=dict)
    source_facts: dict[str, Fact] = Field(default_factory=dict)
    signals: dict = Field(default_factory=dict)
    overview: Overview | None = None
    answers: dict[str, Fact] = Field(default_factory=dict)
    assessment: Assessment | None = None
    evidence: dict[str, dict] = Field(default_factory=dict)
    trace: list[dict] = Field(default_factory=list)
    profile_confirmed: bool = False
    assessment_confirmed: bool = False
    source_preferences: dict = Field(default_factory=lambda: {'sites': [], 'mode': 'include'})
    updated_at: str = Field(default_factory=now)

class AgentAction(StrictModel):
    tool: Literal['search_library', 'ask_user', 'finish']
    query: str
    question: str
    assessment: Assessment | None
