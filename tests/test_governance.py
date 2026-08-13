from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from b1ack_memory.db import MemoryDatabase, content_hash, utc_now
from b1ack_memory.dream import DreamEngine
from b1ack_memory.llm import LlmResult
from b1ack_memory.service import MemoryService


class AdmissionClient:
    configured = True
    model = "governance-test"

    def __init__(self, decisions: list[dict] | None = None):
        self.decisions = decisions

    def chat_json(self, *, system: str, user: str) -> LlmResult:
        payload = json.loads(user)
        if "strict admission gate" in system:
            decisions = self.decisions
            if decisions is None:
                turn = payload["turns"][0]
                decisions = [
                    {
                        "disposition": "admit",
                        "content": "用户长期偏好简洁回答",
                        "kind": "preference",
                        "confidence": 0.95,
                        "sensitive": False,
                        "source_turn_id": turn["id"],
                        "evidence_quote": "长期偏好简洁回答",
                        "explanation": "稳定偏好",
                    }
                ]
            parsed = {"decisions": decisions}
        elif "Review every supplied" in system:
            parsed = {
                "summary": "reviewed",
                "reviews": [
                    {
                        "candidate_id": item["id"],
                        "decision": "durable",
                        "explanation": "durable user-grounded preference",
                    }
                    for item in payload["candidates"]
                ],
            }
        elif "conservative integration gate" in system:
            parsed = {
                "integrations": [
                    {
                        "candidate_id": item["id"],
                        "action": "create",
                        "content": item["content"],
                        "explanation": "no related memory",
                        "confidence": 0.95,
                    }
                    for item in payload["candidates"]
                ]
            }
        elif "Audit active personal memories" in system:
            parsed = {"issues": []}
        else:
            parsed = {"action": "defer", "explanation": "test", "confidence": 0}
        return LlmResult(parsed=parsed, raw=parsed, input_tokens=5, output_tokens=3)


class AuditClient(AdmissionClient):
    def chat_json(self, *, system: str, user: str) -> LlmResult:
        if "Audit active personal memories" not in system:
            return super().chat_json(system=system, user=user)
        payload = json.loads(user)
        issue = None
        for memory in payload["memories"]:
            if memory["related_memories"]:
                issue = {
                    "memory_id": memory["id"],
                    "issue_type": "duplicate",
                    "proposed_action": "merge",
                    "target_memory_id": memory["related_memories"][0]["id"],
                    "explanation": "same durable preference",
                    "confidence": 0.92,
                }
                break
        parsed = {"issues": [issue] if issue else []}
        return LlmResult(parsed=parsed, raw=parsed, input_tokens=5, output_tokens=3)


class PartialInvalidDeepClient(AdmissionClient):
    def chat_json(self, *, system: str, user: str) -> LlmResult:
        if "conservative integration gate" not in system:
            return super().chat_json(system=system, user=user)
        candidates = json.loads(user)["candidates"]
        parsed = {
            "integrations": [
                {
                    "candidate_id": candidates[0]["id"],
                    "action": "create",
                    "content": candidates[0]["content"],
                    "explanation": "valid first action",
                    "confidence": 0.9,
                },
                {
                    "candidate_id": candidates[1]["id"],
                    "action": "conflict",
                    "content": candidates[1]["content"],
                    "target_memory_id": "unknown-memory",
                    "explanation": "invalid target",
                    "confidence": 0.9,
                },
            ]
        }
        return LlmResult(parsed=parsed, raw=parsed, input_tokens=5, output_tokens=3)


class GovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = MemoryService(self.root)

    def tearDown(self) -> None:
        self.service.shutdown(timeout=0.1)
        self.temp.cleanup()

    def test_light_admit_observe_discard_and_user_quote_evidence(self) -> None:
        turn_id = self.service.capture_turn(
            "quality",
            "我长期偏好简洁回答；今天只是讨论迁移方案",
            "助手猜测用户可能喜欢表格",
        )
        decisions = [
            {
                "disposition": "admit",
                "content": "用户长期偏好简洁回答",
                "kind": "preference",
                "confidence": 0.96,
                "sensitive": False,
                "source_turn_id": turn_id,
                "evidence_quote": "我长期偏好简洁回答",
                "explanation": "稳定偏好",
            },
            {
                "disposition": "observe",
                "content": "今天讨论迁移方案",
                "kind": "project",
                "confidence": 0.9,
                "sensitive": False,
                "source_turn_id": turn_id,
                "evidence_quote": "今天只是讨论迁移方案",
                "explanation": "尚未落地的临时方案",
            },
            {
                "disposition": "discard",
                "content": "助手猜测用户喜欢表格",
                "kind": "preference",
                "confidence": 0.9,
                "sensitive": False,
                "source_turn_id": turn_id,
                "evidence_quote": "",
                "explanation": "助手推测不是用户事实",
            },
        ]
        outcome = DreamEngine(self.service.db, AdmissionClient(decisions)).run()
        self.assertEqual(outcome.admitted_count, 1)
        self.assertEqual(outcome.observed_count, 1)
        self.assertEqual(outcome.discarded_count, 1)
        candidate = self.service.list_candidates()[0]
        self.assertEqual(candidate["evidence"][0]["excerpt"], "我长期偏好简洁回答")
        dispositions = {
            item["disposition"] for item in self.service.db.list_admission_decisions()
        }
        self.assertTrue({"admit", "observe", "discard"}.issubset(dispositions))

    def test_forged_assistant_quote_is_rejected(self) -> None:
        turn_id = self.service.capture_turn("forged", "今天只讨论方案", "用户长期喜欢表格")
        client = AdmissionClient(
            [
                {
                    "disposition": "admit",
                    "content": "用户长期喜欢表格",
                    "kind": "preference",
                    "confidence": 0.99,
                    "sensitive": False,
                    "source_turn_id": turn_id,
                    "evidence_quote": "用户长期喜欢表格",
                    "explanation": "伪造来源",
                }
            ]
        )
        outcome = DreamEngine(self.service.db, client).run()
        self.assertEqual(outcome.filtered_count, 1)
        self.assertEqual(self.service.list_candidates(), [])
        self.assertEqual(
            self.service.db.list_admission_decisions()[0]["disposition"], "invalid"
        )

    def test_prefetch_excludes_candidates_and_explicit_search_has_no_signal(self) -> None:
        candidate = self.service.db.upsert_candidate(
            "用户长期偏好黑咖啡",
            kind="preference",
            confidence=0.95,
            sensitive=False,
            raw_turn_id=None,
            excerpt="用户长期偏好黑咖啡",
        )
        before = self.service.db.get_candidate(candidate.id)
        self.service.rebuild_derived()
        self.assertEqual(self.service.format_prefetch("黑咖啡偏好"), "")
        hits = self.service.search("黑咖啡偏好", include_candidates=True)
        self.assertEqual(hits[0].source, "candidate")
        self.assertTrue(hits[0].unverified)
        after = self.service.db.get_candidate(candidate.id)
        self.assertEqual(after.recall_count, before.recall_count)
        self.assertEqual(after.unique_query_count, before.unique_query_count)
        self.assertEqual(after.last_activity_at, before.last_activity_at)

    def test_rem_queue_skips_fresh_reviews_and_uses_oldest_unreviewed(self) -> None:
        candidates = [
            self.service.db.upsert_candidate(
                f"长期偏好 {index}",
                kind="preference",
                confidence=0.9,
                sensitive=False,
                raw_turn_id=None,
                excerpt=f"长期偏好 {index}",
            )
            for index in range(31)
        ]
        now = utc_now()
        with self.service.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE candidates SET rem_status='approved',rem_reviewed_at=?,last_activity_at=? "
                "WHERE id=?",
                (now, now, candidates[0].id),
            )
        due = self.service.db.candidates_due_for_rem(limit=30)
        self.assertEqual(len(due), 30)
        self.assertNotIn(candidates[0].id, {item.id for item in due})

    def test_related_explicit_write_edit_and_restore_require_review(self) -> None:
        first = self.service.db.add_memory(
            "用户偏好简洁的中文回答", kind="preference", origin="manual"
        )
        second = self.service.db.add_memory(
            "用户偏好详细的中文回答", kind="preference", origin="manual"
        )
        self.service.rebuild_derived()
        remembered = self.service.remember(
            "用户偏好简洁清晰的中文回答", kind="preference"
        )
        self.assertEqual(remembered["status"], "review_required")
        self.assertEqual(len(self.service.list_memories()), 2)
        edited = self.service.update_memory(
            first.id, "用户偏好详细中文回答", "preference"
        )
        self.assertEqual(edited["status"], "review_required")
        self.assertEqual(self.service.db.get_memory(first.id).content, first.content)
        self.service.trash_memory(first.id)
        restored = self.service.restore_memory(first.id)
        self.assertEqual(restored["status"], "review_required")
        self.assertEqual(self.service.db.get_memory(first.id).status, "trashed")
        self.assertEqual(self.service.db.get_memory(second.id).status, "active")

    def test_invalid_deep_batch_has_no_partial_write(self) -> None:
        for index in range(2):
            content = f"跨日稳定约束 {index}"
            for day in (2, 1):
                raw_id = self.service.db.add_raw_turn(
                    f"seed-{index}-{day}", content, "ok", redacted=False
                )
                self.service.db.upsert_candidate(
                    content,
                    kind="fact",
                    confidence=0.95,
                    sensitive=False,
                    raw_turn_id=raw_id,
                    excerpt=content,
                    observed_at=(datetime.now(UTC) - timedelta(days=day)).isoformat(),
                )
                self.service.db.mark_turns_ingested([raw_id])
        outcome = DreamEngine(self.service.db, PartialInvalidDeepClient()).run()
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(self.service.list_memories(), [])
        self.assertEqual(len(self.service.list_candidates()), 2)

    def test_v5_upgrade_creates_backup_and_freezes_legacy_candidates(self) -> None:
        path = self.root / "upgrade-v5.db"
        old = MemoryDatabase(path)
        candidate = old.upsert_candidate(
            "旧版待审候选",
            kind="fact",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=None,
            excerpt="旧版待审候选",
        )
        with old.transaction(immediate=True) as conn:
            conn.execute("UPDATE schema_meta SET version=5")
            conn.execute("DROP INDEX IF EXISTS idx_candidates_promoted_memory")
            conn.execute(
                "CREATE UNIQUE INDEX idx_candidates_promoted_memory ON candidates(promoted_memory_id) "
                "WHERE promoted_memory_id IS NOT NULL"
            )
        migrated = MemoryDatabase(path)
        self.assertEqual(migrated.get_candidate(candidate.id).admission_state, "legacy_review")
        self.assertEqual(migrated.candidates_due_for_rem(), [])
        self.assertTrue(any((path.parent / "backups").glob("*-pre-schema-v6.db")))
        with migrated.connect() as conn:
            self.assertEqual(conn.execute("SELECT version FROM schema_meta").fetchone()[0], 7)
            self.assertEqual(
                conn.execute(
                    "SELECT [unique] FROM pragma_index_list('candidates') "
                    "WHERE name='idx_candidates_promoted_memory'"
                ).fetchone()[0],
                0,
            )

    def test_dismissed_fingerprint_is_not_reopened_until_content_changes(self) -> None:
        memory = self.service.db.add_memory("用户偏好简洁回答", kind="preference")
        review = self.service.db.create_review_item(
            issue_type="duplicate",
            proposed_action="merge",
            proposed_content=memory.content,
            reason="duplicate",
            primary_memory_id=memory.id,
            basis_hash=content_hash(memory.content),
        )
        self.service.dismiss_review(review.id)
        same = self.service.db.create_review_item(
            issue_type="duplicate",
            proposed_action="merge",
            proposed_content=memory.content,
            reason="duplicate again",
            primary_memory_id=memory.id,
            basis_hash=content_hash(memory.content),
        )
        self.assertEqual(same.id, review.id)
        self.assertEqual(same.status, "dismissed")
        changed = self.service.db.create_review_item(
            issue_type="duplicate",
            proposed_action="merge",
            proposed_content=memory.content + " 新版本",
            reason="content changed",
            primary_memory_id=memory.id,
            basis_hash=content_hash(memory.content + " 新版本"),
        )
        self.assertNotEqual(changed.id, review.id)
        self.assertEqual(changed.status, "open")

    def test_full_audit_queues_legacy_and_memory_issues_without_mutation(self) -> None:
        first = self.service.db.add_memory("用户偏好简洁中文回答", kind="preference")
        second = self.service.db.add_memory("用户喜欢简洁的中文回答", kind="preference")
        legacy = self.service.db.upsert_candidate(
            "旧版项目进度候选",
            kind="project",
            confidence=0.8,
            sensitive=False,
            raw_turn_id=None,
            excerpt="旧版项目进度候选",
            admission_state="legacy_review",
            source_type="legacy",
        )
        self.service.rebuild_derived()
        self.service.llm_client = lambda: AuditClient()  # type: ignore[method-assign]
        result = self.service.run_memory_audit(scope="full")
        self.assertEqual(result["status"], "completed")
        issue_types = {item["issue_type"] for item in self.service.list_reviews()}
        self.assertIn("legacy_admission", issue_types)
        self.assertIn("duplicate", issue_types)
        self.assertEqual(self.service.db.get_memory(first.id).status, "active")
        self.assertEqual(self.service.db.get_memory(second.id).status, "active")
        self.assertEqual(self.service.db.get_candidate(legacy.id).status, "pending")

    def test_privacy_purge_cleans_new_admission_and_review_tables(self) -> None:
        result = self.service.remember("我的银行卡需要单独管理", kind="fact")
        candidate_id = result["candidate"]["id"]
        self.assertEqual(len(self.service.list_reviews()), 1)
        self.service.purge_candidate(candidate_id)
        with self.service.db.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM admission_decisions WHERE candidate_id=? OR content LIKE '%银行卡%'",
                    (candidate_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_review_items WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
