from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

MemoryKind = Literal[
    "preference",
    "fact",
    "decision",
    "project",
    "procedure",
    "relationship",
    "correction",
    "episode",
]
MemoryStatus = Literal["active", "superseded", "trashed"]
CandidateStatus = Literal["pending", "promoted", "rejected", "expired"]
AdmissionState = Literal["admitted", "legacy_review", "review_required"]
ReviewStatus = Literal["open", "resolved", "dismissed", "obsolete"]
SubjectType = Literal["project", "person", "organization", "tool", "topic"]
SubjectStatus = Literal["active", "paused", "archived"]
WorkItemType = Literal["decision", "current_state", "open_question", "proposal", "milestone"]
WorkItemStatus = Literal["suggested", "active", "resolved", "expired", "archived"]

MEMORY_KINDS: tuple[str, ...] = (
    "preference",
    "fact",
    "decision",
    "project",
    "procedure",
    "relationship",
    "correction",
    "episode",
)


@dataclass(slots=True)
class MemoryRecord:
    id: str
    content: str
    kind: str
    status: str
    origin: str
    confidence: float
    importance: float
    created_at: str
    updated_at: str
    valid_until: str | None = None
    supersedes_id: str | None = None
    content_hash: str = ""
    sensitive: bool = False
    valid_from: str | None = None
    valid_to: str | None = None
    temporal_status: str = "current"
    temporal_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CandidateRecord:
    id: str
    content: str
    kind: str
    status: str
    model_confidence: float
    sensitive: bool
    score: float
    recall_count: int
    unique_query_count: int
    evidence_days: int
    first_seen_at: str
    last_seen_at: str
    last_activity_at: str
    last_recalled_at: str | None = None
    expired_at: str | None = None
    rejected_at: str | None = None
    promoted_at: str | None = None
    promotion_origin: str | None = None
    promoted_memory_id: str | None = None
    rem_status: str = "unreviewed"
    rem_reason: str | None = None
    rem_reviewed_at: str | None = None
    score_components: dict[str, float] = field(default_factory=dict)
    conflict_memory_id: str | None = None
    conflict_reason: str | None = None
    admission_state: str = "admitted"
    source_type: str = "dream_user"
    admission_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReviewItem:
    id: str
    issue_type: str
    status: str
    proposed_action: str
    proposed_content: str | None
    reason: str
    confidence: float
    source: str
    basis_hash: str
    fingerprint: str
    created_at: str
    updated_at: str
    candidate_id: str | None = None
    related_candidate_id: str | None = None
    primary_memory_id: str | None = None
    related_memory_id: str | None = None
    dream_run_id: str | None = None
    resolved_at: str | None = None
    resolution: str | None = None
    subject_id: str | None = None
    queue: str = "decision"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AuditRun:
    id: str
    scope: str
    status: str
    checked_count: int
    issue_count: int
    started_at: str
    finished_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SearchHit:
    id: str
    content: str
    kind: str
    source: str
    final_score: float
    keyword_rank: int | None = None
    vector_rank: int | None = None
    unverified: bool = False
    project_id: str | None = None
    temporal_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SubjectRecord:
    id: str
    subject_type: str
    name: str
    slug: str
    status: str
    description: str
    created_at: str
    updated_at: str
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class WorkItemRecord:
    id: str
    item_type: str
    content: str
    status: str
    confirmed: bool
    confidence: float
    subject_id: str | None
    raw_turn_id: str | None
    evidence_quote: str | None
    source: str
    expires_at: str | None
    created_at: str
    updated_at: str
    resolved_at: str | None = None
    promoted_memory_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
