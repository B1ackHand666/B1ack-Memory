from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from b1ack_memory.db import MemoryDatabase
from b1ack_memory.llm import LlmError, LlmResult
from b1ack_memory.service import MemoryService
from b1ack_memory.web import create_app


class SummaryClient:
    configured = True
    model = "summary-test"

    def __init__(self, *, unknown: bool = False):
        self.unknown = unknown

    def chat_json(self, *, system: str, user: str) -> LlmResult:
        payload = json.loads(user)
        source_ids = [item["id"] for item in payload["sources"]]
        parsed = {
            "summary": "项目当前采用保守的本地优先方案。",
            "change_reason": "工作项发生变化",
            "statements": [
                {
                    "text": "项目当前采用保守的本地优先方案。",
                    "source_ids": ["unknown"] if self.unknown else source_ids,
                }
            ],
        }
        return LlmResult(parsed=parsed, raw=parsed, input_tokens=10, output_tokens=8)


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = MemoryService(self.root)

    def tearDown(self) -> None:
        self.service.shutdown(timeout=0.1)
        self.temp.cleanup()

    def test_schema_v7_and_rebuildable_projection(self) -> None:
        project = self.service.create_project({"name": "记忆治理", "aliases": ["治理项目"]})
        self.assertEqual(self.service.db.schema_version(), 7)
        self.assertTrue((self.root / "vault" / "profile.md").is_file())
        self.assertTrue((self.root / "vault" / "projects" / "记忆治理.md").is_file() is False)
        project_page = self.root / "vault" / "projects" / f"{project['slug']}.md"
        self.assertTrue(project_page.is_file())
        project_page.unlink()
        result = self.service.workspace.rebuild_projections()
        self.assertTrue(result["ok"])
        self.assertTrue(project_page.is_file())
        self.assertTrue(self.service.workspace.storage_health()["ok"])

    def test_project_detection_priority_and_ambiguity_boundary(self) -> None:
        first = self.service.create_project(
            {"name": "Alpha", "aliases": ["共同项目"], "workspace_aliases": ["D:/alpha"]}
        )
        second = self.service.create_project({"name": "Beta", "aliases": ["共同项目"]})
        explicit = self.service.workspace.identify_project("共同项目", explicit_project_id=first["id"])
        self.assertEqual(explicit["reason"], "explicit_project_id")
        workspace = self.service.workspace.identify_project("无项目名", workspace="D:/alpha")
        self.assertEqual(workspace["project"]["id"], first["id"])
        ambiguous = self.service.workspace.identify_project("继续共同项目")
        self.assertTrue(ambiguous["ambiguous"])
        self.assertIsNone(ambiguous["project"])
        self.service.set_session_project("session-a", second["id"])
        session = self.service.workspace.identify_project("无项目名", session_id="session-a")
        self.assertEqual(session["project"]["id"], second["id"])

    def test_work_items_require_grounding_and_proposals_never_inject(self) -> None:
        project = self.service.create_project({"name": "连续性项目"})
        raw = self.service.capture_turn("s1", "我们已经决定继续使用 SQLite。", "好的")
        decision = self.service.workspace.create_work_item(
            "继续使用 SQLite",
            item_type="decision",
            confidence=0.96,
            subject_id=project["id"],
            raw_turn_id=raw,
            evidence_quote="我们已经决定继续使用 SQLite。",
            confirmed=True,
        )
        proposal = self.service.workspace.create_work_item(
            "也许改用图数据库",
            item_type="proposal",
            confidence=0.97,
            subject_id=project["id"],
            raw_turn_id=raw,
            evidence_quote="我们已经决定继续使用 SQLite。",
            confirmed=True,
        )
        ungrounded = self.service.workspace.create_work_item(
            "助手推测会改数据库",
            item_type="decision",
            confidence=0.99,
            subject_id=project["id"],
            confirmed=True,
        )
        self.assertEqual(decision["status"], "active")
        self.assertEqual(proposal["status"], "suggested")
        self.assertFalse(ungrounded["confirmed"])
        preview = self.service.context_preview("SQLite", project_id=project["id"])
        injected = {item["id"] for item in preview["items"]}
        self.assertIn(decision["id"], injected)
        self.assertNotIn(proposal["id"], injected)
        self.assertNotIn(ungrounded["id"], injected)

    def test_raw_retention_preserves_work_item_but_privacy_purge_removes_it(self) -> None:
        project = self.service.create_project({"name": "证据生命周期"})
        raw = self.service.capture_turn("s1", "项目当前处于联调阶段。", "收到")
        item = self.service.workspace.create_work_item(
            "项目处于联调阶段",
            item_type="current_state",
            confidence=0.96,
            subject_id=project["id"],
            raw_turn_id=raw,
            evidence_quote="项目当前处于联调阶段。",
            confirmed=True,
        )
        with self.service.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE raw_turns SET observed_at=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(days=60)).isoformat(), raw),
            )
        self.service.db.retention_cleanup(30, 30)
        retained = self.service.workspace.get_work_item(item["id"])
        self.assertIsNone(retained["raw_turn_id"])

        raw_private = self.service.capture_turn("s2", "需要永久删除这条工作证据。", "收到")
        candidate = self.service.db.upsert_candidate(
            "需要永久删除这条工作证据",
            kind="fact",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=raw_private,
            excerpt="需要永久删除这条工作证据。",
        )
        private_item = self.service.workspace.create_work_item(
            "需要永久删除这条工作证据",
            item_type="current_state",
            confidence=0.95,
            subject_id=project["id"],
            raw_turn_id=raw_private,
            evidence_quote="需要永久删除这条工作证据。",
            confirmed=True,
        )
        removed = self.service.db.purge_candidate(candidate.id, privacy=True)
        self.assertEqual(removed["work_items"], 1)
        with self.assertRaises(KeyError):
            self.service.workspace.get_work_item(private_item["id"])

    def test_paused_project_and_project_scoped_memory_do_not_cross_inject(self) -> None:
        project = self.service.create_project({"name": "隔离项目"})
        stored = self.service.remember("隔离项目使用独立发布流程", project_id=project["id"])
        memory_id = stored["memory"]["id"]
        self.assertIn(memory_id, {item["id"] for item in self.service.context_preview("发布流程", project_id=project["id"])["items"]})
        self.assertNotIn(memory_id, {item["id"] for item in self.service.context_preview("发布流程")["items"]})
        self.service.update_project(project["id"], {"status": "paused"})
        preview = self.service.context_preview("隔离项目发布流程")
        self.assertIsNone(preview["project_detection"]["project"])

    def test_temporal_memory_only_recalls_current_valid_version(self) -> None:
        current = self.service.db.add_memory("当前发布通道是 stable").to_dict()
        old = self.service.db.add_memory("旧发布通道是 beta").to_dict()
        self.service.db.update_memory_temporal(old["id"], temporal_status="historical")
        future = self.service.db.add_memory("未来发布通道是 edge").to_dict()
        self.service.db.update_memory_temporal(
            future["id"], valid_from=(datetime.now(UTC) + timedelta(days=2)).isoformat()
        )
        self.service.rebuild_derived()
        ids = {item.id for item in self.service.search("发布通道", injected=True)}
        self.assertIn(current["id"], ids)
        self.assertNotIn(old["id"], ids)
        self.assertNotIn(future["id"], ids)

    def test_supersede_and_merge_preserve_historical_chain_and_project_links(self) -> None:
        project = self.service.create_project({"name": "版本项目"})
        old = self.service.db.add_memory("旧的项目事实").to_dict()
        new = self.service.db.add_memory("新的项目事实").to_dict()
        self.service.workspace.link_subject(project["id"], "memory", old["id"])
        self.service.db.supersede_memory(new["id"], old["id"])
        historical = self.service.db.get_memory(old["id"])
        self.assertEqual(historical.temporal_status, "historical")
        self.assertIsNotNone(historical.valid_to)
        self.assertEqual(self.service.db.get_memory(new["id"]).supersedes_id, old["id"])

        duplicate = self.service.db.add_memory("新的项目事实（重复表达）").to_dict()
        self.service.workspace.link_subject(project["id"], "memory", duplicate["id"])
        self.service.db.merge_memories(new["id"], duplicate["id"])
        merged = self.service.db.get_memory(duplicate["id"])
        self.assertEqual(merged.temporal_status, "historical")
        with self.service.db.connect() as conn:
            self.assertTrue(
                conn.execute(
                    "SELECT 1 FROM subject_links WHERE subject_id=? AND object_type='memory' AND object_id=?",
                    (project["id"], new["id"]),
                ).fetchone()
            )

    def test_grounded_summary_versions_override_and_rollback(self) -> None:
        project = self.service.create_project({"name": "摘要项目"})
        raw = self.service.capture_turn("s", "项目决定本地优先。", "收到")
        self.service.workspace.create_work_item(
            "项目采用本地优先方案",
            item_type="decision",
            confidence=0.95,
            subject_id=project["id"],
            raw_turn_id=raw,
            evidence_quote="项目决定本地优先。",
            confirmed=True,
        )
        self.service.workspace.client_factory = lambda: SummaryClient()
        first = self.service.regenerate_summary("project", project["id"])
        manual = self.service.override_summary("project", project["id"], "手工项目摘要")
        self.assertEqual(manual["mode"], "manual_override")
        with self.assertRaisesRegex(ValueError, "manual override"):
            self.service.regenerate_summary("project", project["id"])
        rolled = self.service.rollback_summary("project", project["id"], first["id"])
        self.assertEqual(rolled["mode"], "manual_override")
        self.service.workspace.client_factory = lambda: SummaryClient(unknown=True)
        with self.assertRaises(LlmError):
            self.service.regenerate_summary("project", project["id"])

    def test_zip_backup_manifest_and_restore_preview(self) -> None:
        self.service.remember("备份中需要保留的长期事实")
        backup = self.service.create_backup()
        self.assertEqual(backup.suffix, ".zip")
        with zipfile.ZipFile(backup) as archive:
            self.assertIn("memory.db", archive.namelist())
            self.assertIn("manifest.json", archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["schema_version"], 7)
            self.assertNotIn("secrets.json", archive.namelist())
        preview = self.service.preview_restore(backup.name)
        self.assertEqual(preview["database_integrity"], "ok")
        self.assertEqual(preview["schema_version"], 7)

    def test_v6_upgrade_creates_pre_v7_backup(self) -> None:
        path = self.root / "upgrade-v6.db"
        old = MemoryDatabase(path)
        with old.transaction(immediate=True) as conn:
            conn.execute("UPDATE schema_meta SET version=6")
        migrated = MemoryDatabase(path)
        self.assertEqual(migrated.schema_version(), 7)
        self.assertTrue(any((path.parent / "backups").glob("*-pre-schema-v7.db")))


class WorkspaceWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = MemoryService(Path(self.temp.name))
        self.client = TestClient(create_app(self.service))
        self.headers = {"Authorization": f"Bearer {self.service.mutation_token}"}
        self.client.headers.update(self.headers)

    def tearDown(self) -> None:
        self.client.close()
        self.service.shutdown(timeout=0.1)
        self.temp.cleanup()

    def test_project_work_context_storage_and_backup_apis(self) -> None:
        project = self.client.post("/api/projects", json={"name": "Web 项目"}, headers=self.headers)
        self.assertEqual(project.status_code, 200)
        project_id = project.json()["id"]
        self.assertEqual(self.client.get("/api/projects").json()[0]["name"], "Web 项目")
        self.assertEqual(self.client.get(f"/api/projects/{project_id}/context-preview?query=Web").status_code, 200)
        self.assertEqual(self.client.get("/api/maintenance/storage").status_code, 200)
        backup = self.client.post("/api/backup", headers=self.headers).json()["name"]
        preview = self.client.post(
            f"/api/backups/{backup}/preview-restore", headers=self.headers
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["schema_version"], 7)
