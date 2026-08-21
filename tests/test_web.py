from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from b1ack_memory.service import MemoryService
from b1ack_memory.web import create_app, create_router


class WebTests(unittest.TestCase):
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

    def test_ui_and_mutation_token(self) -> None:
        self.assertEqual(self.client.get("/api/ui/").status_code, 200)
        bundle = self.client.get("/api/ui-bundle")
        self.assertEqual(bundle.status_code, 200)
        self.assertIn("B1ack Memory", bundle.json()["html"])
        self.assertIn("HERMES NATIVE FILES", bundle.json()["html"])
        self.assertIn("Daily Memory", bundle.json()["html"])
        self.assertNotIn('data-candidate-status="promoted"', bundle.json()["html"])
        self.assertIn("REM", bundle.json()["html"])
        self.assertIn("@media(max-width:700px)", bundle.json()["css"])
        self.assertIn("bridge.request", bundle.json()["js"])
        self.assertIn("Promise.allSettled", bundle.json()["js"])
        self.assertIn('classList.remove("skeleton")', bundle.json()["js"])
        self.assertNotIn("confirm-text", bundle.json()["html"])
        self.assertNotIn("confirmText=", bundle.json()["js"])
        self.assertIn('value="cancel" class="quiet" formnovalidate', bundle.json()["html"])
        self.assertIn("await mutate(`/memories/${b.dataset.trashMemory}/trash`);closeDrawer()", bundle.json()["js"])
        self.assertIn("await mutate(`/memories/${b.dataset.purgeMemory}`,{},'DELETE');closeDrawer()", bundle.json()["js"])
        self.assertIn("admission_state=legacy_history", bundle.json()["js"])
        self.assertIn("loadHermesNativeMemory", bundle.json()["js"])
        self.assertEqual(self.client.post(
            "/api/memories", json={"content": "测试"}, headers={"Authorization": ""}
        ).status_code, 403)
        self.assertNotIn("token", self.client.get("/api/bootstrap", headers=self.headers).json())
        response = self.client.post(
            "/api/memories",
            json={"content": "用户喜欢白盒化记忆", "kind": "preference"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.client.get("/api/memories").json()), 1)
        invalid = self.client.post(
            "/api/settings/recall",
            json={"limit": 100},
            headers=self.headers,
        )
        self.assertEqual(invalid.status_code, 400)

    def test_dashboard_router_serves_external_ui_assets(self) -> None:
        app = FastAPI()
        app.include_router(
            create_router(self.service, auth_mode="dashboard"),
            prefix="/api/plugins/b1ack-memory",
        )
        with TestClient(app) as client:
            page = client.get("/api/plugins/b1ack-memory/ui/")
            self.assertEqual(page.status_code, 200)
            self.assertIn('src="app.js"', page.text)
            self.assertEqual(client.get("/api/plugins/b1ack-memory/ui/app.js").status_code, 200)
            self.assertEqual(client.get("/api/plugins/b1ack-memory/ui/style.css").status_code, 200)
            bootstrap = client.get("/api/plugins/b1ack-memory/bootstrap")
            self.assertEqual(bootstrap.status_code, 200)
            self.assertEqual(bootstrap.json()["auth_mode"], "dashboard")

    def test_operational_errors_are_safe_and_readable(self) -> None:
        headers = self.headers

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
        headers = self.headers
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
        headers = self.headers
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

    def test_review_center_scan_resolve_and_dismiss_api(self) -> None:
        headers = self.headers
        pending = self.client.post(
            "/api/memories",
            json={"content": "我的银行卡需要单独管理", "kind": "fact"},
            headers=headers,
        )
        self.assertEqual(pending.json()["status"], "review_required")
        reviews = self.client.get("/api/reviews?status=open").json()
        self.assertEqual(reviews[0]["issue_type"], "sensitive")
        resolved = self.client.post(
            f"/api/reviews/{reviews[0]['id']}/resolve",
            json={"action": "create"},
            headers=headers,
        )
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.json()["status"], "resolved")
        self.assertEqual(len(self.client.get("/api/memories").json()), 1)

        second = self.client.post(
            "/api/memories",
            json={"content": "我的身份证需要离线保管", "kind": "fact"},
            headers=headers,
        ).json()
        review_id = second["review"]["id"]
        dismissed = self.client.post(
            f"/api/reviews/{review_id}/dismiss", headers=headers
        )
        self.assertEqual(dismissed.json()["status"], "dismissed")
        self.assertTrue(self.client.get("/api/reviews?status=dismissed").json())

        scan = self.client.post(
            "/api/reviews/scan", json={"scope": "full"}, headers=headers
        )
        self.assertEqual(scan.status_code, 200)
        self.assertEqual(scan.json()["status"], "failed")

    def test_timezone_settings_validate_and_recompute(self) -> None:
        headers = self.headers
        response = self.client.post(
            "/api/settings/general", json={"timezone": "Asia/Shanghai"}, headers=headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["timezone"], "Asia/Shanghai")
        self.assertEqual(
            self.client.get("/api/status").json()["general"]["timezone"], "Asia/Shanghai"
        )
        invalid = self.client.post(
            "/api/settings/general", json={"timezone": "Not/AZone"}, headers=headers
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("IANA timezone", invalid.json()["detail"])

    def test_analytics_lineage_and_promoted_candidate_api(self) -> None:
        headers = self.headers
        candidate = self.service.db.upsert_candidate(
            "演化接口候选",
            kind="project",
            confidence=0.91,
            sensitive=False,
            raw_turn_id=None,
            excerpt="演化接口候选",
        )
        candidate_lineage = self.client.get(f"/api/lineage/candidate/{candidate.id}")
        self.assertEqual(candidate_lineage.status_code, 200)
        self.assertEqual(candidate_lineage.json()["candidate"]["id"], candidate.id)
        promoted = self.client.post(f"/api/candidates/{candidate.id}/promote", headers=headers)
        self.assertEqual(promoted.status_code, 200)
        self.assertEqual(promoted.json()["status"], "promoted")
        memory_id = promoted.json()["memory"]["id"]
        promoted_rows = self.client.get("/api/candidates?status=promoted").json()
        self.assertEqual(promoted_rows[0]["id"], candidate.id)
        self.assertEqual(promoted_rows[0]["promoted_memory_id"], memory_id)
        self.assertEqual(promoted_rows[0]["linked_memory"]["id"], memory_id)
        memory_lineage = self.client.get(f"/api/lineage/memory/{memory_id}").json()
        event_types = {item["event_type"] for item in memory_lineage["events"]}
        self.assertIn("candidate_promoted", event_types)
        promotion = next(
            item for item in memory_lineage["events"] if item["event_type"] == "candidate_promoted"
        )
        self.assertEqual(promotion["data"]["promotion_lane"], "manual")

        analytics = self.client.get("/api/analytics/memory-flow?range=30d")
        self.assertEqual(analytics.status_code, 200)
        self.assertEqual(analytics.json()["promotion_lanes"]["manual"], 1)
        self.assertNotIn("promoted", analytics.json()["status"])
        self.assertEqual(self.client.get("/api/analytics/memory-flow?range=bad").status_code, 400)


if __name__ == "__main__":
    unittest.main()
