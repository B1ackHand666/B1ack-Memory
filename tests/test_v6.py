from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from b1ack_memory.db import MemoryDatabase, content_hash
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

    def test_sensitive_recent_source_forces_deep_review_even_if_model_says_safe(self) -> None:
        signal, _ = self.service.db.upsert_recent_signal(
            "用户的敏感医疗信息", sensitive=True, retention_days=30
        )
        reflection = self.service.db.create_rem_reflection(
            "用户有一个稳定偏好。", reflection_type="theme", confidence=0.95,
            signal_ids=[signal["id"]], daily_memory_ids=[], sensitive=False,
        )
        self.assertTrue(reflection["sensitive"])
        result = self.service.db.apply_deep_integrations(
            [{
                "reflection_id": reflection["id"], "action": "create",
                "content": "不应自动晋升的长期记忆", "kind": "fact", "confidence": 0.95,
            }],
            dream_run_id="missing-run",
            timezone_name="Asia/Shanghai",
        )
        self.assertEqual(result["create"], 0)
        self.assertEqual(result["review"], 1)
        self.assertEqual(self.service.db.list_memories(), [])
        self.assertEqual(self.service.db.list_rem_reflections(status="review")[0]["status"], "review")

    def test_deep_evidence_dates_follow_configured_timezone(self) -> None:
        same_day_signal, _ = self.service.db.upsert_recent_signal(
            "同一本地日期", retention_days=30
        )
        different_day_signal, _ = self.service.db.upsert_recent_signal(
            "不同本地日期", retention_days=30
        )
        reflection_same = self.service.db.create_rem_reflection(
            "同一本地日期的反思", reflection_type="theme", confidence=0.95,
            signal_ids=[same_day_signal["id"]], daily_memory_ids=[],
        )
        reflection_different = self.service.db.create_rem_reflection(
            "不同本地日期的反思", reflection_type="theme", confidence=0.95,
            signal_ids=[different_day_signal["id"]], daily_memory_ids=[],
        )
        with self.service.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE recent_signals SET expires_at='2099-01-01T00:00:00+00:00' "
                "WHERE id IN (?,?)", (same_day_signal["id"], different_day_signal["id"]),
            )
            conn.executemany(
                "INSERT INTO recent_evidence(signal_id,excerpt,role,observed_at) VALUES(?,?,?,?)",
                [
                    (same_day_signal["id"], "a", "user", "2026-08-20T16:30:00+00:00"),
                    (same_day_signal["id"], "b", "user", "2026-08-21T00:30:00+00:00"),
                    (different_day_signal["id"], "c", "user", "2026-08-20T15:30:00+00:00"),
                    (different_day_signal["id"], "d", "user", "2026-08-21T00:30:00+00:00"),
                ],
            )
        same_result = self.service.db.apply_deep_integrations(
            [{
                "reflection_id": reflection_same["id"], "action": "create",
                "content": "同一本地日期不应晋升", "kind": "fact", "confidence": 0.95,
            }],
            dream_run_id="missing-run", timezone_name="Asia/Shanghai",
        )
        different_result = self.service.db.apply_deep_integrations(
            [{
                "reflection_id": reflection_different["id"], "action": "create",
                "content": "不同本地日期可以晋升", "kind": "fact", "confidence": 0.95,
            }],
            dream_run_id="missing-run", timezone_name="Asia/Shanghai",
        )
        self.assertEqual(same_result["defer"], 1)
        self.assertEqual(different_result["create"], 1)
        self.assertEqual(len(self.service.db.list_memories()), 1)

    def test_expired_or_deleted_recent_source_cannot_deep_integrate(self) -> None:
        signal, _ = self.service.db.upsert_recent_signal("即将过期的来源", retention_days=30)
        reflection = self.service.db.create_rem_reflection(
            "过期来源反思", reflection_type="theme", confidence=0.95,
            signal_ids=[signal["id"]], daily_memory_ids=[],
        )
        with self.service.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE recent_signals SET status='expired',expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (signal["id"],),
            )
        result = self.service.db.apply_deep_integrations(
            [{
                "reflection_id": reflection["id"], "action": "create",
                "content": "不应从过期来源创建", "kind": "fact", "confidence": 0.95,
            }],
            dream_run_id="missing-run", timezone_name="Asia/Shanghai",
        )
        self.assertEqual(result["discarded"], 1)
        self.assertEqual(self.service.db.list_memories(), [])
        self.assertEqual(self.service.db.list_rem_reflections(status="active"), [])

    def test_recent_cascade_delete_cleans_derived_records_and_keeps_memory(self) -> None:
        raw_id = self.service.db.add_raw_turn("session", "隐私来源内容", "assistant", redacted=False)
        signal, _ = self.service.db.upsert_recent_signal(
            "隐私来源内容", raw_turn_id=raw_id, retention_days=30
        )
        daily = self.service.db.add_daily_memory(
            "隐私来源内容", raw_turn_id=raw_id, signal_id=signal["id"], retention_days=30,
        )
        reflection = self.service.db.create_rem_reflection(
            "关联反思", reflection_type="theme", confidence=0.9,
            signal_ids=[signal["id"]], daily_memory_ids=[daily["id"]],
        )
        memory = self.service.db.add_memory("长期结论保留", kind="fact")
        now = datetime.now(UTC).isoformat()
        work_id = str(uuid.uuid4())
        call_id = str(uuid.uuid4())
        admission_id = self.service.db.add_admission_decision(
            disposition="observe", content="隐私来源内容", reason="测试", raw_turn_id=raw_id,
        )
        with self.service.db.transaction(immediate=True) as conn:
            conn.executemany(
                "INSERT INTO evidence(memory_id,recent_signal_id,daily_memory_id,raw_turn_id,excerpt,role,observed_at) "
                "VALUES(?,?,?,?,?,?,?)",
                [
                    (memory.id, signal["id"], None, raw_id, "隐私来源内容", "recent", now),
                    (memory.id, None, daily["id"], raw_id, "隐私来源内容", "daily", now),
                ],
            )
            conn.execute(
                "INSERT INTO work_items(id,item_type,content,content_hash,status,confirmed,confidence,raw_turn_id,"
                "admission_decision_id,evidence_quote,source,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (work_id, "open_question", "隐私工作项", content_hash("隐私工作项"), "suggested", 0, 0.5,
                 raw_id, admission_id, "隐私来源内容", "test", now, now),
            )
            conn.execute(
                "INSERT INTO model_calls(id,dream_run_id,phase,request_json,response_json,model,input_tokens,output_tokens,error,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (call_id, None, "rem", "隐私来源内容", "隐私来源内容", "test", 1, 1, None, now),
            )
            conn.execute(
                "INSERT INTO model_call_records(call_id,record_type,record_id) VALUES(?,?,?)",
                (call_id, "recent_signal", signal["id"]),
            )
            conn.execute(
                "INSERT INTO recall_events(record_id,source,query_text,query_hash,final_score,created_at) "
                "VALUES(?,?,?,?,?,?)", (signal["id"], "recent_signal", "隐私", "hash", 1.0, now),
            )
            conn.execute(
                "INSERT INTO embeddings(record_id,source,model_fingerprint,vector_json,updated_at) "
                "VALUES(?,?,?,?,?)", (signal["id"], "recent_signal", "test", "[]", now),
            )
            conn.execute(
                "INSERT INTO search_fts(record_id,source,pool,content,search_text) VALUES(?,?,?,?,?)",
                (signal["id"], "recent_signal", "recent", "隐私来源内容", "隐私来源内容"),
            )
            conn.execute(
                "INSERT INTO projection_jobs(id,projection_type,target_id,revision,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), "profile", signal["id"], "revision", "pending", now, now),
            )
            conn.execute(
                "INSERT INTO summary_versions(id,scope,content,source_ids_json,change_reason,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), "profile", "隐私摘要", json.dumps([signal["id"]]), "test", now),
            )
        result = self.service.delete_recent_record("recent_signal", signal["id"], permanent=True)
        removed = result["removed"]
        self.assertEqual(removed["recent_signals"], 1)
        self.assertEqual(removed["daily_memories"], 1)
        self.assertEqual(removed["raw_turns"], 1)
        self.assertGreaterEqual(removed["work_items"], 1)
        self.assertGreaterEqual(removed["admission_decisions"], 1)
        self.assertIsNotNone(self.service.db.get_memory(memory.id))
        self.assertEqual(self.service.db.list_rem_reflections(status="active"), [])
        with self.service.db.connect() as conn:
            for table, key, value in [
                ("recent_signals", "id", signal["id"]), ("daily_memories", "id", daily["id"]),
                ("raw_turns", "id", raw_id), ("work_items", "id", work_id),
                ("admission_decisions", "id", admission_id), ("model_calls", "id", call_id),
            ]:
                self.assertIsNone(conn.execute(f"SELECT 1 FROM {table} WHERE {key}=?", (value,)).fetchone())
            self.assertEqual(conn.execute("SELECT count(*) FROM search_fts WHERE record_id=?", (signal["id"],)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM embeddings WHERE record_id=?", (signal["id"],)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM projection_jobs WHERE target_id=?", (signal["id"],)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM summary_versions WHERE source_ids_json LIKE ?", (f'%"{signal["id"]}"%',)).fetchone()[0], 0)
        self.assertTrue(any(item["issue_type"] == "evidence_affected" for item in self.service.list_reviews()))

    def test_shared_raw_turn_is_retained_for_other_recent_reference(self) -> None:
        raw_id = self.service.db.add_raw_turn("session", "共享原始内容", "assistant", redacted=False)
        first, _ = self.service.db.upsert_recent_signal(
            "第一条共享内容", raw_turn_id=raw_id, retention_days=30
        )
        second, _ = self.service.db.upsert_recent_signal(
            "第二条共享内容", raw_turn_id=raw_id, retention_days=30
        )
        admission_id = self.service.db.add_admission_decision(
            disposition="observe", content="第二条共享内容", reason="仍有效", raw_turn_id=raw_id,
        )
        result = self.service.delete_recent_record("recent_signal", first["id"], permanent=True)
        self.assertEqual(result["removed"]["raw_turns"], 0)
        with self.service.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM recent_signals WHERE id=?", (first["id"],)).fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM recent_signals WHERE id=?", (second["id"],)).fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM raw_turns WHERE id=?", (raw_id,)).fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM admission_decisions WHERE id=?", (admission_id,)).fetchone())


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
