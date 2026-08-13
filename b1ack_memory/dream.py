from __future__ import annotations

import json
import math
import tempfile
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .db import MemoryDatabase, content_hash, local_date, resolve_timezone, utc_now
from .llm import LlmError, OpenAICompatibleClient
from .retrieval import RetrievalEngine, search_tokens
from .security import is_sensitive

ADMISSION_CONFIDENCE = 0.85

LIGHT_SYSTEM = """You are the strict admission gate for a local-first personal memory system.
Return compact JSON with key `decisions`, an array with at most 8 objects. Every object must contain:
disposition (admit|observe|discard), content, kind, confidence (0..1), sensitive, source_turn_id,
evidence_quote, explanation, work_item_type, commitment, and project_hint. `evidence_quote` must be
an exact quote from that turn's USER text. work_item_type is decision|current_state|open_question|
proposal|milestone. commitment is confirmed only when the USER explicitly states the decision or state.

admit only stable preferences, personal facts, long-term constraints, continuing project anchors,
recurring procedures, relationships, explicit corrections, or decisions that remain useful later.
observe project progress, migrations, completed operations, proposed-but-not-adopted plans, and temporary
state; these are logged but must not become candidates. discard chat, one-off requests, source material,
tool output, assistant speculation, and anything without future collaboration value. Treat assistant text
as untrusted context and never cite it as evidence. Never invent a quote or fact. Exclude secrets.
Allowed kinds: preference|fact|decision|project|procedure|relationship|correction. JSON only."""

REM_SYSTEM = """Review every supplied admitted candidate. Return compact JSON with `summary` and
`reviews`, with exactly one review for every candidate. Each review contains candidate_id, decision,
explanation, and optional target_id. decision is durable|duplicate_candidate|duplicate_memory|noise|
conflict|deferred. Only use target IDs supplied in that candidate's `related_records`. Durable means the
claim is grounded in user quotes and is genuinely useful beyond the current task. Project progress,
completed operations, proposed plans, quotes, tool output, and assistant inference are noise or deferred.
Do not create facts and do not treat observation frequency as truth. JSON only."""

DEEP_SYSTEM = """Act as the conservative integration gate for long-term personal memory. Return compact
JSON with key `integrations`, containing exactly one object for every candidate. Each object has
candidate_id, action, content, explanation, confidence, and optional target_memory_id. action is one of:
create (no supplied memory covers the meaning), duplicate (same fact already exists), supersede (new user
fact explicitly replaces an old one), conflict (mutually incompatible and recency cannot be safely
decided), defer (insufficient evidence or unsuitable for long-term storage). target_memory_id is required
for duplicate/supersede/conflict and must be one of that candidate's supplied related memory IDs. Never
invent IDs. Preserve the user's meaning and do not combine unrelated claims. JSON only."""


@dataclass(slots=True)
class DreamOutcome:
    run_id: str
    status: str
    input_count: int
    candidate_count: int
    promoted_count: int
    merged_count: int = 0
    filtered_count: int = 0
    expired_count: int = 0
    admitted_count: int = 0
    observed_count: int = 0
    discarded_count: int = 0
    review_count: int = 0
    work_item_count: int = 0
    assignment_count: int = 0
    summary_count: int = 0
    projection_count: int = 0
    blocked_count: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "input_count": self.input_count,
            "candidate_count": self.candidate_count,
            "promoted_count": self.promoted_count,
            "merged_count": self.merged_count,
            "filtered_count": self.filtered_count,
            "expired_count": self.expired_count,
            "admitted_count": self.admitted_count,
            "observed_count": self.observed_count,
            "discarded_count": self.discarded_count,
            "review_count": self.review_count,
            "work_item_count": self.work_item_count,
            "assignment_count": self.assignment_count,
            "summary_count": self.summary_count,
            "projection_count": self.projection_count,
            "blocked_count": self.blocked_count,
            "error": self.error,
        }


class DreamEngine:
    def __init__(
        self,
        db: MemoryDatabase,
        client: OpenAICompatibleClient,
        embed_query: Callable[[str], list[float] | None] | None = None,
        observe_handler: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    ):
        self.db = db
        self.client = client
        self.retrieval = RetrievalEngine(db)
        self.embed_query = embed_query
        self.observe_handler = observe_handler

    def _related_records(self, content: str, **kwargs: Any) -> list[Any]:
        vector: list[float] | None = None
        if self.embed_query:
            try:
                vector = self.embed_query(content)
            except Exception:
                vector = None
        return self.retrieval.related_records(content, query_vector=vector, **kwargs)

    def run(self, *, dry_run: bool = False) -> DreamOutcome:
        if dry_run:
            return self._run_dry()
        run_id = str(uuid.uuid4())
        owner = f"{run_id}:{uuid.uuid4()}"
        if not self.db.acquire_lease("dream", owner, 20 * 60):
            return DreamOutcome(run_id, "skipped_locked", 0, 0, 0)
        self._create_run(run_id, utc_now())
        counts = {
            "input": 0,
            "candidates": 0,
            "promoted": 0,
            "merged": 0,
            "filtered": 0,
            "expired": 0,
            "admitted": 0,
            "observed": 0,
            "discarded": 0,
            "reviews": 0,
            "work_items": 0,
            "assignments": 0,
            "summaries": 0,
            "projections": 0,
            "blocked": 0,
        }
        try:
            turns = self.db.pending_raw_turns()
            pending_any = self.db.list_candidates(limit=1)
            if not turns and not pending_any:
                self._finish_run(run_id, "completed", counts, "No new turns", "", "")
                return self._outcome(run_id, "completed", counts)
            if not self.client.configured:
                raise LlmError("LLM is not configured")

            all_settings = self.db.get_settings()
            settings = all_settings["dream"]
            timezone_name = str(all_settings["general"]["timezone"])
            light_summary = self._run_light(
                run_id, turns, settings, timezone_name, counts
            )
            self.retrieval.rebuild_index()
            rem_summary = self._run_rem(run_id, timezone_name, counts)
            deep_summary = self._run_deep(run_id, settings, timezone_name, counts)
            self._finish_run(
                run_id, "completed", counts, light_summary, rem_summary, deep_summary
            )
            return self._outcome(run_id, "completed", counts)
        except Exception as error:
            self._finish_run(run_id, "failed", counts, "", "", "", error=str(error))
            return self._outcome(run_id, "failed", counts, error=str(error))
        finally:
            self.db.release_lease("dream", owner)

    def _run_light(
        self,
        run_id: str,
        turns: list[Any],
        settings: dict[str, Any],
        timezone_name: str,
        counts: dict[str, int],
    ) -> str:
        known = self.db.list_candidates(status="pending", limit=50)
        batches = self._make_batches(
            turns,
            max_chars=int(settings["batch_chars"]),
            max_batches=int(settings["max_light_batches"]),
        )
        processed_ids: list[str] = []
        new_hashes: set[str] = set()
        max_new = int(settings.get("max_new_candidates", 8))
        for batch in batches:
            turn_map = {row["id"]: row for row in batch}
            payload = {
                "turns": [
                    {
                        "id": row["id"],
                        "observed_at": row["observed_at"],
                        "user": row["user_content"],
                        "assistant_context_untrusted": row["assistant_content"],
                    }
                    for row in batch
                ],
                "known_candidates": [
                    {"id": item.id, "content": item.content, "kind": item.kind}
                    for item in known
                ],
            }
            result = self._call(
                run_id,
                "light",
                LIGHT_SYSTEM,
                json.dumps(payload, ensure_ascii=False),
                references=[("raw_turn", row["id"]) for row in batch],
            )
            if not isinstance(result.parsed, dict) or not isinstance(
                result.parsed.get("decisions"), list
            ):
                raise LlmError("Light completion did not contain a decisions array")
            for raw_item in result.parsed["decisions"][:20]:
                if not isinstance(raw_item, dict):
                    counts["filtered"] += 1
                    continue
                disposition = str(raw_item.get("disposition", "")).strip().lower()
                content = str(raw_item.get("content", "")).strip()
                source_id = str(raw_item.get("source_turn_id", "")).strip()
                quote = str(raw_item.get("evidence_quote", "")).strip()
                reason = str(raw_item.get("explanation", "")).strip() or "No admission reason"
                kind = str(raw_item.get("kind", "fact")).strip()
                try:
                    confidence = max(0.0, min(1.0, float(raw_item.get("confidence", 0.0))))
                except (TypeError, ValueError):
                    confidence = 0.0
                source = turn_map.get(source_id)
                valid_common = (
                    disposition in {"admit", "observe", "discard"}
                    and bool(content)
                    and len(content) <= 240
                    and source is not None
                )
                normalized_quote = self._normalize_quote(quote)
                quote_valid = bool(
                    source
                    and len(normalized_quote) >= 4
                    and normalized_quote
                    in self._normalize_quote(str(source["user_content"]))
                )
                if not valid_common or (disposition in {"admit", "observe"} and not quote_valid):
                    counts["filtered"] += 1
                    self.db.add_admission_decision(
                        disposition="invalid",
                        content=content,
                        evidence_quote=quote,
                        reason="Invalid structure, source turn, or user evidence quote",
                        confidence=confidence,
                        raw_turn_id=source_id if source else None,
                        dream_run_id=run_id,
                    )
                    continue
                if disposition != "admit":
                    counts["observed" if disposition == "observe" else "discarded"] += 1
                    decision_id = self.db.add_admission_decision(
                        disposition=disposition,
                        content=content,
                        evidence_quote=quote if quote_valid else None,
                        reason=reason,
                        confidence=confidence,
                        raw_turn_id=source_id,
                        dream_run_id=run_id,
                    )
                    if disposition == "observe" and self.observe_handler:
                        try:
                            stored = self.observe_handler({
                                "content": content,
                                "confidence": confidence,
                                "raw_turn_id": source_id,
                                "evidence_quote": quote,
                                "admission_decision_id": decision_id,
                                "work_item_type": str(raw_item.get("work_item_type", "proposal")),
                                "commitment": str(raw_item.get("commitment", "proposed")),
                                "project_hint": str(raw_item.get("project_hint", "")).strip(),
                                "session_id": str(source["session_id"]),
                                "user_content": str(source["user_content"]),
                            })
                            if stored:
                                counts["work_items"] += 1
                                if stored.get("subject_id"):
                                    counts["assignments"] += 1
                        except Exception:
                            counts["blocked"] += 1
                    continue
                if (
                    confidence < ADMISSION_CONFIDENCE
                    or kind not in {
                        "preference",
                        "fact",
                        "decision",
                        "project",
                        "procedure",
                        "relationship",
                        "correction",
                    }
                ):
                    counts["filtered"] += 1
                    self.db.add_admission_decision(
                        disposition="invalid",
                        content=content,
                        evidence_quote=quote,
                        reason="Admission confidence or memory kind did not pass policy",
                        confidence=confidence,
                        raw_turn_id=source_id,
                        dream_run_id=run_id,
                    )
                    continue
                digest = content_hash(content)
                existing = self.db.candidate_by_content(content)
                if self.db.active_memory_has_content(content) or (
                    existing and existing.status in {"rejected", "promoted"}
                ):
                    counts["filtered"] += 1
                    self.db.add_admission_decision(
                        disposition="discard",
                        content=content,
                        evidence_quote=quote,
                        reason="Exact content already exists or was explicitly rejected",
                        confidence=confidence,
                        raw_turn_id=source_id,
                        dream_run_id=run_id,
                    )
                    continue
                if not existing and digest not in new_hashes and len(new_hashes) >= max_new:
                    counts["filtered"] += 1
                    self.db.add_admission_decision(
                        disposition="invalid",
                        content=content,
                        evidence_quote=quote,
                        reason="Per-run new candidate limit reached",
                        confidence=confidence,
                        raw_turn_id=source_id,
                        dream_run_id=run_id,
                    )
                    continue
                stored = self.db.upsert_candidate(
                    content,
                    kind=kind,
                    confidence=confidence,
                    sensitive=bool(raw_item.get("sensitive", False)) or is_sensitive(content),
                    raw_turn_id=source_id,
                    excerpt=quote,
                    observed_at=source["observed_at"],
                    timezone_name=timezone_name,
                    dream_run_id=run_id,
                    admission_state="admitted",
                    source_type="dream_user",
                    admission_reason=reason,
                )
                self.db.add_admission_decision(
                    disposition="admit",
                    content=content,
                    evidence_quote=quote,
                    reason=reason,
                    confidence=confidence,
                    raw_turn_id=source_id,
                    candidate_id=stored.id,
                    dream_run_id=run_id,
                )
                counts["admitted"] += 1
                if existing:
                    counts["merged"] += 1
                elif digest not in new_hashes:
                    new_hashes.add(digest)
                    counts["candidates"] += 1
            processed_ids.extend(turn_map)
        self.db.mark_turns_ingested(processed_ids)
        counts["input"] = len(set(processed_ids))
        return (
            f"admit {counts['admitted']}, observe {counts['observed']}, "
            f"discard {counts['discarded']}, invalid {counts['filtered']}"
        )

    def _run_rem(
        self,
        run_id: str,
        timezone_name: str,
        counts: dict[str, int],
    ) -> str:
        reviewed = self.db.candidates_due_for_rem(limit=30)
        if not reviewed:
            return "No candidate required REM review"
        related_by_candidate: dict[str, list[Any]] = {}
        evidence: dict[str, list[dict[str, str]]] = {}
        references: list[tuple[str, str]] = [("candidate", item.id) for item in reviewed]
        with self.db.connect() as conn:
            for candidate in reviewed:
                evidence[candidate.id] = [
                    {
                        "date": local_date(row["observed_at"], timezone_name),
                        "quote": row["excerpt"],
                    }
                    for row in conn.execute(
                        "SELECT excerpt,observed_at FROM evidence WHERE candidate_id=? "
                        "ORDER BY observed_at",
                        (candidate.id,),
                    )
                ]
                related = self._related_records(
                    candidate.content,
                    include_candidates=True,
                    exclude={(candidate.id, "candidate")},
                    limit=10,
                )
                related_by_candidate[candidate.id] = related
                references.extend((hit.source, hit.id) for hit in related)
        payload = {
            "candidates": [
                {
                    "id": candidate.id,
                    "content": candidate.content,
                    "kind": candidate.kind,
                    "user_evidence": evidence[candidate.id],
                    "related_records": [hit.to_dict() for hit in related_by_candidate[candidate.id]],
                }
                for candidate in reviewed
            ]
        }
        result = self._call(
            run_id,
            "rem",
            REM_SYSTEM,
            json.dumps(payload, ensure_ascii=False),
            references=list(dict.fromkeys(references)),
        )
        reviews = result.parsed.get("reviews") if isinstance(result.parsed, dict) else None
        if not isinstance(reviews, list):
            raise LlmError("REM completion did not contain a reviews array")
        expected = {candidate.id for candidate in reviewed}
        parsed: dict[str, dict[str, Any]] = {}
        for item in reviews:
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("candidate_id", ""))
            if candidate_id in expected and candidate_id not in parsed:
                parsed[candidate_id] = item
        if set(parsed) != expected:
            raise LlmError("REM completion did not review every supplied candidate exactly once")
        durable: list[str] = []
        noise: dict[str, str] = {}
        reasons: dict[str, str] = {}
        for candidate in reviewed:
            item = parsed[candidate.id]
            decision = str(item.get("decision", "deferred"))
            reason = str(item.get("explanation", "")).strip() or "REM deferred"
            reasons[candidate.id] = reason
            related = {(hit.id, hit.source): hit for hit in related_by_candidate[candidate.id]}
            target_id = str(item.get("target_id", "")).strip()
            if decision == "durable":
                durable.append(candidate.id)
            elif decision == "noise":
                noise[candidate.id] = reason
            elif decision in {"duplicate_candidate", "duplicate_memory", "conflict"}:
                target_source = "candidate" if decision == "duplicate_candidate" else "memory"
                if (target_id, target_source) not in related:
                    raise LlmError(f"REM referenced unknown target ID: {target_id}")
                target = related[(target_id, target_source)]
                basis = content_hash(candidate.content + "\n" + target.content)
                self.db.create_review_item(
                    issue_type=decision,
                    proposed_action="duplicate" if "duplicate" in decision else "conflict",
                    proposed_content=candidate.content,
                    reason=reason,
                    confidence=float(item.get("confidence", 0.0) or 0.0),
                    candidate_id=candidate.id,
                    related_candidate_id=target_id if target_source == "candidate" else None,
                    related_memory_id=target_id if target_source == "memory" else None,
                    source="rem",
                    basis_hash=basis,
                    dream_run_id=run_id,
                )
                counts["reviews"] += 1
        counts["expired"] += self.db.update_candidate_rem_review(
            reviewed_ids=list(expected),
            durable_ids=durable,
            noise=noise,
            reasons=reasons,
            dream_run_id=run_id,
        )
        return str(result.parsed.get("summary", "")) or f"Reviewed {len(reviewed)} candidate(s)"

    def _run_deep(
        self,
        run_id: str,
        settings: dict[str, Any],
        timezone_name: str,
        counts: dict[str, int],
    ) -> str:
        pending = self.db.list_candidates(
            status="pending", admission_state="admitted", limit=500
        )
        eligible = []
        for candidate in pending:
            score, components = self.score(candidate)
            self.db.update_candidate_score(candidate.id, score, components)
            candidate.score = score
            if (
                candidate.model_confidence >= ADMISSION_CONFIDENCE
                and candidate.evidence_days >= 2
                and candidate.rem_status == "approved"
                and candidate.rem_reviewed_at is not None
                and candidate.rem_reviewed_at >= candidate.last_activity_at
                and not candidate.sensitive
                and not candidate.conflict_memory_id
                and not candidate.conflict_reason
                and not self.db.has_open_review(candidate_id=candidate.id)
            ):
                eligible.append(candidate)
        eligible.sort(key=lambda item: item.score, reverse=True)
        local_now = datetime.now(resolve_timezone(timezone_name))
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        remaining = max(
            0,
            int(settings["max_auto_promotions"])
            - self.db.count_auto_promotions_since(local_midnight.astimezone(UTC).isoformat()),
        )
        eligible = eligible[:remaining]
        if not eligible:
            return "No candidate passed all promotion gates"

        related_by_candidate: dict[str, list[Any]] = {}
        references: list[tuple[str, str]] = [("candidate", item.id) for item in eligible]
        for candidate in eligible:
            related = self._related_records(
                candidate.content,
                include_candidates=False,
                exclude={(candidate.id, "candidate")},
                limit=12,
            )
            related_by_candidate[candidate.id] = related
            references.extend(("memory", hit.id) for hit in related)
        payload = {
            "candidates": [
                {
                    "id": candidate.id,
                    "content": candidate.content,
                    "kind": candidate.kind,
                    "related_memories": [hit.to_dict() for hit in related_by_candidate[candidate.id]],
                }
                for candidate in eligible
            ]
        }
        result = self._call(
            run_id,
            "deep",
            DEEP_SYSTEM,
            json.dumps(payload, ensure_ascii=False),
            references=list(dict.fromkeys(references)),
        )
        integrations = result.parsed.get("integrations") if isinstance(result.parsed, dict) else None
        if not isinstance(integrations, list):
            raise LlmError("Deep completion did not contain an integrations array")
        expected = {candidate.id for candidate in eligible}
        parsed: dict[str, dict[str, Any]] = {}
        for item in integrations:
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("candidate_id", ""))
            if candidate_id in expected and candidate_id not in parsed:
                parsed[candidate_id] = item
        if set(parsed) != expected:
            raise LlmError("Deep completion did not integrate every supplied candidate exactly once")

        creates: list[dict[str, Any]] = []
        reviews: list[dict[str, Any]] = []
        allowed = {"create", "duplicate", "supersede", "conflict", "defer"}
        for candidate in eligible:
            item = parsed[candidate.id]
            action = str(item.get("action", "defer"))
            if action not in allowed:
                raise LlmError(f"Deep returned unsupported action: {action}")
            content = str(item.get("content", "")).strip() or candidate.content
            target_id = str(item.get("target_memory_id", "")).strip()
            related = {hit.id: hit for hit in related_by_candidate[candidate.id]}
            if action in {"duplicate", "supersede", "conflict"} and target_id not in related:
                raise LlmError(f"Deep referenced unknown target ID: {target_id}")
            reason = str(item.get("explanation", "")).strip() or "Deep integration decision"
            confidence = float(item.get("confidence", 0.0) or 0.0)
            if action == "create" and not related:
                creates.append({"candidate_id": candidate.id, "content": content})
            else:
                # Even a model `create` is reviewed when the deterministic full-pool
                # shortlist found a related memory.
                effective = "related_create" if action == "create" else action
                basis_parts = [candidate.content]
                basis_parts.extend(hit.content for hit in related.values())
                reviews.append(
                    {
                        "candidate_id": candidate.id,
                        "action": action,
                        "issue_type": effective,
                        "content": content,
                        "target_memory_id": target_id or None,
                        "reason": reason,
                        "confidence": confidence,
                        "basis_hash": content_hash("\n".join(basis_parts)),
                    }
                )
        applied = self.db.apply_dream_consolidation(
            creates=creates, reviews=reviews, dream_run_id=run_id
        )
        counts["promoted"] += len(applied["promoted"])
        counts["reviews"] += len(applied["reviews"])
        return (
            f"create {len(applied['promoted'])}, review {len(applied['reviews'])}; "
            "batch validated before commit"
        )

    @staticmethod
    def score(candidate: Any) -> tuple[float, dict[str, float]]:
        now = datetime.now(UTC)
        age_days = max(
            0.0,
            (now - datetime.fromisoformat(candidate.last_seen_at)).total_seconds() / 86400,
        )
        confidence = max(0.0, min(1.0, float(candidate.model_confidence)))
        repeat_evidence = min(1.0, candidate.evidence_days / 3)
        recency = math.exp(-math.log(2) * age_days / 30)
        conceptual_units = max(len(candidate.content.split()), len(search_tokens(candidate.content)))
        conceptual = min(1.0, conceptual_units / 20)
        components = {
            "confidence": confidence,
            "repeat_evidence": repeat_evidence,
            "recency": recency,
            "conceptual": conceptual,
        }
        score = confidence * 0.45 + repeat_evidence * 0.35 + recency * 0.15 + conceptual * 0.05
        return round(score, 6), components

    @staticmethod
    def _normalize_quote(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())

    def _run_dry(self) -> DreamOutcome:
        with tempfile.TemporaryDirectory(prefix="b1ack-memory-dry-") as directory:
            clone_path = Path(directory) / "memory.db"
            self.db.backup(clone_path)
            clone = MemoryDatabase(clone_path)
            with clone.transaction(immediate=True) as conn:
                conn.execute("DELETE FROM leases")
            outcome = DreamEngine(clone, self.client, self.embed_query).run(dry_run=False)
            outcome.status = "dry_run" if outcome.status == "completed" else outcome.status
            return outcome

    def _call(
        self,
        run_id: str,
        phase: str,
        system: str,
        user: str,
        *,
        references: list[tuple[str, str]] | None = None,
    ):
        call_id = str(uuid.uuid4())
        created = utc_now()
        try:
            result = self.client.chat_json(system=system, user=user)
            with self.db.transaction(immediate=True) as conn:
                conn.execute(
                    "INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        call_id,
                        run_id,
                        phase,
                        json.dumps({"system": system, "user": user}, ensure_ascii=False),
                        json.dumps(result.raw, ensure_ascii=False),
                        self.client.model,
                        result.input_tokens,
                        result.output_tokens,
                        None,
                        created,
                    ),
                )
                self._insert_call_refs(conn, call_id, references or [])
            return result
        except Exception as error:
            with self.db.transaction(immediate=True) as conn:
                conn.execute(
                    "INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        call_id,
                        run_id,
                        phase,
                        json.dumps({"system": system, "user": user}, ensure_ascii=False),
                        None,
                        self.client.model,
                        0,
                        0,
                        str(error),
                        created,
                    ),
                )
                self._insert_call_refs(conn, call_id, references or [])
            raise

    @staticmethod
    def _insert_call_refs(
        conn: Any, call_id: str, references: list[tuple[str, str]]
    ) -> None:
        if references:
            conn.executemany(
                "INSERT OR IGNORE INTO model_call_records(call_id,record_type,record_id) "
                "VALUES(?,?,?)",
                [(call_id, record_type, record_id) for record_type, record_id in references],
            )

    @staticmethod
    def _make_batches(rows: list[Any], *, max_chars: int, max_batches: int) -> list[list[Any]]:
        batches: list[list[Any]] = []
        current: list[Any] = []
        size = 0
        for row in rows:
            row_size = len(row["user_content"]) + len(row["assistant_content"])
            if current and size + row_size > max_chars:
                batches.append(current)
                current = []
                size = 0
                if len(batches) >= max_batches:
                    break
            current.append(row)
            size += row_size
        if current and len(batches) < max_batches:
            batches.append(current)
        return batches

    def _create_run(self, run_id: str, started: str) -> None:
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO dream_runs(id,status,started_at,model) VALUES(?,?,?,?)",
                (run_id, "running", started, self.client.model),
            )

    def _finish_run(
        self,
        run_id: str,
        status: str,
        counts: dict[str, int],
        light: str,
        rem: str,
        deep: str,
        *,
        error: str | None = None,
    ) -> None:
        with self.db.transaction(immediate=True) as conn:
            usage = conn.execute(
                "SELECT coalesce(sum(input_tokens),0),coalesce(sum(output_tokens),0) "
                "FROM model_calls WHERE dream_run_id=?",
                (run_id,),
            ).fetchone()
            conn.execute(
                """UPDATE dream_runs SET status=?,finished_at=?,light_summary=?,rem_summary=?,
                deep_summary=?,input_count=?,candidate_count=?,merged_count=?,filtered_count=?,
                expired_count=?,promoted_count=?,admitted_count=?,observed_count=?,discarded_count=?,
                review_count=?,work_item_count=?,assignment_count=?,summary_count=?,projection_count=?,
                blocked_count=?,input_tokens=?,output_tokens=?,error=? WHERE id=?""",
                (
                    status,
                    utc_now(),
                    light,
                    rem,
                    deep,
                    counts["input"],
                    counts["candidates"],
                    counts["merged"],
                    counts["filtered"],
                    counts["expired"],
                    counts["promoted"],
                    counts["admitted"],
                    counts["observed"],
                    counts["discarded"],
                    counts["reviews"],
                    counts["work_items"],
                    counts["assignments"],
                    counts["summaries"],
                    counts["projections"],
                    counts["blocked"],
                    usage[0],
                    usage[1],
                    error,
                    run_id,
                ),
            )

    @staticmethod
    def _outcome(
        run_id: str,
        status: str,
        counts: dict[str, int],
        *,
        error: str | None = None,
    ) -> DreamOutcome:
        return DreamOutcome(
            run_id=run_id,
            status=status,
            input_count=counts["input"],
            candidate_count=counts["candidates"],
            promoted_count=counts["promoted"],
            merged_count=counts["merged"],
            filtered_count=counts["filtered"],
            expired_count=counts["expired"],
            admitted_count=counts["admitted"],
            observed_count=counts["observed"],
            discarded_count=counts["discarded"],
            review_count=counts["reviews"],
            work_item_count=counts["work_items"],
            assignment_count=counts["assignments"],
            summary_count=counts["summaries"],
            projection_count=counts["projections"],
            blocked_count=counts["blocked"],
            error=error,
        )
