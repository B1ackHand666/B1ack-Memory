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
    score_components: dict[str, float] = field(default_factory=dict)
    conflict_memory_id: str | None = None
    conflict_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SearchHit:
    id: str
    content: str
    kind: str
    source: Literal["memory", "candidate"]
    final_score: float
    keyword_rank: int | None = None
    vector_rank: int | None = None
    unverified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
