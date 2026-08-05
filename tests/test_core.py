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

        if "extract durable" in system:
            turn_id = json.loads(user)["turns"][0]["id"]
            parsed = {
                "candidates": [
                    {
                        "content": "用户偏好简洁的中文回答",
                        "kind": "preference",
                        "confidence": 0.94,
                        "sensitive": False,
                        "source_turn_id": turn_id,
                    }
                ]
            }
        elif "review personal-memory" in system:
            parsed = {"summary": "一项偏好", "themes": ["表达风格"], "conflicts": []}
        else:
            parsed = {"memories": []}
        return LlmResult(parsed=parsed, raw={"ok": True}, input_tokens=10, output_tokens=5)


class ConflictClient(FakeClient):
    def chat_json(self, *, system: str, user: str) -> LlmResult:
        import json

        if "review personal-memory" not in system:
            return super().chat_json(system=system, user=user)
        payload = json.loads(user)
        parsed = {
            "summary": "发现偏好冲突",
            "themes": ["表达风格"],
            "conflicts": [
                {
                    "candidate_id": payload["candidates"][0]["id"],
                    "memory_id": payload["existing_memories"][0]["id"],
                    "explanation": "新旧偏好不一致",
                }
            ],
        }
        return LlmResult(parsed=parsed, raw=parsed, input_tokens=10, output_tokens=5)


class ApprovingClient(FakeClient):
    def chat_json(self, *, system: str, user: str) -> LlmResult:
        import json

        if "review personal-memory" in system:
            payload = json.loads(user)
            parsed = {
                "summary": "耐久信息已通过复核",
                "themes": [],
                "durable_candidate_ids": [item["id"] for item in payload["candidates"]],
                "noise": [],
                "duplicates": [],
                "conflicts": [],
            }
            return LlmResult(parsed=parsed, raw=parsed, input_tokens=10, output_tokens=5)
        if "compact qualified" in system:
            payload = json.loads(user)
            parsed = {
                "memories": [
                    {"candidate_id": item["id"], "content": item["content"]}
                    for item in payload["candidates"]
                ]
            }
            return LlmResult(parsed=parsed, raw=parsed, input_tokens=10, output_tokens=5)
        return super().chat_json(system=system, user=user)


class ExistingApprovingClient(ApprovingClient):
    def chat_json(self, *, system: str, user: str) -> LlmResult:
        if "extract durable" in system:
            parsed = {"candidates": []}
            return LlmResult(parsed=parsed, raw=parsed, input_tokens=10, output_tokens=5)
        return super().chat_json(system=system, user=user)


class ManyCandidateClient(ExistingApprovingClient):
    def chat_json(self, *, system: str, user: str) -> LlmResult:
        import json

        if "extract durable" in system:
            turn_id = json.loads(user)["turns"][0]["id"]
            parsed = {
                "candidates": [
                    {
                        "content": f"用户的稳定偏好编号 {index}",
                        "kind": "preference",
                        "confidence": 0.9,
                        "sensitive": False,
                        "source_turn_id": turn_id,
                    }
                    for index in range(12)
                ]
            }
            return LlmResult(parsed=parsed, raw=parsed, input_tokens=10, output_tokens=5)
        return super().chat_json(system=system, user=user)


class DuplicateReviewClient(ExistingApprovingClient):
    def chat_json(self, *, system: str, user: str) -> LlmResult:
        import json

        if "review personal-memory" in system:
            payload = json.loads(user)
            candidates = payload["candidates"]
            parsed = {
                "summary": "合并同义候选",
                "themes": [],
                "durable_candidate_ids": [candidates[0]["id"]],
                "noise": [],
                "duplicates": [
                    {
                        "candidate_id": candidates[1]["id"],
                        "canonical_candidate_id": candidates[0]["id"],
                        "explanation": "表达不同但含义相同",
                    }
                ],
                "conflicts": [],
            }
            return LlmResult(parsed=parsed, raw=parsed, input_tokens=10, output_tokens=5)
        return super().chat_json(system=system, user=user)


class NoiseReviewClient(ExistingApprovingClient):
    def chat_json(self, *, system: str, user: str) -> LlmResult:
        import json

        if "review personal-memory" in system:
            candidate_id = json.loads(user)["candidates"][0]["id"]
            parsed = {
                "summary": "发现一次性噪声",
                "themes": [],
                "durable_candidate_ids": [],
                "noise": [{"candidate_id": candidate_id, "explanation": "一次性任务进度"}],
                "duplicates": [],
                "conflicts": [],
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
        memory_id = self.service.promote_candidate(candidate_id)["id"]
        old_backup = self.service.create_backup().name
        self.service.trash_memory(memory_id)
        result = self.service.purge_memory(memory_id)
        self.assertEqual(result["removed"]["memories"], 1)
        self.assertEqual(self.service.list_memories(), [])
        self.assertEqual(self.service.list_candidates(status="promoted"), [])
        self.assertFalse(any("PURGE_SENTINEL" in str(item) for item in self.service.model_calls()))
        self.assertFalse(any(item["name"] == old_backup for item in self.service.list_backups()))
        with self.service.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM raw_turns").fetchone()[0], 0)

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
        self.assertEqual(candidate["conflict_memory_id"], existing_id)
        self.assertEqual(candidate["conflict_reason"], "新旧偏好不一致")

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

    def test_utility_lane_auto_promotes_after_two_distinct_injections(self) -> None:
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
        self.assertEqual(outcome.promoted_count, 1)
        self.assertEqual(self.service.list_memories()[0]["content"], candidate.content)

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
        self.assertEqual(outcome.merged_count, 1)
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
        self.assertTrue(result["clean_backup"].endswith("-post-purge.db"))

    def test_active_long_term_memory_cannot_be_hard_deleted(self) -> None:
        memory_id = self.service.remember("需要先回收的长期记忆")["memory"]["id"]
        with self.assertRaisesRegex(ValueError, "trash"):
            self.service.purge_memory(memory_id)

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
            self.assertEqual(store.masked_status("llm_api_key")["masked"], "••••3456")


class ClientTests(unittest.TestCase):
    def test_dream_prompts_bound_structured_output(self) -> None:
        self.assertIn("at most 8 candidates", LIGHT_SYSTEM)
        self.assertIn("under 240 characters", LIGHT_SYSTEM)
        self.assertIn("durable_candidate_ids", REM_SYSTEM)
        self.assertIn("duplicates", REM_SYSTEM)

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
