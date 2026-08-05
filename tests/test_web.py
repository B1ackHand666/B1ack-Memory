from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from b1ack_memory.service import MemoryService
from b1ack_memory.web import create_app


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = MemoryService(Path(self.temp.name))
        self.client = TestClient(create_app(self.service))

    def tearDown(self) -> None:
        self.client.close()
        self.service.shutdown(timeout=0.1)
        self.temp.cleanup()

    def test_ui_and_mutation_token(self) -> None:
        self.assertEqual(self.client.get("/api/ui/").status_code, 200)
        bundle = self.client.get("/api/ui-bundle")
        self.assertEqual(bundle.status_code, 200)
        self.assertIn("B1ack Memory", bundle.json()["html"])
        self.assertIn("dashboardBridge.request", bundle.json()["js"])
        self.assertEqual(self.client.post("/api/memories", json={"content": "测试"}).status_code, 403)
        token = self.client.get("/api/bootstrap").json()["token"]
        response = self.client.post(
            "/api/memories",
            json={"content": "用户喜欢白盒化记忆", "kind": "preference"},
            headers={"X-B1ack-Memory-Token": token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.client.get("/api/memories").json()), 1)
        invalid = self.client.post(
            "/api/settings/recall",
            json={"limit": 100},
            headers={"X-B1ack-Memory-Token": token},
        )
        self.assertEqual(invalid.status_code, 400)

    def test_operational_errors_are_safe_and_readable(self) -> None:
        token = self.client.get("/api/bootstrap").json()["token"]
        headers = {"X-B1ack-Memory-Token": token}

        model = self.client.post("/api/model/test", json={"kind": "llm"}, headers=headers)
        self.assertEqual(model.status_code, 400)
        self.assertIn("configured", model.json()["detail"].lower())

        dry_run = self.client.post(
            "/api/dream/run", json={"dry_run": True}, headers=headers
        )
        self.assertEqual(dry_run.status_code, 200)
        self.assertEqual(dry_run.json()["status"], "dry_run")
        self.assertEqual(self.client.get("/api/dream-runs").json(), [])

        create_response = self.client.post(
            "/api/memories", json={"content": "用户喜欢黑咖啡"}, headers=headers
        )
        self.assertEqual(create_response.status_code, 200)
        created = create_response.json()["memory"]
        backup = self.client.post("/api/backup", headers=headers).json()["name"]
        missing = self.client.delete("/api/memories/missing-id", headers=headers)
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(self.client.get("/api/memories").json()[0]["id"], created["id"])
        self.assertIn(backup, {item["name"] for item in self.client.get("/api/backups").json()})

        corrupt = self.service.backup_dir / "corrupt.db"
        corrupt.write_text("not a sqlite database", encoding="utf-8")
        restore = self.client.post(f"/api/backups/{corrupt.name}/restore", headers=headers)
        self.assertEqual(restore.status_code, 400)
        self.assertIn("backup", restore.json()["detail"].lower())
        self.assertEqual(self.client.get("/api/memories").json()[0]["id"], created["id"])

    def test_candidate_status_restore_and_privacy_delete_api(self) -> None:
        token = self.client.get("/api/bootstrap").json()["token"]
        headers = {"X-B1ack-Memory-Token": token}
        candidate = self.service.db.upsert_candidate(
            "候选 API 测试",
            kind="fact",
            confidence=0.9,
            sensitive=False,
            raw_turn_id=None,
            excerpt="候选 API 测试",
        )
        rejected = self.client.post(
            f"/api/candidates/{candidate.id}/reject", headers=headers
        )
        self.assertEqual(rejected.status_code, 200)
        rows = self.client.get("/api/candidates?status=rejected").json()
        self.assertEqual(rows[0]["id"], candidate.id)
        self.assertIsNotNone(rows[0]["lifecycle"]["purge_at"])

        restored = self.client.post(
            f"/api/candidates/{candidate.id}/restore", headers=headers
        )
        self.assertEqual(restored.status_code, 200)
        deleted = self.client.delete(f"/api/candidates/{candidate.id}", headers=headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get("/api/candidates").json(), [])

    def test_candidate_retention_settings_are_exposed(self) -> None:
        token = self.client.get("/api/bootstrap").json()["token"]
        headers = {"X-B1ack-Memory-Token": token}
        settings = self.client.get("/api/settings").json()
        self.assertEqual(settings["dream"]["max_new_candidates"], 8)
        self.assertEqual(settings["retention"]["candidate_inactive_days"], 14)
        response = self.client.post(
            "/api/settings/retention",
            json={"candidate_inactive_days": 21},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["candidate_inactive_days"], 21)


if __name__ == "__main__":
    unittest.main()
