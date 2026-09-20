from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class EvidenceStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    INFERRED = "INFERRED"
    HYPOTHESIS = "HYPOTHESIS"
    UNKNOWN = "UNKNOWN"


class RecordKind(StrEnum):
    FACT = "HISTORICAL_FACT"
    DECISION = "HISTORICAL_DECISION"
    INSIGHT = "HISTORICAL_INSIGHT"
    RECOMMENDATION = "CURRENT_AI_RECOMMENDATION"
    FUTURE_ACTION = "USER_APPROVED_FUTURE_ACTION"


class Evidence(Model):
    id: Identifier
    repository_key: Identifier | None = None
    source_type: str
    locator: str
    content: str
    occurred_at: str
    synthetic: Literal[True] = True
    provenance_note: str


class Question(Model):
    id: Identifier
    label: str


class Event(Model):
    id: Identifier
    evidence_ids: list[Identifier]


class Claim(Model):
    id: Identifier
    question_id: Identifier
    text: str | None
    evidence_status: EvidenceStatus
    record_kind: RecordKind
    evidence_ids: list[Identifier]
    unknown_reason: str | None = None

    @model_validator(mode="after")
    def check_status(self) -> Self:
        if self.evidence_status == EvidenceStatus.UNKNOWN:
            if self.text is not None or not self.unknown_reason:
                raise ValueError("UNKNOWN must have null text and an explicit reason")
        else:
            if not self.text or self.unknown_reason is not None:
                raise ValueError("A substantive claim needs text and no unknown_reason")
            if not self.evidence_ids:
                raise ValueError("A substantive claim must cite supplied evidence")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("Duplicate evidence reference")
        return self


class Link(Model):
    event_id: Identifier
    story_key: Identifier


class Draft(Model):
    title: str
    claims: list[Claim]
    links: list[Link]

    @model_validator(mode="after")
    def unique_keys(self) -> Self:
        for values in [
            [c.id for c in self.claims],
            [c.question_id for c in self.claims],
            [link.event_id for link in self.links],
        ]:
            if len(values) != len(set(values)):
                raise ValueError("Duplicate claim, question or event assignment")
        return self


class Golden(Model):
    draft: Draft
    important_question_ids: list[Identifier]
    forbidden_claims: list[str]
    annotation_notes: str


class Case(Model):
    id: Identifier
    project_key: Identifier
    title: str
    purpose: str
    repositories: list[Identifier]
    questions: list[Question]
    evidence: list[Evidence]
    events: list[Event]
    golden_candidate: Golden
    mock_initial: Draft
    mock_corrected: Draft

    @model_validator(mode="after")
    def references(self) -> Self:
        evidence_ids = {e.id for e in self.evidence}
        question_ids = {q.id for q in self.questions}
        event_ids = {e.id for e in self.events}
        if len(evidence_ids) != len(self.evidence) or len(question_ids) != len(self.questions):
            raise ValueError("Duplicate evidence or question ID")
        if len(event_ids) != len(self.events):
            raise ValueError("Duplicate event ID")
        if any(e.repository_key not in [None, *self.repositories] for e in self.evidence):
            raise ValueError("Unknown repository")
        if any(not set(e.evidence_ids) <= evidence_ids for e in self.events):
            raise ValueError("Unknown event evidence")
        for draft in [self.mock_initial, self.mock_corrected, self.golden_candidate.draft]:
            validate_draft(self, draft)
        validate_golden(self, self.golden_candidate)
        return self


def validate_draft(case: Case, draft: Draft) -> None:
    evidence_ids = {e.id for e in case.evidence}
    question_ids = {q.id for q in case.questions}
    event_ids = {e.id for e in case.events}
    for claim in draft.claims:
        if claim.question_id not in question_ids or not set(claim.evidence_ids) <= evidence_ids:
            raise ValueError("Claim references evidence or question outside this case/project")
    if any(link.event_id not in event_ids for link in draft.links):
        raise ValueError("Link references an event outside this case/project")


def validate_golden(case: Case, golden: Golden) -> None:
    validate_draft(case, golden.draft)
    answered = {c.question_id for c in golden.draft.claims}
    if answered != {q.id for q in case.questions}:
        raise ValueError("Golden must classify every applicable question, including UNKNOWN")
    knowable = {
        c.question_id for c in golden.draft.claims if c.evidence_status != EvidenceStatus.UNKNOWN
    }
    if not set(golden.important_question_ids) <= knowable:
        raise ValueError("Important recall denominator must contain knowable claims only")
    if len(golden.important_question_ids) != len(set(golden.important_question_ids)):
        raise ValueError("Duplicate important question")
    if {link.event_id for link in golden.draft.links} != {e.id for e in case.events}:
        raise ValueError("Golden must assign every event for linking evaluation")


class RunRequest(Model):
    mode: Literal["mock", "gemini"] = "mock"
    base_revision_id: Identifier | None = None
    correction: str | None = Field(default=None, max_length=4000)


class ReviewRequest(Model):
    revision_id: Identifier
    action: Literal["APPROVE", "HOLD", "CLARIFICATION", "UI_FRICTION"]
    note: str = Field(default="", max_length=4000)


class GoldenApproval(Model):
    candidate_id: Identifier
    candidate_hash: str
    acknowledged: Literal[True]


class Judgment(Model):
    claim_id: Identifier
    supported: bool
    content_correct: bool
    severe_fabrication: bool = False
    note: str = Field(default="", max_length=4000)


class EvaluationRequest(Model):
    revision_id: Identifier
    golden_approval_id: Identifier
    judgments: list[Judgment]
    acknowledged: Literal[True]


class TimingSample(Model):
    session_id: Identifier
    sequence: int = Field(ge=0)
    revision_id: Identifier | None = None
    duration_ms: float = Field(ge=0, le=60000)
    active: bool
    waiting: bool
    closed: bool = False
    observed_at: str
