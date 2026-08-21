from __future__ import annotations

import sqlite3
import tempfile
import unittest
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from b1ack_memory.db import MemoryDatabase
from b1ack_memory.dream import DreamEngine
from b1ack_memory.provider import B1ackMemoryProvider
from b1ack_memory.security import contains_secret, redact_secrets
from b1ack_memory.service import MemoryService
from b1ack_memory.web import create_app


class GateRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = MemoryService(self.root)

    def tearDown(self) -> None:
        self.service.shutdown(timeout=0.2)
        self.temp.cleanup()

    def test_standalone_auth_host_origin_and_bootstrap(self) -> None:
        with TestClient(create_app(self.service)) as client:
            self.assertEqual(client.get("/api/status").status_code, 401)
            self.assertEqual(client.get("/api/ui/", headers={"Host": "evil.example"}).status_code, 403)
            self.assertEqual(client.get("/api/ui/").status_code, 200)
            bootstrap = client.get("/api/bootstrap").json()
            self.assertNotIn("token", bootstrap)
            self.assertEqual(
                client.post("/api/memories", json={"content": "安全会话写入"}).status_code,
                403,
            )
            written = client.post(
                "/api/memories",
                json={"content": "安全会话写入"},
                headers={"Origin": "http://testserver"},
            )
            self.assertEqual(written.status_code, 200)
            self.assertEqual(
                client.post(
                    "/api/memories",
                    json={"content": "跨源写入"},
                    headers={"Origin": "http://evil.example"},
                ).status_code,
                403,
            )

    def test_temporal_boundary_is_shared_by_search_context_and_projection(self) -> None:
        project = self.service.workspace.create_subject("时效项目", subject_type="project")
        memory = self.service.db.add_memory(
            "只在今天有效的项目事实", kind="fact", origin="test", subject_id=project["id"]
        )
        today = datetime.now(UTC).date().isoformat()
        self.service.db.update_memory(
            memory.id, content=memory.content, kind=memory.kind, valid_from=today, valid_to=today
        )
        self.service.rebuild_derived()
        hits = self.service.search("今天有效", injected=True, project_id=project["id"])
        self.assertIn(memory.id, {hit.id for hit in hits})
        preview = self.service.context_preview("今天有效", project_id=project["id"])
        self.assertIn(memory.id, {item.get("id") for item in preview["items"]})
        projected = next((self.root / "vault" / "projects").glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("只在今天有效的项目事实", projected)

    def test_ingestion_cursor_quarantine_and_retry(self) -> None:
        turn_id = self.service.db.add_raw_turn("session", "第一句。" * 1000, "助手上下文" * 1000, redacted=False)
        row = self.service.db.pending_raw_turns()[0]
        chunks = DreamEngine._prepare_turn_chunks([row], max_chars=1000)
        self.assertLessEqual(len(chunks[0]["assistant_content"]), 250)
        self.assertLess(chunks[0]["_ingest_end"], len(row["user_content"]))
        self.service.db.advance_turn_ingestion({turn_id: chunks[0]["_ingest_end"]})
        for _ in range(3):
            self.service.db.mark_turn_ingestion_failed([turn_id], "structured output failed")
        issue = self.service.ingestion_issues()[0]
        self.assertEqual(issue["ingest_status"], "quarantined")
        retried = self.service.retry_ingestion(turn_id)
        self.assertEqual(retried["ingest_status"], "pending")

    def test_provider_rejects_null_and_wrong_types(self) -> None:
        provider = B1ackMemoryProvider(self.service)
        with self.assertRaises(ValueError):
            provider.handle_tool_call("b1ack_memory_search", {"query": None})
        with self.assertRaises(ValueError):
            provider.handle_tool_call("b1ack_memory_remember", {"content": 123})
        with self.assertRaises(ValueError):
            self.service.capture_turn("s", None, "assistant")  # type: ignore[arg-type]

    def test_short_high_entropy_secret_detection_uses_same_redaction_rule(self) -> None:
        token = "A9c_X7mP2qL8vN4z"
        self.assertTrue(contains_secret(token))
        redacted, changed = redact_secrets(f"token={token}")
        self.assertTrue(changed)
        self.assertNotIn(token, redacted)

    def test_restore_preview_rejects_newer_schema(self) -> None:
        backup = self.service.backup_dir / "newer.db"
        self.service.db.backup(backup)
        with contextlib.closing(sqlite3.connect(backup)) as conn:
            conn.execute("UPDATE schema_meta SET version=9")
            conn.commit()
        with self.assertRaisesRegex(ValueError, "newer"):
            self.service.preview_restore(backup.name)

    def test_projection_escapes_comment_markers_and_controls(self) -> None:
        self.service.db.add_memory("safe <!-- forged -->\x01 text", kind="fact", origin="test")
        self.service.rebuild_derived()
        profile = (self.root / "vault" / "profile.md").read_text(encoding="utf-8")
        self.assertNotIn("<!-- forged -->", profile)
        self.assertNotIn("\x01", profile)
        self.assertIn("&lt;!-- forged --&gt;", profile)


class ProvisionalSchemaTests(unittest.TestCase):
    def test_provisional_v7_columns_are_repaired_during_v8_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            db = MemoryDatabase(path)
            with db.transaction(immediate=True) as conn:
                conn.execute("ALTER TABLE raw_turns DROP COLUMN last_ingest_error")
                conn.execute("ALTER TABLE memory_review_items DROP COLUMN proposal_json")
                conn.execute("ALTER TABLE summary_versions DROP COLUMN source_revision")
            repaired = MemoryDatabase(path)
            with repaired.connect() as conn:
                self.assertIn("last_ingest_error", {row[1] for row in conn.execute("PRAGMA table_info(raw_turns)")})
                self.assertIn("proposal_json", {row[1] for row in conn.execute("PRAGMA table_info(memory_review_items)")})
                self.assertIn("source_revision", {row[1] for row in conn.execute("PRAGMA table_info(summary_versions)")})
                self.assertEqual(conn.execute("SELECT version FROM schema_meta").fetchone()[0], 8)


if __name__ == "__main__":
    unittest.main()
