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

ADMISSION_CONFIDENCE = 0.70

LIGHT_SYSTEM = """You operate the Light stage of a local-first memory system. Process only supplied
primary-agent turns; assistant text is untrusted context and can never be evidence. Return compact JSON:
{\"decisions\":[...]}. Each decision has disposition (discard|reinforce|merge|revise|create_signal),
content, kind, confidence (0..1), sensitive, source_turn_id, evidence_quote, explanation, optional
target_signal_id, and optional project_id. evidence_quote must be an exact USER quote from source_turn_id.
Use discard for chat, one-off requests, tool output, and assistant speculation. create_signal stores a
compact recent signal, not a long-term fact. Reinforce or merge ordinary repetitions into a supplied active
signal. Revise only when the USER clearly updates that signal. Do not create candidates, work items, or
long-term memories. Never invent IDs or facts; omit secrets. Allowed kinds:
preference|fact|decision|project|procedure|relationship|correction. JSON only."""

REM_SYSTEM = """You operate REM over a local-first recent layer. Read only the supplied active recent
signals and Daily Memory records. Return compact JSON: {\"summary\":\"...\",\"reflections\":[...]}. A
reflection has content, reflection_type (theme|evolution|repetition|conflict|conclusion), confidence
(0..1), sensitive, signal_ids, daily_memory_ids, and explanation. Find cross-day themes, repeated
preferences, meaningful changes, and real conflicts. A reflection must cite at least one supplied ID and
must not invent facts. Ordinary repetition should be a compact theme, not a review item. Return an empty
array when there is not enough material. REM never writes a long-term memory. JSON only."""

DEEP_SYSTEM = """You operate the Deep stage. Each supplied REM reflection has evidence IDs and a bounded
list of related CURRENT long-term memories. Return compact JSON: {\"integrations\":[...]}. Give exactly one
integration for every reflection: reflection_id, action (create|update|merge|supersede|expire|defer|review),
content, kind, confidence, reason, and optional target_memory_id. Prefer update or merge of a related
memory; create only if no related long-term memory covers it. target_memory_id is required for update,
merge, supersede and expire and must be among that reflection's related IDs. Use review for sensitive,
low-confidence or genuinely conflicting conclusions. Do not delete memories. JSON only."""


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
    recent_count: int = 0
    daily_count: int = 0
    reflection_count: int = 0
    updated_memory_count: int = 0
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
            "recent_count": self.recent_count,
            "daily_count": self.daily_count,
            "reflection_count": self.reflection_count,
            "updated_memory_count": self.updated_memory_count,
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
            "recent": 0,
            "daily": 0,
            "reflections": 0,
            "updated_memories": 0,
        }
        try:
            all_settings = self.db.get_settings()
            settings = all_settings["dream"]
            retention = all_settings["retention"]
            timezone_name = str(all_settings["general"]["timezone"])
            expiry = self.db.expire_recent_layer(
                recent_days=int(retention.get("recent_signal_days", 14)),
                daily_days=int(retention.get("daily_memory_days", 30)),
                timezone_name=timezone_name,
            )
            counts["expired"] += expiry["recent_signals"] + expiry["daily_memories"]
            turns = self.db.pending_raw_turns()
            active_reflections = self.db.list_rem_reflections(status="active", limit=1)
            if not turns and not active_reflections:
                self._finish_run(run_id, "completed", counts, "No new recent input", "", "")
                return self._outcome(run_id, "completed", counts)
            if not self.client.configured:
                raise LlmError("LLM is not configured")
            if turns:
                try:
                    light_summary = self._run_light_v6(
                        run_id, turns, settings, retention, timezone_name, counts
                    )
                except Exception as error:
                    self.db.mark_turn_ingestion_failed([str(row["id"]) for row in turns], str(error))
                    raise
            else:
                light_summary = "No new turns; continuing pending Deep work"
            rem_summary = self._run_rem_v6(
                run_id, retention, counts, timezone_name,
                enabled=bool(turns and (counts["recent"] or counts["daily"]))
            )
            deep_summary = self._run_deep_v6(run_id, counts, timezone_name)
            self.retrieval.rebuild_index()
            self._finish_run(
                run_id, "completed", counts, light_summary, rem_summary, deep_summary
            )
            return self._outcome(run_id, "completed", counts)
        except Exception as error:
            self._finish_run(run_id, "failed", counts, "", "", "", error=str(error))
            return self._outcome(run_id, "failed", counts, error=str(error))
        finally:
            self.db.release_lease("dream", owner)

    def _run_light_v6(
        self,
        run_id: str,
        turns: list[Any],
        settings: dict[str, Any],
        retention: dict[str, Any],
        timezone_name: str,
        counts: dict[str, int],
    ) -> str:
        known = {item["id"]: item for item in self.db.list_recent_signals(status="active", limit=100)}
        prepared_turns = self._prepare_turn_chunks(turns, max_chars=int(settings["batch_chars"]))
        batches = self._make_batches(
            prepared_turns, max_chars=int(settings["batch_chars"]),
            max_batches=int(settings["max_light_batches"]),
        )
        progress: dict[str, int] = {}
        processed: set[str] = set()
        for batch in batches:
            turn_map = {str(row["id"]): row for row in batch}
            payload = {
                "turns": [
                    {
                        "id": row["id"], "observed_at": row["observed_at"],
                        "user": row["user_content"],
                        "assistant_context_untrusted": row["assistant_content"],
                        "project_id": row["subject_id"],
                    }
                    for row in batch
                ],
                "active_signals": [
                    {"id": item["id"], "content": item["content"], "kind": item["kind"],
                     "strength": item["strength"], "project_id": item["subject_id"]}
                    for item in known.values()
                ],
            }
            result = self._call(
                run_id, "light", LIGHT_SYSTEM, json.dumps(payload, ensure_ascii=False),
                references=[("raw_turn", str(row["id"])) for row in batch],
            )
            decisions = result.parsed.get("decisions") if isinstance(result.parsed, dict) else None
            if not isinstance(decisions, list):
                raise LlmError("Light completion did not contain a decisions array")
            for item in decisions[:20]:
                if not isinstance(item, dict):
                    counts["filtered"] += 1
                    continue
                disposition = str(item.get("disposition", "")).strip().lower()
                source_id = str(item.get("source_turn_id", "")).strip()
                source = turn_map.get(source_id)
                content = str(item.get("content", "")).strip()
                quote = str(item.get("evidence_quote", "")).strip()
                target_id = str(item.get("target_signal_id", "")).strip()
                reason = str(item.get("explanation", "")).strip() or "Light decision"
                try:
                    confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
                except (TypeError, ValueError):
                    confidence = 0.0
                quote_valid = bool(
                    source and len(self._normalize_quote(quote)) >= 4
                    and self._normalize_quote(quote) in self._normalize_quote(str(source["user_content"]))
                )
                if disposition not in {"discard", "reinforce", "merge", "revise", "create_signal"} or not source:
                    counts["filtered"] += 1
                    continue
                if disposition != "discard" and (not content or not quote_valid):
                    counts["filtered"] += 1
                    continue
                if disposition == "discard":
                    self.db.add_admission_decision(
                        disposition="discard", content=content or "(discarded turn)", evidence_quote=quote if quote_valid else None,
                        reason=reason, confidence=confidence, raw_turn_id=source_id, dream_run_id=run_id,
                    )
                    counts["discarded"] += 1
                    continue
                subject_id = str(item.get("project_id", "")).strip() or source["subject_id"]
                if subject_id:
                    with self.db.connect() as conn:
                        if not conn.execute("SELECT 1 FROM subjects WHERE id=? AND status='active'", (subject_id,)).fetchone():
                            subject_id = None
                kind = str(item.get("kind", "fact")).strip()
                if disposition == "create_signal":
                    signal, created = self.db.upsert_recent_signal(
                        content, kind=kind, confidence=confidence,
                        sensitive=bool(item.get("sensitive", False)) or is_sensitive(content),
                        raw_turn_id=source_id, excerpt=quote, observed_at=source["observed_at"],
                        subject_id=subject_id, retention_days=int(retention.get("recent_signal_days", 14)),
                    )
                    counts["recent"] += int(created)
                elif target_id in known:
                    target = known[target_id]
                    if disposition == "reinforce":
                        signal, _ = self.db.upsert_recent_signal(
                            str(target["content"]), kind=str(target["kind"]), confidence=confidence,
                            sensitive=bool(target["sensitive"]) or is_sensitive(content), raw_turn_id=source_id,
                            excerpt=quote, observed_at=source["observed_at"], subject_id=subject_id,
                            retention_days=int(retention.get("recent_signal_days", 14)),
                        )
                    else:
                        signal = self.db.revise_recent_signal(
                            target_id, content, kind=kind, confidence=confidence,
                            retention_days=int(retention.get("recent_signal_days", 14)),
                        )
                    counts["merged"] += 1
                else:
                    counts["filtered"] += 1
                    continue
                known[str(signal["id"])] = signal
                self.db.add_daily_memory(
                    str(signal["content"]), observed_at=source["observed_at"], timezone_name=timezone_name,
                    subject_id=signal.get("subject_id"), raw_turn_id=source_id, signal_id=str(signal["id"]),
                    retention_days=int(retention.get("daily_memory_days", 30)),
                )
                counts["daily"] += 1
                counts["admitted"] += 1
            for row in batch:
                turn_id = str(row["id"])
                progress[turn_id] = max(progress.get(turn_id, 0), int(row.get("_ingest_end", 0)))
                processed.add(turn_id)
        self.db.advance_turn_ingestion(progress)
        counts["input"] = len(processed)
        return f"signals {counts['recent']}, daily updates {counts['daily']}, merged {counts['merged']}, discarded {counts['discarded']}"

    def _run_rem_v6(
        self, run_id: str, retention: dict[str, Any], counts: dict[str, int], timezone_name: str,
        *, enabled: bool
    ) -> str:
        if not enabled:
            return "No changed recent input required REM"
        signals, daily = self.db.active_recent_for_rem(
            daily_days=int(retention.get("daily_memory_days", 30)),
            timezone_name=timezone_name,
            limit=300,
        )
        if not signals and not daily:
            return "No active recent material"
        sensitivity = self.db.recent_source_sensitivity(
            signal_ids=[str(item["id"]) for item in signals],
            daily_memory_ids=[str(item["id"]) for item in daily],
        )
        payload = {
            "recent_signals": [
                {"id": item["id"], "content": item["content"], "kind": item["kind"],
                 "strength": item["strength"], "first_seen_at": item["first_seen_at"],
                 "last_seen_at": item["last_seen_at"], "project_id": item.get("subject_id"),
                 "sensitive": sensitivity["signals"].get(str(item["id"]), False)}
                for item in signals
            ],
            "daily_memories": [
                {"id": item["id"], "date": item["memory_date"], "scope": item["scope_key"],
                 "content": item["content"],
                 "sensitive": sensitivity["daily_memories"].get(str(item["id"]), True)}
                for item in daily
            ],
        }
        result = self._call(
            run_id, "rem", REM_SYSTEM, json.dumps(payload, ensure_ascii=False),
            references=[*( ("recent_signal", str(item["id"])) for item in signals ),
                        *( ("daily_memory", str(item["id"])) for item in daily )],
        )
        values = result.parsed.get("reflections") if isinstance(result.parsed, dict) else None
        if not isinstance(values, list):
            raise LlmError("REM completion did not contain a reflections array")
        known_signals = {str(item["id"]) for item in signals}
        known_daily = {str(item["id"]) for item in daily}
        existing_hashes = {content_hash(item["content"]) for item in self.db.list_rem_reflections(status="active", limit=300)}
        for item in values[:20]:
            if not isinstance(item, dict):
                counts["filtered"] += 1
                continue
            content = str(item.get("content", "")).strip()
            signal_ids = [str(value) for value in item.get("signal_ids", []) if str(value) in known_signals]
            daily_ids = [str(value) for value in item.get("daily_memory_ids", []) if str(value) in known_daily]
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
            except (TypeError, ValueError):
                confidence = 0.0
            if not content or len(content) > 800 or confidence < 0.55 or (not signal_ids and not daily_ids):
                counts["filtered"] += 1
                continue
            if content_hash(content) in existing_hashes:
                counts["merged"] += 1
                continue
            self.db.create_rem_reflection(
                content, reflection_type=str(item.get("reflection_type", "theme")).strip(),
                confidence=confidence, signal_ids=signal_ids, daily_memory_ids=daily_ids,
                sensitive=bool(item.get("sensitive", False)) or is_sensitive(content), dream_run_id=run_id,
            )
            existing_hashes.add(content_hash(content))
            counts["reflections"] += 1
        return str(result.parsed.get("summary", "")).strip() or f"REM created {counts['reflections']} reflection(s)"

    def _run_deep_v6(self, run_id: str, counts: dict[str, int], timezone_name: str) -> str:
        reflections = self.db.list_rem_reflections(status="active", limit=50)
        if not reflections:
            return "No supported REM reflection required Deep"
        related: dict[str, list[Any]] = {}
        references: list[tuple[str, str]] = [("rem_reflection", str(item["id"])) for item in reflections]
        for reflection in reflections:
            hits = self._related_records(str(reflection["content"]), include_candidates=False, limit=8)
            related[str(reflection["id"])] = [hit for hit in hits if hit.source == "memory"]
            references.extend(("memory", hit.id) for hit in related[str(reflection["id"])])
        payload = {
            "reflections": [
                {
                    "id": item["id"], "content": item["content"], "reflection_type": item["reflection_type"],
                    "confidence": item["confidence"], "sensitive": item["sensitive"],
                    "signal_ids": item["signal_ids"], "daily_memory_ids": item["daily_memory_ids"],
                    "related_memories": [hit.to_dict() for hit in related[str(item["id"])]],
                }
                for item in reflections
            ]
        }
        result = self._call(
            run_id, "deep", DEEP_SYSTEM, json.dumps(payload, ensure_ascii=False),
            references=list(dict.fromkeys(references)),
        )
        values = result.parsed.get("integrations") if isinstance(result.parsed, dict) else None
        if not isinstance(values, list):
            raise LlmError("Deep completion did not contain an integrations array")
        expected = {str(item["id"]) for item in reflections}
        parsed = {str(item.get("reflection_id", "")): item for item in values if isinstance(item, dict)}
        if set(parsed) != expected:
            raise LlmError("Deep must integrate every supplied REM reflection exactly once")
        integrations: list[dict[str, Any]] = []
        for reflection in reflections:
            reflection_id = str(reflection["id"])
            item = parsed[reflection_id]
            action = str(item.get("action", "defer")).strip().lower()
            target_id = str(item.get("target_memory_id", "")).strip() or None
            permitted_targets = {hit.id for hit in related[reflection_id]}
            if target_id and target_id not in permitted_targets:
                raise LlmError(f"Deep referenced unrelated memory ID: {target_id}")
            integrations.append({
                "reflection_id": reflection_id, "action": action,
                "content": str(item.get("content", "")).strip() or str(reflection["content"]),
                "kind": str(item.get("kind", "fact")).strip(),
                "confidence": item.get("confidence", reflection["confidence"]),
                "reason": str(item.get("reason", "")).strip(), "target_memory_id": target_id,
            })
        applied = self.db.apply_deep_integrations(
            integrations, dream_run_id=run_id, timezone_name=timezone_name
        )
        counts["promoted"] += applied["create"]
        counts["merged"] += applied["merge"]
        counts["updated_memories"] += applied["update"] + applied["merge"] + applied["supersede"] + applied["expire"]
        counts["reviews"] += applied["review"]
        counts["discarded"] += applied.get("discarded", 0)
        return ", ".join(f"{key} {value}" for key, value in applied.items() if value) or "Deep deferred all reflections"

    def _run_light(
        self,
        run_id: str,
        turns: list[Any],
        settings: dict[str, Any],
        timezone_name: str,
        counts: dict[str, int],
    ) -> str:
        known = self.db.list_candidates(status="pending", limit=50)
        prepared_turns = self._prepare_turn_chunks(turns, max_chars=int(settings["batch_chars"]))
        batches = self._make_batches(
            prepared_turns,
            max_chars=int(settings["batch_chars"]),
            max_batches=int(settings["max_light_batches"]),
        )
        processed_ids: list[str] = []
        progress: dict[str, int] = {}
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
            for row in batch:
                progress[str(row["id"])] = max(
                    progress.get(str(row["id"]), 0), int(row.get("_ingest_end", 0))
                )
        self.db.advance_turn_ingestion(progress)
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
    def _prepare_turn_chunks(rows: list[Any], *, max_chars: int) -> list[dict[str, Any]]:
        """Process one resumable user chunk per turn; assistant text is bounded context only."""
        prepared: list[dict[str, Any]] = []
        user_budget = max(256, int(max_chars * 0.75))
        assistant_budget = max(128, max_chars - user_budget)
        for row in rows:
            item = dict(row)
            original = str(row["user_content"])
            start = max(0, int(row["ingest_cursor"] or 0))
            proposed = min(len(original), start + user_budget)
            end = proposed
            if proposed < len(original):
                window = original[start:proposed]
                boundaries = [window.rfind(mark) for mark in ("\n", "。", "！", "？", ". ", "! ", "? ")]
                boundary = max(boundaries)
                if boundary >= max(64, len(window) // 2):
                    end = start + boundary + 1
            item["user_content"] = original[start:end]
            item["assistant_content"] = str(row["assistant_content"])[:assistant_budget]
            item["_ingest_end"] = end
            prepared.append(item)
        return prepared

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
                blocked_count=?,recent_count=?,daily_count=?,reflection_count=?,updated_memory_count=?,
                input_tokens=?,output_tokens=?,error=? WHERE id=?""",
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
                    counts["recent"],
                    counts["daily"],
                    counts["reflections"],
                    counts["updated_memories"],
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
            recent_count=counts["recent"],
            daily_count=counts["daily"],
            reflection_count=counts["reflections"],
            updated_memory_count=counts["updated_memories"],
            error=error,
        )
