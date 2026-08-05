from __future__ import annotations

import tempfile
import unittest
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
        self.assertIn("at most 20 candidates", LIGHT_SYSTEM)
        self.assertIn("under 240 characters", LIGHT_SYSTEM)
        self.assertIn("at most 10 themes and 20 conflicts", REM_SYSTEM)

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
