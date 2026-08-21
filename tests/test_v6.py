from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from b1ack_memory.db import MemoryDatabase
from b1ack_memory.dream import DreamEngine
from b1ack_memory.llm import LlmResult
from b1ack_memory.provider import B1ackMemoryProvider
from b1ack_memory.service import MemoryService
from b1ack_memory.web import create_app


class V6Client:
    configured = True
    model = "v6-test"

    def chat_json(self, *, system: str, user: str) -> LlmResult:
        payload = json.loads(user)
        if "Light stage" in system:
            parsed = {"decisions": [
                {
                    "disposition": "create_signal",
                    "content": "用户偏好简洁中文回答",
                    "kind": "preference",
                    "confidence": 0.92,
                    "sensitive": False,
                    "source_turn_id": turn["id"],
                    "evidence_quote": "我偏好简洁中文回答",
                    "explanation": "用户的明确偏好",
                }
                for turn in payload["turns"]
            ]}
        elif "operate REM" in system:
            parsed = {"summary": "跨日重复偏好", "reflections": [{
                "content": "用户稳定偏好简洁中文回答。",
                "reflection_type": "repetition",
                "confidence": 0.91,
                "sensitive": False,
                "signal_ids": [item["id"] for item in payload["recent_signals"]],
                "daily_memory_ids": [item["id"] for item in payload["daily_memories"]],
                "explanation": "在两个日期重复出现",
            }]}
        elif "Deep stage" in system:
            parsed = {"integrations": [{
                "reflection_id": item["id"],
                "action": "create",
                "content": "用户偏好简洁中文回答。",
                "kind": "preference",
                "confidence": 0.91,
                "reason": "跨日且有用户原话支持",
            } for item in payload["reflections"]]}
        else:
            parsed = {"issues": []}
        return LlmResult(parsed=parsed, raw=parsed, input_tokens=4, output_tokens=2)


class DreamV6Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = MemoryService(Path(self.temp.name))

    def tearDown(self) -> None:
        self.service.shutdown(timeout=0.1)
        self.temp.cleanup()

    def test_v7_upgrade_backs_up_and_freezes_candidates_as_history(self) -> None:
        path = Path(self.temp.name) / "upgrade.db"
        old = MemoryDatabase(path)
        candidate = old.upsert_candidate(
            "旧候选", kind="fact", confidence=0.9, sensitive=False,
            raw_turn_id=None, excerpt="旧候选",
        )
        with old.transaction(immediate=True) as conn:
            conn.execute("UPDATE schema_meta SET version=7")
        upgraded = MemoryDatabase(path)
        self.assertEqual(upgraded.schema_version(), 8)
        self.assertEqual(upgraded.get_candidate(candidate.id).admission_state, "legacy_history")
        self.assertTrue(any(path.parent.joinpath("backups").glob("*-pre-schema-v8.db")))

    def test_recent_light_rem_deep_flow_and_prefetch_isolation(self) -> None:
        first = self.service.db.add_raw_turn("s", "我偏好简洁中文回答", "ok", redacted=False)
        second = self.service.db.add_raw_turn("s", "我偏好简洁中文回答", "ok", redacted=False)
        with self.service.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE raw_turns SET observed_at=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(days=2)).isoformat(), first),
            )
        outcome = DreamEngine(self.service.db, V6Client()).run()
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.recent_count, 1)
        self.assertEqual(outcome.daily_count, 2)
        self.assertEqual(outcome.reflection_count, 1)
        self.assertEqual(outcome.promoted_count, 1)
        self.assertEqual(len(self.service.list_recent_signals()), 1)
        self.assertEqual(len(self.service.list_daily_memories()), 2)
        self.assertEqual(len(self.service.list_memories()), 1)
        self.assertIn("用户偏好简洁中文回答", self.service.format_prefetch("中文回答"))

    def test_recent_records_are_not_injected_before_deep(self) -> None:
        self.service.db.upsert_recent_signal("只存在于近期池", retention_days=14)
        self.service.rebuild_derived()
        self.assertEqual(self.service.format_prefetch("近期池"), "")
        hits = self.service.search("近期池")
        self.assertEqual(hits[0].source, "recent_signal")

    def test_light_merge_and_retention_expiry_converge_without_reviews(self) -> None:
        first, _ = self.service.db.upsert_recent_signal("用户偏好简洁回答")
        second, _ = self.service.db.upsert_recent_signal("用户喜欢简明回答")
        merged = self.service.db.merge_recent_signals(first["id"], second["id"])
        self.assertEqual(merged["strength"], 2)
        self.assertEqual(self.service.db.list_recent_signals(status="merged")[0]["id"], second["id"])
        with self.service.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE recent_signals SET last_seen_at=?,expires_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", first["id"]),
            )
        expired = self.service.db.expire_recent_layer(recent_days=14, daily_days=30)
        self.assertEqual(expired["recent_signals"], 1)
        self.assertEqual(self.service.list_reviews(), [])

    def test_dry_run_keeps_recent_and_long_term_storage_unchanged(self) -> None:
        first = self.service.db.add_raw_turn("s", "我偏好简洁中文回答", "ok", redacted=False)
        second = self.service.db.add_raw_turn("s", "我偏好简洁中文回答", "ok", redacted=False)
        with self.service.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE raw_turns SET observed_at=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(days=2)).isoformat(), first),
            )
        before = self.service.db.path.read_bytes()
        outcome = DreamEngine(self.service.db, V6Client()).run(dry_run=True)
        self.assertEqual(outcome.status, "dry_run")
        self.assertEqual(before, self.service.db.path.read_bytes())

    def test_permanent_recent_delete_preserves_long_memory_and_creates_impact_review(self) -> None:
        raw_id = self.service.db.add_raw_turn("s", "用户使用茶", "ok", redacted=False)
        signal, _ = self.service.db.upsert_recent_signal(
            "用户使用茶", raw_turn_id=raw_id, retention_days=14
        )
        daily = self.service.db.add_daily_memory(
            "用户使用茶", signal_id=signal["id"], raw_turn_id=raw_id
        )
        reflection = self.service.db.create_rem_reflection(
            "用户稳定使用茶。", reflection_type="theme", confidence=0.9,
            signal_ids=[signal["id"]], daily_memory_ids=[daily["id"]],
        )
        # Create a durable conclusion directly so this deletion test isolates
        # evidence impact rather than the two-date Deep admission gate.
        memory = self.service.db.add_memory("用户稳定使用茶。", kind="preference")
        with self.service.db.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO evidence(memory_id,recent_signal_id,excerpt,role,observed_at) VALUES(?,?,?,'recent',?)",
                (memory.id, signal["id"], "用户使用茶", datetime.now(UTC).isoformat()),
            )
        result = self.service.delete_recent_record("recent_signal", signal["id"], permanent=True)
        self.assertEqual(result["removed"]["purged"], 1)
        self.assertIsNotNone(self.service.db.get_memory(memory.id))
        self.assertTrue(any(item["issue_type"] == "evidence_affected" for item in self.service.list_reviews()))
        self.assertEqual(self.service.db.list_rem_reflections(status="active"), [])
        with self.service.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM raw_turns WHERE id=?", (raw_id,)).fetchone())


class HermesNativeMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = MemoryService(self.root / "b1ack")
        self.hermes_home = self.root / "hermes-profile"

    def tearDown(self) -> None:
        self.service.shutdown(timeout=0.1)
        self.temp.cleanup()

    def test_native_files_are_profile_scoped_conflict_checked_and_database_independent(self) -> None:
        before = self.service.db.path.read_bytes()
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.hermes_home)}, clear=False):
            loaded = self.service.get_hermes_native_memory("user")
            self.assertFalse(loaded["exists"])
            self.assertEqual(loaded["limit"], 1375)
            saved = self.service.save_hermes_native_memory(
                "user", "只给 Hermes 的说明", expected_hash=loaded["content_hash"]
            )
            self.assertEqual(saved["characters"], len("只给 Hermes 的说明"))
            with self.assertRaisesRegex(ValueError, "changed externally"):
                self.service.save_hermes_native_memory("user", "覆盖", expected_hash=loaded["content_hash"])
            with self.assertRaisesRegex(ValueError, "2200"):
                self.service.save_hermes_native_memory(
                    "memory", "x" * 2201,
                    expected_hash=self.service.get_hermes_native_memory("memory")["content_hash"],
                )
        self.assertEqual(before, self.service.db.path.read_bytes())

    def test_native_file_api_requires_current_profile_and_mutation_authorization(self) -> None:
        app = create_app(self.service)
        client = TestClient(app)
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.hermes_home)}, clear=False):
            page = client.get("/api/ui/")
            token = page.cookies.get("b1ack_memory_session")
            loaded = client.get("/api/hermes-native-memory/memory", cookies={"b1ack_memory_session": token})
            self.assertEqual(loaded.status_code, 200)
            denied = client.put("/api/hermes-native-memory/memory", json={"content": "x", "expected_hash": loaded.json()["content_hash"]})
            self.assertEqual(denied.status_code, 403)
            saved = client.put(
                "/api/hermes-native-memory/memory",
                json={"content": "x", "expected_hash": loaded.json()["content_hash"]},
                headers={"origin": "http://testserver"}, cookies={"b1ack_memory_session": token},
            )
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json()["content"], "x")

    def test_hermes_builtin_write_hook_is_an_explicit_noop(self) -> None:
        provider = B1ackMemoryProvider(self.service)
        provider.on_memory_write("replace", "MEMORY.md", "Hermes 自动写入", {})
        self.assertEqual(self.service.list_memories(), [])
        self.assertEqual(self.service.list_recent_signals(), [])
