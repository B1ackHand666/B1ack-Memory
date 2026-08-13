from __future__ import annotations

import tempfile
import unittest
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import b1ack_memory
from b1ack_memory.dream import DreamEngine
from b1ack_memory.dream import LIGHT_SYSTEM, REM_SYSTEM
from b1ack_memory.llm import LlmError, LlmResult, OpenAICompatibleClient
from b1ack_memory.security import SecretStore, contains_secret, is_sensitive, redact_secrets
from b1ack_memory.service import MemoryService


class FakeClient:
    configured = True
    model = "fake-json-model"

    def chat_json(self, *, system: str, user: str) -> LlmResult:
        import json

        payload = json.loads(user)
        if "strict admission gate" in system:
            turn = payload["turns"][0]
            parsed = {
                "decisions": [
                    {
                        "disposition": "admit",
                        "content": "用户偏好简洁的中文回答",
                        "kind": "preference",
                        "confidence": 0.94,
                        "sensitive": False,
                        "source_turn_id": turn["id"],
                        "evidence_quote": turn["user"],
                        "explanation": "用户明确表达稳定偏好",
                    }
                ]
            }
        elif "Review every supplied" in system:
            parsed = {
                "summary": "一项偏好",
                "reviews": [
                    {
                        "candidate_id": item["id"],
                        "decision": "deferred",
                        "explanation": "等待跨日证据",
                    }
                    for item in payload["candidates"]
                ],
            }
        elif "conservative integration gate" in system:
            parsed = {"integrations": []}
        elif "Audit active personal memories" in system:
            parsed = {"issues": []}
        else:
            parsed = {"action": "defer", "explanation": "test", "confidence": 0}
        return LlmResult(parsed=parsed, raw={"ok": True}, input_tokens=10, output_tokens=5)


class ConflictClient(FakeClient):
    def chat_json(self, *, system: str, user: str) -> LlmResult:
        import json

        if "Review every supplied" not in system:
            return super().chat_json(system=system, user=user)
        payload = json.loads(user)
        target = next(
            item
            for item in payload["candidates"][0]["related_records"]
            if item["source"] == "memory"
        )
        parsed = {
            "summary": "发现偏好冲突",
            "reviews": [
                {
                    "candidate_id": payload["candidates"][0]["id"],
                    "decision": "conflict",
                    "target_id": target["id"],
                    "explanation": "新旧偏好不一致",
                }
            ],
        }
        return LlmResult(parsed=parsed, raw=parsed, input_tokens=10, output_tokens=5)


class ApprovingClient(FakeClient):
    def chat_json(self, *, system: str, user: str) -> LlmResult:
        import json

        if "Review every supplied" in system:
            payload = json.loads(user)
            parsed = {
                "summary": "耐久信息已通过复核",
                "reviews": [
                    {
                        "candidate_id": item["id"],
                        "decision": "durable",
                        "explanation": "稳定偏好",
                    }
                    for item in payload["candidates"]
                ],
            }
            return LlmResult(parsed=parsed, raw=parsed, input_tokens=10, output_tokens=5)
        if "conservative integration gate" in system:
            payload = json.loads(user)
            parsed = {
                "integrations": [
                    {
                        "candidate_id": item["id"],
                        "action": "create",
                        "content": item["content"],
                        "explanation": "全池无相关项",
                        "confidence": 0.95,
                    }
                    for item in payload["candidates"]
                ]
            }
            return LlmResult(parsed=parsed, raw=parsed, input_tokens=10, output_tokens=5)
        return super().chat_json(system=system, user=user)


class ExistingApprovingClient(ApprovingClient):
    def chat_json(self, *, system: str, user: str) -> LlmResult:
        if "strict admission gate" in system:
            parsed = {"decisions": []}
            return LlmResult(parsed=parsed, raw=parsed, input_tokens=10, output_tokens=5)
        return super().chat_json(system=system, user=user)


class ManyCandidateClient(ExistingApprovingClient):
    def chat_json(self, *, system: str, user: str) -> LlmResult:
        import json

        if "strict admission gate" in system:
            turn = json.loads(user)["turns"][0]
            parsed = {
                "decisions": [
                    {
                        "disposition": "admit",
                        "content": f"用户的稳定偏好编号 {index}",
                        "kind": "preference",
                        "confidence": 0.9,
                        "sensitive": False,
                        "source_turn_id": turn["id"],
                        "evidence_quote": turn["user"],
                        "explanation": "稳定偏好",
                    }
                    for index in range(12)
                ]
            }
            return LlmResult(parsed=parsed, raw=parsed, input_tokens=10, output_tokens=5)
        return super().chat_json(system=system, user=user)


class DuplicateReviewClient(ExistingApprovingClient):
    def chat_json(self, *, system: str, user: str) -> LlmResult:
        import json

        if "Review every supplied" in system:
            payload = json.loads(user)
            candidates = payload["candidates"]
            parsed = {
                "summary": "合并同义候选",
                "reviews": [
                    {
                        "candidate_id": candidates[0]["id"],
                        "decision": "durable",
                        "explanation": "稳定偏好",
                    },
                    {
                        "candidate_id": candidates[1]["id"],
                        "decision": "duplicate_candidate",
                        "target_id": candidates[0]["id"],
                        "explanation": "表达不同但含义相同",
                    }
                ],
            }
            return LlmResult(parsed=parsed, raw=parsed, input_tokens=10, output_tokens=5)
        return super().chat_json(system=system, user=user)


class NoiseReviewClient(ExistingApprovingClient):
    def chat_json(self, *, system: str, user: str) -> LlmResult:
        import json

        if "Review every supplied" in system:
            candidate_id = json.loads(user)["candidates"][0]["id"]
            parsed = {
                "summary": "发现一次性噪声",
                "reviews": [
                    {
                        "candidate_id": candidate_id,
                        "decision": "noise",
                        "explanation": "一次性任务进度",
                    }
                ],
            }
            return LlmResult(parsed=parsed, raw=parsed, input_tokens=10, output_tokens=5)
        return super().chat_json(system=system, user=user)


class StructuredDuplicateClient(ExistingApprovingClient):
    def chat_json(self, *, system: str, user: str) -> LlmResult:
        import json

        payload = json.loads(user)
        if "Review every supplied" in system:
            candidates = payload["candidates"]
            canonical = next(item for item in candidates if "简洁" in item["content"])
            duplicate = next(item for item in candidates if item["id"] != canonical["id"])
            parsed = {
                "summary": "确认两种中文措辞表达同一项稳定偏好",
                "reviews": [
                    {
                        "candidate_id": canonical["id"],
                        "decision": "durable",
                        "target_id": None,
                        "explanation": "稳定偏好",
                    },
                    {
                        "candidate_id": duplicate["id"],
                        "decision": "duplicate_candidate",
                        "target_id": canonical["id"],
                        "explanation": "中文措辞不同但含义相同",
                    },
                ],
            }
            return LlmResult(parsed=parsed, raw=parsed, input_tokens=10, output_tokens=5)
        if "conservative integration gate" in system:
            candidate = payload["candidates"][0]
            parsed = {
                "integrations": [
                    {
                        "candidate_id": candidate["id"],
                        "action": "create",
                        "content": "用户偏好简洁、清晰的中文回答。",
                        "explanation": "新记忆",
                        "confidence": 0.95,
                    }
                ]
            }
            return LlmResult(parsed=parsed, raw=parsed, input_tokens=10, output_tokens=5)
        return super().chat_json(system=system, user=user)


class UnknownDeepCandidateClient(ApprovingClient):
    def chat_json(self, *, system: str, user: str) -> LlmResult:
        if "conservative integration gate" in system:
            parsed = {
                "integrations": [
                    {
                        "candidate_id": "not-a-qualified-candidate",
                        "action": "create",
                        "content": "不能凭空创建",
                        "explanation": "invalid",
                        "confidence": 1.0,
                    }
                ]
            }
            return LlmResult(parsed=parsed, raw=parsed, input_tokens=10, output_tokens=5)
        return super().chat_json(system=system, user=user)


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = MemoryService(self.root)

    def tearDown(self) -> None:
        self.service.shutdown(timeout=0.1)
        self.temp.cleanup()

    def test_memory_lifecycle_and_chinese_search(self) -> None:
        result = self.service.remember("我偏好简洁的中文回答", kind="preference")
        record_id = result["memory"]["id"]
        hits = self.service.search("中文回答", include_candidates=False)
        self.assertEqual(hits[0].id, record_id)
        self.service.update_memory(record_id, "我偏好非常简洁的中文回答", "preference")
        self.service.trash_memory(record_id)
        self.assertEqual(self.service.list_memories(status="active"), [])
        self.service.restore_memory(record_id)
        self.assertEqual(self.service.list_memories()[0]["id"], record_id)

    def test_sensitive_memory_requires_review(self) -> None:
        result = self.service.remember("我的银行卡需要单独管理", kind="fact")
        self.assertEqual(result["status"], "review_required")
        self.assertTrue(result["candidate"]["sensitive"])

    def test_dream_extracts_but_does_not_eagerly_promote(self) -> None:
        self.service.capture_turn("s1", "请记住我偏好简洁中文", "好的", )
        outcome = DreamEngine(self.service.db, FakeClient()).run()
        self.service.rebuild_derived()
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.candidate_count, 1)
        self.assertEqual(outcome.promoted_count, 0)
        self.assertEqual(len(self.service.list_candidates()), 1)
        self.assertEqual(self.service.status()["counts"]["pending_turns"], 0)

    def test_backup_and_restore(self) -> None:
        first = self.service.remember("原始内容")["memory"]["id"]
        backup = self.service.create_backup()
        self.service.update_memory(first, "修改内容", "fact")
        self.service.restore_backup(backup.name)
        self.assertEqual(self.service.list_memories()[0]["content"], "原始内容")

    def test_restore_is_safe_when_only_one_backup_is_kept(self) -> None:
        self.service.save_settings("retention", {"backup_count": 1})
        record_id = self.service.remember("恢复前内容")["memory"]["id"]
        selected = self.service.create_backup()
        self.service.update_memory(record_id, "恢复后修改", "fact")
        self.service.restore_backup(selected.name)
        self.assertEqual(self.service.list_memories()[0]["content"], "恢复前内容")
        self.assertEqual(len(self.service.list_backups()), 1)

    def test_invalid_purge_preserves_memory_and_backups(self) -> None:
        self.service.remember("不能误删")
        backup = self.service.create_backup()
        with self.assertRaises(KeyError):
            self.service.purge_memory("missing-id")
        self.assertEqual(len(self.service.list_memories()), 1)
        self.assertTrue(any(item["name"] == backup.name for item in self.service.list_backups()))

    def test_purge_removes_linked_private_residue(self) -> None:
        self.service.capture_turn("s1", "PURGE_SENTINEL 用户文本", "已记录")
        DreamEngine(self.service.db, FakeClient()).run()
        candidate_id = self.service.list_candidates()[0]["id"]
        memory_id = self.service.promote_candidate(candidate_id)["memory"]["id"]
        old_backup = self.service.create_backup().name
        self.service.trash_memory(memory_id)
        self.assertEqual(
            self.service.db.get_candidate(candidate_id).promoted_memory_id,
            memory_id,
        )
        self.assertEqual(
            self.service.list_memories(status="trashed")[0]["lineage"]["id"],
            candidate_id,
        )
        self.assertEqual(self.service.memory_lineage(memory_id)["candidate"]["id"], candidate_id)
        result = self.service.purge_memory(memory_id)
        self.assertEqual(result["removed"]["memories"], 1)
        self.assertEqual(self.service.list_memories(), [])
        self.assertEqual(self.service.list_candidates(status="promoted"), [])
        self.assertFalse(any("PURGE_SENTINEL" in str(item) for item in self.service.model_calls()))
        self.assertFalse(any(item["name"] == old_backup for item in self.service.list_backups()))
        with self.service.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM raw_turns").fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_events WHERE candidate_id=? OR memory_id=?",
                    (candidate_id, memory_id),
                ).fetchone()[0],
                0,
            )

    def test_corrupt_backup_cannot_replace_live_database(self) -> None:
        self.service.remember("当前数据")
        corrupt = self.service.backup_dir / "corrupt.db"
        corrupt.write_text("not a sqlite database", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.service.restore_backup(corrupt.name)
        self.assertEqual(self.service.list_memories()[0]["content"], "当前数据")

    def test_dry_run_has_no_persistent_side_effects(self) -> None:
        self.service.capture_turn("s1", "请记住我偏好简洁中文", "好的")
        outcome = DreamEngine(self.service.db, FakeClient()).run(dry_run=True)
        self.assertEqual(outcome.status, "dry_run")
        self.assertEqual(self.service.status()["counts"]["pending_turns"], 1)
        self.assertEqual(self.service.list_candidates(), [])
        self.assertEqual(self.service.list_dream_runs(), [])
        self.assertEqual(self.service.model_calls(), [])

    def test_rem_conflict_is_persisted_for_manual_review(self) -> None:
        existing_id = self.service.remember("用户偏好详细回答", kind="preference")["memory"]["id"]
        self.service.capture_turn("s1", "我现在偏好简洁回答", "好的")
        outcome = DreamEngine(self.service.db, ConflictClient()).run()
        self.assertEqual(outcome.status, "completed")
        candidate = self.service.list_candidates()[0]
        self.assertEqual(candidate["rem_status"], "deferred")
        review = self.service.list_reviews()[0]
        self.assertEqual(review["issue_type"], "conflict")
        self.assertEqual(review["related_memory_id"], existing_id)

    def test_evidence_days_use_original_observation_date(self) -> None:
        first = self.service.db.add_raw_turn("s1", "u1", "a1", redacted=False)
        second = self.service.db.add_raw_turn("s2", "u2", "a2", redacted=False)
        self.service.db.upsert_candidate(
            "跨日重复偏好",
            kind="preference",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=first,
            excerpt="跨日重复偏好",
            observed_at="2026-07-01T08:00:00+00:00",
        )
        candidate = self.service.db.upsert_candidate(
            "跨日重复偏好",
            kind="preference",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=second,
            excerpt="跨日重复偏好",
            observed_at="2026-07-02T08:00:00+00:00",
        )
        self.assertEqual(candidate.evidence_days, 2)

    def test_candidate_without_conversation_evidence_starts_at_zero(self) -> None:
        candidate = self.service.db.upsert_candidate(
            "人工建立但尚无会话证据的候选",
            kind="fact",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=None,
            excerpt="人工候选",
        )
        self.assertEqual(candidate.evidence_days, 0)

    def test_evidence_dates_follow_configured_timezone_boundaries(self) -> None:
        first = self.service.db.add_raw_turn("tz1", "u1", "a1", redacted=False)
        second = self.service.db.add_raw_turn("tz2", "u2", "a2", redacted=False)
        self.service.save_settings("general", {"timezone": "Asia/Shanghai"})
        self.service.db.upsert_candidate(
            "北京时间跨日偏好",
            kind="preference",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=first,
            excerpt="第一次证据",
            observed_at="2026-08-08T15:30:00+00:00",
            timezone_name="Asia/Shanghai",
        )
        candidate = self.service.db.upsert_candidate(
            "北京时间跨日偏好",
            kind="preference",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=second,
            excerpt="第二次证据",
            observed_at="2026-08-08T16:30:00+00:00",
            timezone_name="Asia/Shanghai",
        )
        self.assertEqual(candidate.evidence_days, 2)
        self.service.save_settings("general", {"timezone": "UTC"})
        self.assertEqual(self.service.db.get_candidate(candidate.id).evidence_days, 1)
        self.service.save_settings("general", {"timezone": "Asia/Shanghai"})
        self.assertEqual(self.service.db.get_candidate(candidate.id).evidence_days, 2)

    def test_same_local_date_and_dst_dates_are_counted_safely(self) -> None:
        from b1ack_memory.db import local_date

        self.assertEqual(
            local_date("2026-08-08T23:30:00+00:00", "Asia/Shanghai"),
            local_date("2026-08-09T01:30:00+00:00", "Asia/Shanghai"),
        )
        self.assertNotEqual(
            local_date("2026-03-08T04:30:00+00:00", "America/New_York"),
            local_date("2026-03-08T07:30:00+00:00", "America/New_York"),
        )
        with self.assertRaisesRegex(ValueError, "IANA timezone"):
            self.service.save_settings("general", {"timezone": "Mars/Local"})

    def test_dream_caps_new_candidates_at_eight(self) -> None:
        self.service.capture_turn("s1", "这里包含很多长期偏好", "好的")
        outcome = DreamEngine(self.service.db, ManyCandidateClient()).run()
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.candidate_count, 8)
        self.assertEqual(outcome.filtered_count, 4)
        self.assertEqual(len(self.service.list_candidates()), 8)

    def test_repeat_evidence_lane_auto_promotes(self) -> None:
        raw_id = self.service.db.add_raw_turn("old", "旧证据", "好的", redacted=False)
        self.service.db.upsert_candidate(
            "用户偏好简洁的中文回答",
            kind="preference",
            confidence=0.94,
            sensitive=False,
            raw_turn_id=raw_id,
            excerpt="用户偏好简洁的中文回答",
            observed_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
        )
        self.service.db.mark_turns_ingested([raw_id])
        self.service.capture_turn("today", "请记住我偏好简洁中文", "好的")
        outcome = DreamEngine(self.service.db, ApprovingClient()).run()
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.promoted_count, 1)
        self.assertEqual(len(self.service.list_memories()), 1)

    def test_deep_cannot_create_memory_without_a_qualified_candidate_id(self) -> None:
        for index, observed in enumerate(
            ["2026-08-07T08:00:00+00:00", "2026-08-08T08:00:00+00:00"]
        ):
            raw_id = self.service.db.add_raw_turn(
                f"deep-guard-{index}", "用户偏好简洁中文", "好的", redacted=False
            )
            self.service.db.upsert_candidate(
                "用户偏好简洁的中文回答",
                kind="preference",
                confidence=0.94,
                sensitive=False,
                raw_turn_id=raw_id,
                excerpt="用户偏好简洁的中文回答",
                observed_at=observed,
                timezone_name="Asia/Shanghai",
            )
            self.service.db.mark_turns_ingested([raw_id])
        self.service.save_settings("general", {"timezone": "Asia/Shanghai"})
        self.service.capture_turn("deep-guard-trigger", "继续", "好的")
        outcome = DreamEngine(self.service.db, UnknownDeepCandidateClient()).run()
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.promoted_count, 0)
        self.assertEqual(self.service.list_memories(), [])
        self.assertEqual(len(self.service.list_candidates()), 1)

    def test_search_and_historical_injections_never_promote_candidates(self) -> None:
        candidate = self.service.db.upsert_candidate(
            "用户偏好黑咖啡",
            kind="preference",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=None,
            excerpt="用户偏好黑咖啡",
        )
        self.service.rebuild_derived()
        self.service.search("黑咖啡偏好", injected=True)
        self.service.search("用户喝什么咖啡", injected=True)
        self.service.capture_turn("trigger", "今天继续工作", "好的")
        outcome = DreamEngine(self.service.db, ExistingApprovingClient()).run()
        self.assertEqual(outcome.promoted_count, 0)
        self.assertEqual(self.service.list_memories(), [])
        refreshed = self.service.db.get_candidate(candidate.id)
        self.assertEqual(refreshed.recall_count, 0)
        self.assertEqual(refreshed.unique_query_count, 0)

    def test_rem_merges_semantic_duplicates_and_expires_noise(self) -> None:
        first = self.service.db.upsert_candidate(
            "用户喜欢简洁的中文回答",
            kind="preference",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=None,
            excerpt="用户喜欢简洁的中文回答",
        )
        self.service.db.upsert_candidate(
            "回答用户时应使用精炼中文",
            kind="preference",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=None,
            excerpt="回答用户时应使用精炼中文",
        )
        self.service.capture_turn("merge", "继续", "好的")
        outcome = DreamEngine(self.service.db, DuplicateReviewClient()).run()
        self.assertEqual(outcome.merged_count, 0)
        pending = self.service.list_candidates()
        self.assertEqual(len(pending), 2)
        duplicate_review = next(
            item for item in self.service.list_reviews() if item["issue_type"] == "duplicate_candidate"
        )
        self.service.resolve_review(duplicate_review["id"], action="merge")
        pending = self.service.list_candidates()
        self.assertEqual(len(pending), 1)
        canonical_id = pending[0]["id"]

        with self.service.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE candidates SET rem_status='unreviewed',rem_reviewed_at=NULL WHERE id=?",
                (canonical_id,),
            )
        self.service.capture_turn("noise", "继续", "好的")
        noisy = DreamEngine(self.service.db, NoiseReviewClient()).run()
        self.assertEqual(noisy.expired_count, 1)
        self.assertEqual(self.service.db.get_candidate(canonical_id).status, "expired")

    def test_structured_rem_merges_chinese_evidence_and_records_deep_lineage(self) -> None:
        first_turn = self.service.db.add_raw_turn("cn-1", "偏好简洁", "好的", redacted=False)
        second_turn = self.service.db.add_raw_turn("cn-2", "请精炼作答", "好的", redacted=False)
        first = self.service.db.upsert_candidate(
            "用户偏好简洁的中文回答",
            kind="preference",
            confidence=0.92,
            sensitive=False,
            raw_turn_id=first_turn,
            excerpt="用户偏好简洁的中文回答",
            observed_at="2026-08-07T08:00:00+00:00",
            timezone_name="Asia/Shanghai",
        )
        self.service.db.upsert_candidate(
            "回答用户时应使用精炼中文",
            kind="preference",
            confidence=0.91,
            sensitive=False,
            raw_turn_id=second_turn,
            excerpt="回答用户时应使用精炼中文",
            observed_at="2026-08-08T08:00:00+00:00",
            timezone_name="Asia/Shanghai",
        )
        self.service.save_settings("general", {"timezone": "Asia/Shanghai"})
        self.service.db.mark_turns_ingested([first_turn, second_turn])
        self.service.capture_turn("trigger-cn", "继续", "好的")
        outcome = DreamEngine(self.service.db, StructuredDuplicateClient()).run()
        self.assertEqual(outcome.merged_count, 0)
        self.assertEqual(outcome.promoted_count, 0)
        duplicate_review = next(
            item for item in self.service.list_reviews() if item["issue_type"] == "duplicate_candidate"
        )
        self.service.resolve_review(duplicate_review["id"], action="merge")
        trigger = self.service.db.add_raw_turn("trigger-2", "继续", "好的", redacted=False)
        self.service.db.mark_turns_ingested([trigger])
        second = DreamEngine(self.service.db, ApprovingClient()).run()
        self.assertEqual(second.promoted_count, 1)
        promoted = self.service.list_candidates(status="promoted")[0]
        self.assertEqual(promoted["id"], first.id)
        self.assertIsNotNone(promoted["promoted_memory_id"])
        self.assertEqual(promoted["evidence_days"], 2)
        self.assertEqual(promoted["rem_reason"], "稳定偏好")
        lineage = self.service.candidate_lineage(first.id)
        promotion = next(
            item for item in lineage["events"] if item["event_type"] == "candidate_promoted"
        )
        self.assertEqual(promotion["data"]["promotion_lane"], "different_dates")
        self.assertEqual(promotion["data"]["candidate_content"], "用户偏好简洁的中文回答")
        self.assertEqual(promotion["data"]["memory_content"], "用户偏好简洁的中文回答")
        self.assertEqual(lineage["memory"]["origin"], "dream")
        self.assertTrue(any(item["event_type"] == "candidate_merged" for item in lineage["events"]))
        self.assertTrue(
            any(
                item["event_type"] == "rem_reviewed"
                and item["data"]["reason"] == "稳定偏好"
                for item in lineage["events"]
            )
        )

    def test_auto_promotion_is_capped_per_local_day(self) -> None:
        for index in range(4):
            content = f"跨日稳定偏好 {index}"
            for day in (2, 1):
                raw_id = self.service.db.add_raw_turn(
                    f"seed-{index}-{day}", content, "好的", redacted=False
                )
                self.service.db.upsert_candidate(
                    content,
                    kind="preference",
                    confidence=0.9,
                    sensitive=False,
                    raw_turn_id=raw_id,
                    excerpt=content,
                    observed_at=(datetime.now(UTC) - timedelta(days=day)).isoformat(),
                )
                self.service.db.mark_turns_ingested([raw_id])
        self.service.capture_turn("first", "继续", "好的")
        first = DreamEngine(self.service.db, ExistingApprovingClient()).run()
        self.assertEqual(first.promoted_count, 3)
        self.service.capture_turn("second", "继续", "好的")
        second = DreamEngine(self.service.db, ExistingApprovingClient()).run()
        self.assertEqual(second.promoted_count, 0)
        self.assertEqual(len(self.service.list_memories()), 3)

    def test_candidate_expiration_rejection_and_cleanup(self) -> None:
        candidate = self.service.db.upsert_candidate(
            "长期候选",
            kind="fact",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=None,
            excerpt="长期候选",
        )
        now = datetime(2026, 8, 5, tzinfo=UTC)
        with self.service.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE candidates SET last_activity_at=? WHERE id=?",
                ((now - timedelta(days=15)).isoformat(), candidate.id),
            )
        first = self.service.db.retention_cleanup(365, 365, 14, 30, 30, now=now)
        self.assertEqual(first["expired_candidates"], 1)
        self.assertEqual(self.service.db.get_candidate(candidate.id).status, "expired")
        with self.service.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE candidates SET expired_at=? WHERE id=?",
                ((now - timedelta(days=31)).isoformat(), candidate.id),
            )
        second = self.service.db.retention_cleanup(365, 365, 14, 30, 30, now=now)
        self.assertEqual(second["purged_candidates"], 1)
        self.assertIsNone(self.service.db.get_candidate(candidate.id))

        rejected = self.service.db.upsert_candidate(
            "不要重复建议晨跑",
            kind="preference",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=None,
            excerpt="不要重复建议晨跑",
        )
        self.service.reject_candidate(rejected.id)
        suppressed = self.service.db.upsert_candidate(
            "不要重复建议晨跑",
            kind="preference",
            confidence=0.99,
            sensitive=False,
            raw_turn_id=None,
            excerpt="不要重复建议晨跑",
        )
        self.assertEqual(suppressed.status, "rejected")
        self.assertEqual(self.service.list_candidates(), [])

    def test_candidate_privacy_purge_removes_old_backups(self) -> None:
        self.service.capture_turn("s1", "候选隐私删除标记", "好的")
        DreamEngine(self.service.db, FakeClient()).run()
        candidate_id = self.service.list_candidates()[0]["id"]
        old_backup = self.service.create_backup().name
        result = self.service.purge_candidate(candidate_id)
        self.assertEqual(result["removed"]["candidates"], 1)
        self.assertIsNone(self.service.db.get_candidate(candidate_id))
        self.assertNotIn(old_backup, {item["name"] for item in self.service.list_backups()})
        self.assertTrue(result["clean_backup"].endswith("-post-purge.zip"))
        with self.service.db.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_events WHERE candidate_id=?", (candidate_id,)
                ).fetchone()[0],
                0,
            )

    def test_active_long_term_memory_cannot_be_hard_deleted(self) -> None:
        memory_id = self.service.remember("需要先回收的长期记忆")["memory"]["id"]
        with self.assertRaisesRegex(ValueError, "trash"):
            self.service.purge_memory(memory_id)

    def test_promotion_to_an_already_linked_memory_merges_the_duplicate_candidate(self) -> None:
        first = self.service.db.upsert_candidate(
            "同一长期事实",
            kind="fact",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=None,
            excerpt="first",
        )
        memory_id = self.service.promote_candidate(first.id)["memory"]["id"]
        duplicate = self.service.db.upsert_candidate(
            "同一事实的另一种候选表述",
            kind="fact",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=None,
            excerpt="duplicate",
        )
        self.service.promote_candidate(duplicate.id, "同一长期事实")
        self.assertEqual(self.service.db.get_candidate(duplicate.id).promoted_memory_id, memory_id)
        self.assertEqual(self.service.db.get_candidate(first.id).promoted_memory_id, memory_id)
        self.assertEqual(len(self.service.list_candidates(status="promoted")), 2)

    def test_schema_v2_candidate_migrates_without_immediate_deletion(self) -> None:
        from b1ack_memory.db import MemoryDatabase

        path = self.root / "legacy.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE schema_meta(version INTEGER NOT NULL);
            INSERT INTO schema_meta VALUES(2);
            CREATE TABLE candidates (
                id TEXT PRIMARY KEY, content TEXT NOT NULL, kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', model_confidence REAL NOT NULL DEFAULT 0,
                sensitive INTEGER NOT NULL DEFAULT 0, score REAL NOT NULL DEFAULT 0,
                score_components TEXT NOT NULL DEFAULT '{}', recall_count INTEGER NOT NULL DEFAULT 0,
                unique_query_count INTEGER NOT NULL DEFAULT 0, evidence_days INTEGER NOT NULL DEFAULT 1,
                conflict_memory_id TEXT, conflict_reason TEXT, content_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
            );
            INSERT INTO candidates VALUES(
                'legacy','旧候选','fact','pending',0.9,0,0,'{}',0,0,1,NULL,NULL,
                'hash','2026-01-01T00:00:00+00:00','2026-01-02T00:00:00+00:00'
            );
            """
        )
        conn.close()
        legacy = MemoryDatabase(path)
        migrated = legacy.get_candidate("legacy")
        self.assertIsNotNone(migrated)
        self.assertEqual(migrated.status, "pending")
        self.assertEqual(migrated.last_activity_at, migrated.last_seen_at)

    def test_schema_v3_backfills_memory_events_idempotently(self) -> None:
        from b1ack_memory.db import MemoryDatabase

        path = self.root / "legacy-v3.db"
        legacy = MemoryDatabase(path)
        candidate = legacy.upsert_candidate(
            "旧版候选",
            kind="fact",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=None,
            excerpt="旧版候选",
        )
        memory = legacy.promote_candidate(candidate.id, origin="dream")
        self.assertEqual(legacy.get_candidate(candidate.id).promoted_memory_id, memory.id)
        conn = sqlite3.connect(path)
        try:
            conn.execute("DROP TABLE memory_events")
            conn.execute("UPDATE schema_meta SET version=3")
            conn.commit()
        finally:
            conn.close()
        migrated = MemoryDatabase(path)
        with migrated.connect() as conn:
            self.assertEqual(conn.execute("SELECT version FROM schema_meta").fetchone()[0], 7)
            first_count = conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
            backfilled = conn.execute(
                "SELECT data_json,backfilled FROM memory_events "
                "WHERE event_type='candidate_promoted' AND candidate_id=?",
                (candidate.id,),
            ).fetchone()
        self.assertGreaterEqual(first_count, 3)
        self.assertEqual(backfilled["backfilled"], 1)
        self.assertIn('"promotion_lane": "unknown"', backfilled["data_json"])
        migrated.migrate()
        with migrated.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0], first_count)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memories WHERE id=?", (memory.id,)).fetchone()[0], 1)

    def test_schema_v4_links_promoted_candidates_and_cleans_only_orphans(self) -> None:
        from b1ack_memory.db import MemoryDatabase, utc_now

        path = self.root / "legacy-v4.db"
        legacy = MemoryDatabase(path)

        linked: dict[str, tuple[str, str]] = {}
        for strategy in ("event", "evidence", "model", "exact"):
            candidate = legacy.upsert_candidate(
                f"{strategy} 可证明晋升",
                kind="fact",
                confidence=0.9,
                sensitive=False,
                raw_turn_id=None,
                excerpt=strategy,
            )
            memory = legacy.promote_candidate(candidate.id, origin="review")
            linked[strategy] = (candidate.id, memory.id)

        orphan = legacy.upsert_candidate(
            "没有关联的旧晋升候选",
            kind="fact",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=None,
            excerpt="orphan",
        )
        ambiguous = legacy.upsert_candidate(
            "存在歧义的旧晋升候选",
            kind="fact",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=None,
            excerpt="ambiguous",
        )
        now = utc_now()
        with legacy.transaction(immediate=True) as conn:
            conn.execute("UPDATE candidates SET promoted_memory_id=NULL")
            conn.execute(
                "INSERT INTO evidence(candidate_id,memory_id,excerpt,role,observed_at) "
                "VALUES(?,?,?,'conversation',?)",
                (linked["evidence"][0], linked["evidence"][1], "legacy evidence", now),
            )
            conn.execute(
                "DELETE FROM memory_events WHERE candidate_id=? AND event_type='candidate_promoted'",
                (linked["evidence"][0],),
            )
            for strategy in ("model", "exact"):
                conn.execute(
                    "DELETE FROM memory_events WHERE candidate_id=? AND event_type='candidate_promoted'",
                    (linked[strategy][0],),
                )
                conn.execute(
                    "UPDATE evidence SET memory_id=NULL WHERE candidate_id=?",
                    (linked[strategy][0],),
                )
            conn.execute(
                "INSERT INTO dream_runs(id,status,started_at) VALUES('legacy-run','completed',?)",
                (now,),
            )
            conn.execute(
                "INSERT INTO model_calls(id,dream_run_id,phase,request_json,response_json,model,created_at) "
                "VALUES('legacy-call','legacy-run','deep','{}','{}','legacy',?)",
                (now,),
            )
            conn.executemany(
                "INSERT INTO model_call_records(call_id,record_type,record_id) VALUES(?,?,?)",
                [
                    ("legacy-call", "candidate", linked["model"][0]),
                    ("legacy-call", "memory", linked["model"][1]),
                ],
            )
            conn.execute(
                "UPDATE candidates SET status='promoted',promotion_origin='review',promoted_at=? "
                "WHERE id IN (?,?)",
                (now, orphan.id, ambiguous.id),
            )
            digest = conn.execute(
                "SELECT content_hash FROM candidates WHERE id=?", (ambiguous.id,)
            ).fetchone()[0]
            for suffix in ("a", "b"):
                conn.execute(
                    "INSERT INTO memories(id,content,kind,status,origin,confidence,importance,"
                    "sensitive,content_hash,created_at,updated_at) "
                    "VALUES(?,?,'fact','active','review',0.9,0.5,0,?,?,?)",
                    (
                        f"ambiguous-memory-{suffix}",
                        "存在歧义的旧晋升候选",
                        digest,
                        now,
                        now,
                    ),
                )
            conn.execute("UPDATE schema_meta SET version=4")

        migrated = MemoryDatabase(path)
        for candidate_id, memory_id in linked.values():
            self.assertEqual(migrated.get_candidate(candidate_id).promoted_memory_id, memory_id)
        for candidate_id in (orphan.id, ambiguous.id):
            legacy = migrated.get_candidate(candidate_id)
            self.assertIsNotNone(legacy)
            self.assertEqual(legacy.status, "pending")
            self.assertEqual(legacy.admission_state, "legacy_review")
        with migrated.connect() as conn:
            self.assertEqual(conn.execute("SELECT version FROM schema_meta").fetchone()[0], 7)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM audit_events "
                    "WHERE action='migration-review-unlinked-promoted'"
                ).fetchone()[0],
                2,
            )
            event_count = conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
        self.assertTrue(any((path.parent / "backups").glob("*-pre-schema-v5.db")))
        migrated.migrate()
        with migrated.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0], event_count)

    def test_memory_flow_keeps_full_history_but_returns_latest_twenty(self) -> None:
        base = datetime(2026, 8, 1, tzinfo=UTC)
        expected: list[str] = []
        for index in range(25):
            candidate = self.service.db.upsert_candidate(
                f"最近变化候选 {index}",
                kind="fact",
                confidence=0.9,
                sensitive=False,
                raw_turn_id=None,
                excerpt=str(index),
            )
            occurred_at = (base + timedelta(minutes=index)).isoformat()
            expected.append(occurred_at)
            with self.service.db.transaction(immediate=True) as conn:
                conn.execute(
                    "UPDATE memory_events SET occurred_at=? "
                    "WHERE candidate_id=? AND event_type='candidate_created'",
                    (occurred_at, candidate.id),
                )
        flow = self.service.memory_flow("all")
        self.assertEqual(len(flow["recent"]), 20)
        self.assertEqual(
            [item["occurred_at"] for item in flow["recent"]],
            list(reversed(expected[-20:])),
        )
        with self.service.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0], 25)


class SecurityTests(unittest.TestCase):
    def test_secret_detection_and_redaction(self) -> None:
        value = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
        self.assertTrue(contains_secret(value))
        redacted, changed = redact_secrets("key=" + value)
        self.assertTrue(changed)
        self.assertNotIn(value, redacted)
        self.assertTrue(is_sensitive("我的身份证需要更新"))

    def test_secret_store_only_returns_mask(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SecretStore(Path(directory) / "secrets.json")
            store.save({"llm_api_key": "abcdef123456"})
            self.assertEqual(store.masked_status("llm_api_key"), {"configured": True})


class ClientTests(unittest.TestCase):
    def test_dream_prompts_bound_structured_output(self) -> None:
        self.assertIn("at most 8 objects", LIGHT_SYSTEM)
        self.assertIn("evidence_quote", LIGHT_SYSTEM)
        self.assertIn("admit|observe|discard", LIGHT_SYSTEM)
        self.assertIn("exactly one review", REM_SYSTEM)
        self.assertIn("duplicate_candidate", REM_SYSTEM)

    def test_http_client_sends_cloudflare_compatible_identity(self) -> None:
        client = OpenAICompatibleClient(
            base_url="https://opencode.ai/zen/go/v1",
            model="deepseek-v4-flash",
            api_key="test",
        )
        response = mock.MagicMock(status_code=200, text='{"ok":true}')
        with mock.patch("b1ack_memory.llm.httpx.post", return_value=response) as post:
            self.assertTrue(client._post("/chat/completions", {"model": client.model})["ok"])

        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["User-Agent"], f"B1ack-Memory/{b1ack_memory.__version__}")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["Authorization"], "Bearer test")
        self.assertTrue(post.call_args.kwargs["follow_redirects"])

    def test_http_client_keeps_urllib_fallback(self) -> None:
        client = OpenAICompatibleClient(
            base_url="http://localhost:1234/v1", model="local-model"
        )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true}'
        with (
            mock.patch("b1ack_memory.llm.httpx", None),
            mock.patch("b1ack_memory.llm.urllib.request.urlopen", return_value=response) as open_url,
        ):
            self.assertTrue(client._post("/chat/completions", {})["ok"])
        self.assertEqual(open_url.call_args.args[0].get_header("Accept"), "application/json")

    def test_deepseek_uses_low_cost_non_thinking_mode(self) -> None:
        client = OpenAICompatibleClient(
            base_url="https://opencode.ai/zen/go/v1",
            model="deepseek-v4-flash",
            api_key="test",
        )
        captured = {}

        def fake_post(path, body):
            captured.update(body)
            return {"choices": [{"message": {"content": '{"ok":true}'}}]}

        client._post = fake_post  # type: ignore[method-assign]
        self.assertTrue(client.chat_json(system="JSON only", user="test").parsed["ok"])
        self.assertEqual(captured["thinking"], {"type": "disabled"})

    def test_empty_reasoning_only_completion_has_actionable_error(self) -> None:
        client = OpenAICompatibleClient(
            base_url="https://opencode.ai/zen/go/v1",
            model="deepseek-v4-flash",
            api_key="test",
        )
        client._post = lambda _path, _body: {  # type: ignore[method-assign]
            "choices": [
                {"message": {"content": "", "reasoning_content": "internal reasoning"}}
            ]
        }
        with self.assertRaisesRegex(LlmError, "reasoning_content"):
            client.chat_json(system="JSON only", user="test")

    def test_malformed_json_is_automatically_repaired_once(self) -> None:
        client = OpenAICompatibleClient(
            base_url="https://opencode.ai/zen/go/v1",
            model="deepseek-v4-flash",
            api_key="test",
            max_output_tokens=8192,
        )
        responses = iter(
            [
                {
                    "choices": [{"message": {"content": '{"candidates":[{"content":"cut'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                },
                {
                    "choices": [{"message": {"content": '{"candidates":[]}'}}],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 5},
                },
            ]
        )
        bodies = []

        def fake_post(_path, body):
            bodies.append(body)
            return next(responses)

        client._post = fake_post  # type: ignore[method-assign]
        result = client.chat_json(system="JSON only", user="test")
        self.assertEqual(result.parsed, {"candidates": []})
        self.assertEqual(result.input_tokens, 40)
        self.assertEqual(result.output_tokens, 25)
        self.assertEqual(len(bodies), 2)
        self.assertIn("Repair", bodies[1]["messages"][0]["content"])
        self.assertEqual(bodies[1]["max_tokens"], 4096)
        self.assertEqual(bodies[1]["thinking"], {"type": "disabled"})


if __name__ == "__main__":
    unittest.main()
