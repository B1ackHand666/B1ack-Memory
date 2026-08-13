from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from collections.abc import Callable
from typing import Any

from .db import MemoryDatabase, content_hash, eligible_memory_predicate, utc_now
from .llm import LlmError, OpenAICompatibleClient
from .models import MemoryRecord, ReviewItem
from .retrieval import RetrievalEngine

INTEGRATION_SYSTEM = """Classify how a proposed personal memory relates to the supplied memory records.
Return one compact JSON object containing action, explanation, confidence, and optional target_memory_id
(the field may reference either a supplied candidate or long-term memory). action is
create|duplicate|supersede|conflict|defer. Only cite supplied IDs. create means
none covers the meaning; duplicate means the same fact is already stored; supersede means the proposal
explicitly replaces an older fact; conflict means they are incompatible but recency cannot be safely
decided; defer means evidence is insufficient. Be conservative. JSON only."""

AUDIT_SYSTEM = """Audit active personal memories without changing them. Return compact JSON with key
`issues`, an array. Each issue has memory_id, issue_type, proposed_action, explanation, confidence, and
optional target_memory_id. issue_type is duplicate|conflict|temporary|weak_evidence|possibly_outdated.
Only report actionable quality problems. Only use supplied memory IDs and only cite a target from that
memory's related_memories. Never invent facts. Return an empty issues array when no problem is present.
JSON only."""


class MemoryGovernance:
    def __init__(
        self,
        db: MemoryDatabase,
        retrieval: RetrievalEngine,
        client: OpenAICompatibleClient,
        embed_query: Callable[[str], list[float] | None] | None = None,
    ):
        self.db = db
        self.retrieval = retrieval
        self.client = client
        self.embed_query = embed_query

    def _related(
        self,
        content: str,
        *,
        include_candidates: bool,
        exclude: set[tuple[str, str]] | None = None,
        limit: int = 12,
    ) -> list[Any]:
        vector: list[float] | None = None
        if self.embed_query:
            try:
                vector = self.embed_query(content)
            except Exception:
                vector = None
        return self.retrieval.related_records(
            content,
            include_candidates=include_candidates,
            exclude=exclude,
            limit=limit,
            query_vector=vector,
        )

    def remember(
        self,
        content: str,
        *,
        kind: str,
        origin: str,
        sensitive: bool,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        exact = self.db.active_memory_by_content(content)
        if exact:
            if project_id:
                self.db.link_subject_record(
                    project_id, "memory", exact.id, method="explicit_remember"
                )
            return {"status": "remembered", "memory": exact.to_dict(), "idempotent": True}
        related = self._related(content, include_candidates=True, limit=12)
        if not sensitive and not related:
            memory = self.db.add_memory(
                content,
                kind=kind,
                origin=origin,
                confidence=1.0,
                sensitive=False,
                subject_id=project_id,
            )
            return {"status": "remembered", "memory": memory.to_dict()}

        candidate = self.db.upsert_candidate(
            content,
            kind=kind,
            confidence=1.0,
            sensitive=sensitive,
            raw_turn_id=None,
            excerpt=content,
            admission_state="review_required",
            source_type="explicit_remember",
            admission_reason="用户明确要求记住；因敏感或存在相关长期记忆转入审核",
            subject_id=project_id,
        )
        self.db.add_admission_decision(
            disposition="admit",
            content=content,
            evidence_quote=content,
            reason="Explicit remember request",
            confidence=1.0,
            candidate_id=candidate.id,
        )
        decision = self._classify(content, related) if related else {
            "action": "defer",
            "explanation": "Sensitive content requires explicit review",
            "confidence": 1.0,
            "target_memory_id": None,
        }
        target_hit = next(
            (hit for hit in related if hit.id == decision.get("target_memory_id")), None
        )
        if decision.get("action") == "duplicate" and target_hit and target_hit.source == "candidate":
            canonical = self.db.merge_candidates(target_hit.id, candidate.id)
            review = self._create_integration_review(
                decision,
                content=canonical.content,
                candidate_id=canonical.id,
                related=related,
                source=origin,
                metadata={"project_id": project_id, "kind": kind, "absorbed_candidate": candidate.id},
            )
            return {
                "status": "review_required",
                "candidate": canonical.to_dict(),
                "review": review.to_dict(),
                "idempotent": True,
            }
        review = self._create_integration_review(
            decision,
            content=content,
            candidate_id=candidate.id,
            related=related,
            source=origin,
            issue_override="sensitive" if sensitive and not related else None,
            metadata={"project_id": project_id, "kind": kind},
        )
        return {
            "status": "review_required",
            "candidate": candidate.to_dict(),
            "review": review.to_dict(),
        }

    def promote_candidate(
        self, candidate_id: str, *, content: str | None = None
    ) -> dict[str, Any]:
        candidate = self.db.get_candidate(candidate_id)
        if not candidate or candidate.status != "pending":
            raise KeyError(candidate_id)
        proposed = (content or candidate.content).strip()
        exact = self.db.active_memory_by_content(proposed)
        if exact:
            memory = self.db.promote_candidate(
                candidate_id,
                edited_content=exact.content,
                origin="review",
                promotion_lane="manual_exact",
            )
            return {"status": "promoted", "memory": memory.to_dict(), "idempotent": True}
        related = self._related(
            proposed,
            include_candidates=True,
            exclude={(candidate_id, "candidate")},
            limit=12,
        )
        if not candidate.sensitive and not related:
            memory = self.db.promote_candidate(
                candidate_id,
                edited_content=proposed,
                origin="review",
                promotion_lane="manual",
            )
            return {"status": "promoted", "memory": memory.to_dict()}
        decision = self._classify(proposed, related) if related else {
            "action": "defer",
            "explanation": "Sensitive candidate requires explicit review",
            "confidence": 1.0,
            "target_memory_id": None,
        }
        review = self._create_integration_review(
            decision,
            content=proposed,
            candidate_id=candidate_id,
            related=related,
            source="manual_promotion",
            issue_override="sensitive" if candidate.sensitive and not related else None,
        )
        return {"status": "review_required", "review": review.to_dict()}

    def update_memory(
        self,
        record_id: str,
        *,
        content: str,
        kind: str,
        valid_from: str | None = None,
        valid_to: str | None = None,
        temporal_status: str | None = None,
        temporal_reason: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        current = self.db.get_memory(record_id)
        if not current:
            raise KeyError(record_id)
        unchanged = (
            current.content == content.strip() and current.kind == kind
            and (valid_from is None or current.valid_from == valid_from)
            and (valid_to is None or current.valid_to == valid_to)
            and (temporal_status is None or current.temporal_status == temporal_status)
            and (temporal_reason is None or current.temporal_reason == temporal_reason)
        )
        if unchanged:
            if project_id:
                self.db.link_subject_record(project_id, "memory", record_id, method="memory_edit")
            return {"status": "remembered", "memory": current.to_dict(), "idempotent": True}
        related = self._related(
            content,
            include_candidates=True,
            exclude={(record_id, "memory")},
            limit=12,
        )
        if not related:
            memory = self.db.update_memory(
                record_id, content=content, kind=kind, valid_from=valid_from,
                valid_to=valid_to, temporal_status=temporal_status,
                temporal_reason=temporal_reason, subject_id=project_id,
            )
            return {"status": "remembered", "memory": memory.to_dict()}
        decision = self._classify(content, related)
        review = self._create_integration_review(
            decision,
            content=content,
            primary_memory_id=record_id,
            related=related,
            source="memory_edit",
            metadata={"kind": kind, "valid_from": valid_from, "valid_to": valid_to,
                      "temporal_status": temporal_status, "temporal_reason": temporal_reason,
                      "project_id": project_id},
        )
        return {"status": "review_required", "review": review.to_dict()}

    def restore_memory(self, record_id: str) -> dict[str, Any]:
        memory = self.db.get_memory(record_id)
        if not memory:
            raise KeyError(record_id)
        if memory.status == "active":
            return {"status": "remembered", "memory": memory.to_dict(), "idempotent": True}
        related = self._related(
            memory.content,
            include_candidates=True,
            exclude={(record_id, "memory")},
            limit=12,
        )
        if not related:
            self.db.set_memory_status(record_id, "active")
            restored = self.db.get_memory(record_id)
            return {"status": "remembered", "memory": restored.to_dict()}
        decision = self._classify(memory.content, related)
        review = self._create_integration_review(
            decision,
            content=memory.content,
            primary_memory_id=record_id,
            related=related,
            source="memory_restore",
        )
        return {"status": "review_required", "review": review.to_dict()}

    def _classify(self, content: str, related: list[Any]) -> dict[str, Any]:
        if not related:
            return {
                "action": "create",
                "explanation": "No related active memory found",
                "confidence": 1.0,
                "target_memory_id": None,
            }
        if not self.client.configured:
            return {
                "action": "defer",
                "explanation": "Related records found, but the integration model is unavailable",
                "confidence": 0.0,
                "target_memory_id": None,
            }
        payload = {
            "proposed_memory": content,
            "related_records": [hit.to_dict() for hit in related],
        }
        try:
            parsed = self._call_json(
                "integration",
                INTEGRATION_SYSTEM,
                payload,
                references=[("memory", hit.id) for hit in related],
            )
        except Exception as error:
            return {
                "action": "defer",
                "explanation": f"Integration model failed: {error}",
                "confidence": 0.0,
                "target_memory_id": None,
            }
        action = str(parsed.get("action", "defer"))
        target_value = parsed.get("target_memory_id")
        target_id = str(target_value).strip() if target_value else None
        allowed = {"create", "duplicate", "supersede", "conflict", "defer"}
        related_ids = {hit.id for hit in related}
        if action not in allowed or (
            action in {"duplicate", "supersede", "conflict"} and target_id not in related_ids
        ):
            return {
                "action": "defer",
                "explanation": "Integration model returned an invalid action or unknown target ID",
                "confidence": 0.0,
                "target_memory_id": None,
            }
        return {
            "action": action,
            "explanation": str(parsed.get("explanation", "")).strip()
            or "Integration requires review",
            "confidence": float(parsed.get("confidence", 0.0) or 0.0),
            "target_memory_id": target_id,
        }

    def _create_integration_review(
        self,
        decision: dict[str, Any],
        *,
        content: str,
        related: list[Any],
        source: str,
        candidate_id: str | None = None,
        primary_memory_id: str | None = None,
        issue_override: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ReviewItem:
        action = str(decision.get("action", "defer"))
        target_value = decision.get("target_memory_id")
        target_id = str(target_value).strip() if target_value else None
        if not target_id and related:
            target_id = related[0].id
        target_hit = next((hit for hit in related if hit.id == target_id), None)
        basis = content_hash(
            "\n".join([content, *[str(hit.content) for hit in related]])
        )
        reason = str(decision.get("explanation", "Integration requires review"))
        if metadata:
            reason = f"{reason}\nmetadata={json.dumps(metadata, ensure_ascii=False)}"
        return self.db.create_review_item(
            issue_type=issue_override or action,
            proposed_action=action,
            proposed_content=content,
            reason=reason,
            confidence=float(decision.get("confidence", 0.0) or 0.0),
            candidate_id=candidate_id,
            primary_memory_id=primary_memory_id,
            related_candidate_id=(
                target_id if target_hit and target_hit.source == "candidate" else None
            ),
            related_memory_id=(
                target_id if target_hit and target_hit.source == "memory" else None
            ),
            source=source,
            basis_hash=basis,
            proposal=metadata,
        )

    def run_audit(
        self,
        *,
        scope: str = "full",
        memory_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        run = self.db.create_audit_run(scope)
        checked = issues = 0
        try:
            if scope in {"full", "legacy"}:
                issues += self._queue_legacy_candidates(run.id)
            valid_sql, valid_params = eligible_memory_predicate("m")
            with self.db.connect() as conn:
                memories = [self.db._memory_from_row(row) for row in conn.execute(
                    f"SELECT m.* FROM memories m WHERE {valid_sql} ORDER BY m.updated_at DESC LIMIT 100000",
                    valid_params,
                ).fetchall()]
            if memory_ids is not None:
                selected = set(memory_ids)
                memories = [item for item in memories if item.id in selected]
            if memories:
                if not self.client.configured:
                    raise LlmError("LLM is not configured for memory audit")
                for start in range(0, len(memories), 25):
                    batch = memories[start : start + 25]
                    issues += self._audit_batch(run.id, batch)
                    checked += len(batch)
            finished = self.db.finish_audit_run(
                run.id, checked_count=checked, issue_count=issues
            )
            return finished.to_dict()
        except Exception as error:
            finished = self.db.finish_audit_run(
                run.id,
                checked_count=checked,
                issue_count=issues,
                error=str(error),
            )
            return finished.to_dict()

    def _queue_legacy_candidates(self, audit_run_id: str) -> int:
        count = 0
        for candidate in self.db.list_candidates(
            status="pending", admission_state="legacy_review", limit=100_000
        ):
            review = self.db.create_review_item(
                issue_type="legacy_admission",
                proposed_action="defer",
                proposed_content=candidate.content,
                reason="Legacy candidate predates strict user-quote admission and must be reviewed",
                confidence=candidate.model_confidence,
                candidate_id=candidate.id,
                source="migration_v6",
                basis_hash=content_hash(candidate.content),
                audit_run_id=audit_run_id,
            )
            if review.status == "open":
                count += 1
        return count

    def _audit_batch(self, audit_run_id: str, memories: list[MemoryRecord]) -> int:
        related_by_id: dict[str, list[Any]] = {}
        references: list[tuple[str, str]] = []
        for memory in memories:
            related = self._related(
                memory.content,
                include_candidates=False,
                exclude={(memory.id, "memory")},
                limit=10,
            )
            related_by_id[memory.id] = related
            references.append(("memory", memory.id))
            references.extend(("memory", hit.id) for hit in related)
        payload = {
            "memories": [
                {
                    "id": memory.id,
                    "content": memory.content,
                    "kind": memory.kind,
                    "origin": memory.origin,
                    "related_memories": [hit.to_dict() for hit in related_by_id[memory.id]],
                }
                for memory in memories
            ]
        }
        parsed = self._call_json(
            "audit",
            AUDIT_SYSTEM,
            payload,
            references=list(dict.fromkeys(references)),
        )
        raw_issues = parsed.get("issues")
        if not isinstance(raw_issues, list):
            raise LlmError("Audit completion did not contain an issues array")
        known = {memory.id: memory for memory in memories}
        created = 0
        for item in raw_issues:
            if not isinstance(item, dict):
                continue
            memory_id = str(item.get("memory_id", ""))
            issue_type = str(item.get("issue_type", ""))
            target_value = item.get("target_memory_id")
            target_id = str(target_value).strip() if target_value else None
            allowed = {"duplicate", "conflict", "temporary", "weak_evidence", "possibly_outdated"}
            if memory_id not in known or issue_type not in allowed:
                raise LlmError("Audit completion referenced an unknown memory or issue type")
            related = {hit.id: hit for hit in related_by_id[memory_id]}
            if target_id and target_id not in related:
                raise LlmError(f"Audit completion referenced unknown target ID: {target_id}")
            basis_parts = [known[memory_id].content]
            if target_id:
                basis_parts.append(related[target_id].content)
            review = self.db.create_review_item(
                issue_type=issue_type,
                proposed_action=str(item.get("proposed_action", "defer")),
                proposed_content=known[memory_id].content,
                reason=str(item.get("explanation", "Audit issue")),
                confidence=float(item.get("confidence", 0.0) or 0.0),
                primary_memory_id=memory_id,
                related_memory_id=target_id,
                source="full_audit",
                basis_hash=content_hash("\n".join(basis_parts)),
                audit_run_id=audit_run_id,
            )
            if review.status == "open":
                created += 1
        return created

    def audit_due(self) -> bool:
        latest = self.db.latest_audit_run("full")
        if not latest or latest.status != "completed" or not latest.finished_at:
            return True
        return datetime.fromisoformat(latest.finished_at) <= datetime.now(UTC) - timedelta(days=7)

    def resolve_review(
        self,
        review_id: str,
        *,
        action: str,
        content: str | None = None,
        canonical_id: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        review = self.db.get_review_item(review_id)
        if not review or review.status != "open":
            raise KeyError(review_id)
        candidate = self.db.get_candidate(review.candidate_id) if review.candidate_id else None
        memory = self.db.get_memory(review.primary_memory_id) if review.primary_memory_id else None
        target_id = canonical_id or review.related_memory_id
        proposal = review.proposal or {}
        result: dict[str, Any] = {"status": "resolved", "action": action}
        if action == "keep_both":
            if candidate:
                written = self.db.promote_candidate(
                    candidate.id,
                    edited_content=(content or review.proposed_content or candidate.content),
                    origin="review",
                    promotion_lane="manual_keep_both",
                    review_id=review_id,
                    review_resolution="keep_both",
                )
                result["memory"] = written.to_dict()
                return result
            self.db.close_review_item(review_id, resolution="keep_both", dismissed=True)
            return result
        if action in {"reject", "expire"}:
            if not candidate:
                raise ValueError("Review has no candidate")
            if action == "reject":
                self.db.reject_candidate(candidate.id)
            else:
                self.db.expire_candidate(candidate.id, "Review resolution: expire")
            self.db.close_review_item(review_id, resolution=action)
            return result
        if action == "merge":
            if review.related_candidate_id:
                if not candidate:
                    raise ValueError("Candidate-to-candidate merge requires a candidate source")
                merged_candidate = self.db.merge_candidates(
                    review.related_candidate_id,
                    candidate.id,
                    reason="Review resolution: semantic duplicate",
                )
                result["candidate"] = merged_candidate.to_dict()
                self.db.close_review_item(review_id, resolution=action)
                return result
            if not target_id:
                raise ValueError("canonical_id is required")
            target = self.db.get_memory(target_id)
            if not target or target.status != "active":
                raise KeyError(target_id)
            if candidate:
                merged = self.db.promote_candidate(
                    candidate.id,
                    edited_content=target.content,
                    origin="review",
                    promotion_lane="manual_merge",
                    review_id=review_id,
                    review_resolution=action,
                )
                result["memory"] = merged.to_dict()
                return result
            elif memory:
                merged = self.db.merge_memories(target.id, memory.id)
            else:
                raise ValueError("Review has no merge source")
            result["memory"] = merged.to_dict()
        elif action in {"create", "edit_execute", "supersede"}:
            proposed = (content or review.proposed_content or "").strip()
            if not proposed:
                raise ValueError("content is required")
            if candidate:
                written = self.db.promote_candidate(
                    candidate.id,
                    edited_content=proposed,
                    origin="review",
                    promotion_lane="manual_review",
                    review_id=review_id if action != "supersede" else None,
                    review_resolution=action if action != "supersede" else None,
                )
                result["memory"] = written.to_dict()
                if action != "supersede":
                    return result
            elif memory and action == "edit_execute":
                written = self.db.update_memory(
                    memory.id,
                    content=proposed,
                    kind=kind or str(proposal.get("kind") or memory.kind),
                    valid_from=proposal.get("valid_from"),
                    valid_to=proposal.get("valid_to"),
                    temporal_status=proposal.get("temporal_status"),
                    temporal_reason=proposal.get("temporal_reason"),
                    subject_id=proposal.get("project_id"),
                )
            elif memory:
                written = self.db.add_memory(
                    proposed, kind=kind or memory.kind, origin="review",
                    subject_id=proposal.get("project_id"),
                )
            else:
                written = self.db.add_memory(
                    proposed, kind=kind or str(proposal.get("kind") or "fact"), origin="review",
                    subject_id=proposal.get("project_id"),
                )
            if action == "supersede":
                if not target_id:
                    raise ValueError("canonical_id or related memory is required")
                written = self.db.supersede_memory(written.id, target_id)
            result["memory"] = written.to_dict()
        elif action == "trash":
            if not memory:
                raise ValueError("Review has no memory")
            self.db.set_memory_status(memory.id, "trashed")
        elif action == "keep":
            self.db.close_review_item(review_id, resolution="keep", dismissed=True)
            return result
        else:
            raise ValueError(f"Unsupported review action: {action}")
        self.db.close_review_item(review_id, resolution=action)
        return result

    def _call_json(
        self,
        phase: str,
        system: str,
        payload: dict[str, Any],
        *,
        references: list[tuple[str, str]],
    ) -> dict[str, Any]:
        call_id = str(uuid.uuid4())
        created = utc_now()
        user = json.dumps(payload, ensure_ascii=False)
        try:
            result = self.client.chat_json(system=system, user=user)
            if not isinstance(result.parsed, dict):
                raise LlmError(f"{phase} completion was not a JSON object")
            with self.db.transaction(immediate=True) as conn:
                conn.execute(
                    "INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        call_id,
                        None,
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
                conn.executemany(
                    "INSERT OR IGNORE INTO model_call_records(call_id,record_type,record_id) "
                    "VALUES(?,?,?)",
                    [(call_id, record_type, record_id) for record_type, record_id in references],
                )
            return result.parsed
        except Exception as error:
            with self.db.transaction(immediate=True) as conn:
                conn.execute(
                    "INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        call_id,
                        None,
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
            raise
