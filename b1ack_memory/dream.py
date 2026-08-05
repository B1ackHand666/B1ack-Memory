from __future__ import annotations

import json
import math
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .db import MemoryDatabase, content_hash, utc_now
from .llm import LlmError, OpenAICompatibleClient
from .retrieval import search_tokens
from .security import is_sensitive

LIGHT_SYSTEM = """You extract durable personal-memory candidates from redacted conversation turns.
Only extract information that is truly suitable for long-term personal memory.
Return one JSON object with key `candidates`, an array. Each item must have:
content (concise standalone statement), kind (preference|fact|decision|project|procedure|relationship|correction),
confidence (0..1), sensitive (boolean), source_turn_id.
Keep only stable preferences or facts, long-term goals/projects, important decisions, relationships,
explicit corrections, and recurring procedures. Exclude greetings, transient progress, one-off tasks or
episodes, quoted/source material, system or tool output, unconfirmed assistant claims, and secrets.
Return at most 8 candidates. Keep each content under 240 characters. Do not repeat equivalent facts.
Never invent information. Return compact JSON only, without markdown or commentary."""

REM_SYSTEM = """You review personal-memory candidates for durable personal use. Return JSON with keys
`summary`, `themes`, `durable_candidate_ids`, `noise`, `duplicates`, and `conflicts`.
themes is an array of short strings. conflicts is an array of objects with candidate_id,
optional memory_id, and explanation. Report candidate-to-candidate conflicts without memory_id.
durable_candidate_ids contains candidate IDs that are genuinely durable. noise is an array of objects
with candidate_id and explanation for transient, one-off, system-generated, quoted, or otherwise noisy items.
duplicates is an array of objects with candidate_id, optional canonical_candidate_id, optional memory_id,
optional rejected_candidate_id, and explanation. Use canonical_candidate_id for equivalent pending candidates,
memory_id when active durable memory covers the same meaning, and rejected_candidate_id when the owner recently
rejected the same meaning. Return at most 10 themes, 20 conflicts, 20 noise items and 20 duplicates.
Keep summary and explanations concise.
Do not create durable memories and do not add facts. Return compact JSON only."""

DEEP_SYSTEM = """You compact qualified personal-memory candidates into durable statements.
Return JSON with key `memories`, an array of objects containing candidate_id and content.
Preserve meaning, remove conversational wording, and never combine unrelated facts. JSON only."""


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
            "error": self.error,
        }


class DreamEngine:
    def __init__(self, db: MemoryDatabase, client: OpenAICompatibleClient):
        self.db = db
        self.client = client

    def run(self, *, dry_run: bool = False) -> DreamOutcome:
        if dry_run:
            return self._run_dry()
        run_id = str(uuid.uuid4())
        owner = f"{run_id}:{uuid.uuid4()}"
        if not self.db.acquire_lease("dream", owner, 20 * 60):
            return DreamOutcome(run_id, "skipped_locked", 0, 0, 0)
        started = utc_now()
        self._create_run(run_id, started)
        input_count = candidate_count = promoted_count = 0
        merged_count = filtered_count = expired_count = 0
        error_message: str | None = None
        try:
            turns = self.db.pending_raw_turns()
            input_count = len(turns)
            if not turns and not self.db.list_candidates(limit=1):
                self._finish_run(run_id, "completed", 0, 0, 0, 0, 0, 0, "No new turns", "", "")
                return DreamOutcome(run_id, "completed", 0, 0, 0)
            if not self.client.configured:
                raise LlmError("LLM is not configured")

            settings = self.db.get_settings()["dream"]
            batches = self._make_batches(
                turns,
                max_chars=int(settings["batch_chars"]),
                max_batches=int(settings["max_light_batches"]),
            )
            processed_ids: list[str] = []
            extracted: dict[str, dict[str, Any]] = {}
            for batch in batches:
                payload = {
                    "turns": [
                        {
                            "id": row["id"],
                            "observed_at": row["observed_at"],
                            "user": row["user_content"],
                            "assistant": row["assistant_content"],
                        }
                        for row in batch
                    ]
                }
                result = self._call(
                    run_id,
                    "light",
                    LIGHT_SYSTEM,
                    json.dumps(payload, ensure_ascii=False),
                    references=[("raw_turn", row["id"]) for row in batch],
                )
                if not isinstance(result.parsed, dict) or not isinstance(
                    result.parsed.get("candidates"), list
                ):
                    raise LlmError("Light completion did not contain a candidates array")
                candidates = result.parsed["candidates"]
                turn_map = {row["id"]: row for row in batch}
                for item in candidates[:20]:
                    if not isinstance(item, dict):
                        filtered_count += 1
                        continue
                    content = str(item.get("content", "")).strip()
                    source_id = str(item.get("source_turn_id", ""))
                    try:
                        confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
                    except (TypeError, ValueError):
                        confidence = 0.0
                    kind = str(item.get("kind", "fact"))
                    if (
                        not content
                        or len(content) > 240
                        or source_id not in turn_map
                        or confidence < 0.75
                        or kind == "episode"
                    ):
                        filtered_count += 1
                        continue
                    digest = content_hash(content)
                    if digest in extracted:
                        merged_count += 1
                        extracted[digest]["confidence"] = max(
                            extracted[digest]["confidence"], confidence
                        )
                        extracted[digest]["sources"].append(turn_map[source_id])
                    else:
                        extracted[digest] = {
                            "content": content,
                            "kind": kind,
                            "confidence": confidence,
                            "sensitive": bool(item.get("sensitive", False))
                            or is_sensitive(content),
                            "sources": [turn_map[source_id]],
                        }
                processed_ids.extend(turn_map)

            max_new = int(settings.get("max_new_candidates", 8))
            for item in sorted(
                extracted.values(), key=lambda value: value["confidence"], reverse=True
            ):
                existing = self.db.candidate_by_content(item["content"])
                if self.db.active_memory_has_content(item["content"]):
                    filtered_count += 1
                    continue
                if existing and existing.status in {"rejected", "promoted"}:
                    filtered_count += 1
                    continue
                if not existing and candidate_count >= max_new:
                    filtered_count += 1
                    continue
                for source in item["sources"]:
                    self.db.upsert_candidate(
                        item["content"],
                        kind=item["kind"],
                        confidence=item["confidence"],
                        sensitive=item["sensitive"],
                        raw_turn_id=source["id"],
                        excerpt=item["content"],
                        observed_at=source["observed_at"],
                    )
                if existing:
                    merged_count += 1
                else:
                    candidate_count += 1
            self.db.mark_turns_ingested(processed_ids)
            input_count = len(processed_ids)

            pending = self.db.list_candidates(limit=100)
            rem_summary = ""
            reviewed_ids: set[str] = set()
            if pending:
                reviewed = pending[:30]
                reviewed_ids = {item.id for item in reviewed}
                existing_memories = self.db.list_memories(status="active", limit=100)
                rejected_candidates = self.db.list_candidates(status="rejected", limit=100)
                rem_payload = {
                    "candidates": [
                        {"id": item.id, "content": item.content, "kind": item.kind}
                        for item in reviewed
                    ],
                    "existing_memories": [
                        {"id": item.id, "content": item.content, "kind": item.kind}
                        for item in existing_memories
                    ],
                    "rejected_candidates": [
                        {"id": item.id, "content": item.content, "kind": item.kind}
                        for item in rejected_candidates
                    ],
                }
                rem = self._call(
                    run_id,
                    "rem",
                    REM_SYSTEM,
                    json.dumps(rem_payload, ensure_ascii=False),
                    references=[("candidate", item.id) for item in reviewed]
                    + [("candidate", item.id) for item in rejected_candidates]
                    + [("memory", item.id) for item in existing_memories],
                )
                if isinstance(rem.parsed, dict):
                    rem_summary = str(rem.parsed.get("summary", ""))
                    duplicates = rem.parsed.get("duplicates", [])
                    durable_ids = {
                        str(item) for item in rem.parsed.get("durable_candidate_ids", [])
                    } if isinstance(rem.parsed.get("durable_candidate_ids", []), list) else set()
                    existing_memory_ids = {item.id for item in existing_memories}
                    rejected_candidate_ids = {item.id for item in rejected_candidates}
                    if isinstance(duplicates, list):
                        for duplicate in duplicates:
                            if not isinstance(duplicate, dict):
                                continue
                            duplicate_id = str(duplicate.get("candidate_id", ""))
                            canonical_id = str(duplicate.get("canonical_candidate_id", ""))
                            memory_id = str(duplicate.get("memory_id", ""))
                            rejected_id = str(duplicate.get("rejected_candidate_id", ""))
                            reason = str(duplicate.get("explanation", "同义重复")).strip()
                            if duplicate_id not in reviewed_ids:
                                continue
                            if memory_id in existing_memory_ids:
                                if self.db.expire_candidate(
                                    duplicate_id, reason or "已有长期记忆覆盖相同含义"
                                ):
                                    expired_count += 1
                                    filtered_count += 1
                                    reviewed_ids.discard(duplicate_id)
                                continue
                            if rejected_id in rejected_candidate_ids:
                                if self.db.expire_candidate(
                                    duplicate_id, reason or "与近期人工拒绝的内容含义相同"
                                ):
                                    expired_count += 1
                                    filtered_count += 1
                                    reviewed_ids.discard(duplicate_id)
                                continue
                            if canonical_id in reviewed_ids and canonical_id != duplicate_id:
                                try:
                                    self.db.merge_candidates(canonical_id, duplicate_id)
                                except KeyError:
                                    continue
                                merged_count += 1
                                reviewed_ids.discard(duplicate_id)
                                if duplicate_id in durable_ids:
                                    durable_ids.add(canonical_id)
                                durable_ids.discard(duplicate_id)
                    conflicts = rem.parsed.get("conflicts", [])
                    if isinstance(conflicts, list):
                        self.db.update_candidate_conflicts(
                            [item for item in conflicts if isinstance(item, dict)],
                            reviewed_ids=list(reviewed_ids),
                        )
                    noise_items = rem.parsed.get("noise", [])
                    noise: dict[str, str] = {}
                    if isinstance(noise_items, list):
                        for item in noise_items:
                            if not isinstance(item, dict):
                                continue
                            candidate_id = str(item.get("candidate_id", ""))
                            if candidate_id in reviewed_ids:
                                noise[candidate_id] = str(
                                    item.get("explanation", "REM 判定为非长期信息")
                                )
                    expired_count += self.db.update_candidate_rem_review(
                        reviewed_ids=list(reviewed_ids),
                        durable_ids=list(durable_ids),
                        noise=noise,
                    )
                    pending = self.db.list_candidates(limit=100)

            eligible = []
            for candidate in pending:
                score, components = self.score(candidate)
                self.db.update_candidate_score(candidate.id, score, components)
                candidate.score = score
                candidate.score_components = components
                repeat_evidence = candidate.evidence_days >= 2
                demonstrated_utility = (
                    candidate.recall_count >= 2 and candidate.unique_query_count >= 2
                )
                if (
                    candidate.model_confidence >= 0.80
                    and candidate.rem_status == "approved"
                    and candidate.rem_reviewed_at is not None
                    and candidate.rem_reviewed_at >= candidate.last_activity_at
                    and (repeat_evidence or demonstrated_utility)
                    and not candidate.sensitive
                    and not candidate.conflict_memory_id
                    and not candidate.conflict_reason
                ):
                    eligible.append(candidate)
            eligible.sort(key=lambda item: item.score, reverse=True)
            local_now = datetime.now().astimezone()
            local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            already_promoted = self.db.count_auto_promotions_since(
                local_midnight.astimezone(UTC).isoformat()
            )
            remaining_promotions = max(
                0, int(settings["max_auto_promotions"]) - already_promoted
            )
            eligible = eligible[:remaining_promotions]
            deep_summary = f"{len(eligible)} candidate(s) qualified"
            if eligible:
                deep_payload = {
                    "candidates": [{"id": item.id, "content": item.content} for item in eligible]
                }
                deep = self._call(
                    run_id,
                    "deep",
                    DEEP_SYSTEM,
                    json.dumps(deep_payload, ensure_ascii=False),
                    references=[("candidate", item.id) for item in eligible],
                )
                rewrites = {
                    str(item.get("candidate_id")): str(item.get("content", "")).strip()
                    for item in deep.parsed.get("memories", [])
                    if isinstance(item, dict)
                } if isinstance(deep.parsed, dict) else {}
                for candidate in eligible:
                    rewritten = rewrites.get(candidate.id)
                    if rewritten:
                        self.db.promote_candidate(
                            candidate.id, edited_content=rewritten, origin="dream"
                        )
                        promoted_count += 1
            self._finish_run(
                run_id,
                "completed",
                input_count,
                candidate_count,
                promoted_count,
                merged_count,
                filtered_count,
                expired_count,
                f"Processed {input_count} turn(s)",
                rem_summary,
                deep_summary,
            )
            return DreamOutcome(
                run_id,
                "completed",
                input_count,
                candidate_count,
                promoted_count,
                merged_count,
                filtered_count,
                expired_count,
            )
        except Exception as error:
            error_message = str(error)
            self._finish_run(
                run_id,
                "failed",
                input_count,
                candidate_count,
                promoted_count,
                merged_count,
                filtered_count,
                expired_count,
                "",
                "",
                "",
                error=error_message,
            )
            return DreamOutcome(
                run_id,
                "failed",
                input_count,
                candidate_count,
                promoted_count,
                merged_count,
                filtered_count,
                expired_count,
                error_message,
            )
        finally:
            self.db.release_lease("dream", owner)

    @staticmethod
    def score(candidate: Any) -> tuple[float, dict[str, float]]:
        now = datetime.now(UTC)
        age_days = max(0.0, (now - datetime.fromisoformat(candidate.last_seen_at)).total_seconds() / 86400)
        relevance = min(1.0, candidate.recall_count / 4)
        frequency = min(1.0, (candidate.recall_count + candidate.evidence_days) / 6)
        diversity = min(1.0, candidate.unique_query_count / 3)
        recency = math.exp(-math.log(2) * age_days / 14)
        consolidation = min(1.0, candidate.evidence_days / 3)
        conceptual_units = max(len(candidate.content.split()), len(search_tokens(candidate.content)))
        conceptual = min(1.0, conceptual_units / 20)
        components = {
            "relevance": relevance,
            "frequency": frequency,
            "diversity": diversity,
            "recency": recency,
            "consolidation": consolidation,
            "conceptual": conceptual,
        }
        score = (
            relevance * 0.30
            + frequency * 0.24
            + diversity * 0.15
            + recency * 0.15
            + consolidation * 0.10
            + conceptual * 0.06
        )
        return round(score, 6), components

    def _run_dry(self) -> DreamOutcome:
        with tempfile.TemporaryDirectory(prefix="b1ack-memory-dry-") as directory:
            clone_path = Path(directory) / "memory.db"
            self.db.backup(clone_path)
            clone = MemoryDatabase(clone_path)
            with clone.transaction(immediate=True) as conn:
                conn.execute("DELETE FROM leases")
            outcome = DreamEngine(clone, self.client).run(dry_run=False)
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
        input_count: int,
        candidate_count: int,
        promoted_count: int,
        merged_count: int,
        filtered_count: int,
        expired_count: int,
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
                expired_count=?,promoted_count=?,
                input_tokens=?,output_tokens=?,error=? WHERE id=?""",
                (
                    status,
                    utc_now(),
                    light,
                    rem,
                    deep,
                    input_count,
                    candidate_count,
                    merged_count,
                    filtered_count,
                    expired_count,
                    promoted_count,
                    usage[0],
                    usage[1],
                    error,
                    run_id,
                ),
            )
