from __future__ import annotations

import argparse
import inspect
import json
import tempfile
import unittest
from pathlib import Path

import b1ack_memory
from b1ack_memory.cli import register_cli
from b1ack_memory.provider import B1ackMemoryProvider
from b1ack_memory.service import MemoryService


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = MemoryService(Path(self.temp.name))
        self.provider = B1ackMemoryProvider(self.service)

    def tearDown(self) -> None:
        self.service.shutdown(timeout=1)
        self.temp.cleanup()

    def test_provider_contract_and_primary_turn_capture(self) -> None:
        self.provider.initialize("session-1", agent_context="primary", platform="cli")
        self.provider.sync_turn(
            "用户消息",
            "助手回复",
            session_id="session-1",
            messages=[{"role": "user", "content": "用户消息"}],
        )
        self.service.flush()
        self.assertTrue(self.provider.is_available())
        self.assertEqual(self.provider.name, "b1ack-memory")
        self.assertEqual(len(self.provider.get_tool_schemas()), 2)
        with self.service.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM raw_turns").fetchone()[0], 1)

    def test_subagent_turn_is_not_captured(self) -> None:
        self.provider.initialize("child", agent_context="subagent", platform="cli")
        self.provider.sync_turn("内部任务", "内部结果", messages=[])
        self.service.flush()
        with self.service.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM raw_turns").fetchone()[0], 0)

    def test_tool_calls_return_json(self) -> None:
        remembered = json.loads(
            self.provider.handle_tool_call(
                "b1ack_memory_remember", {"content": "集成测试记忆", "kind": "fact"}
            )
        )
        searched = json.loads(
            self.provider.handle_tool_call("b1ack_memory_search", {"query": "集成测试"})
        )
        self.assertEqual(remembered["status"], "remembered")
        self.assertEqual(len(searched["results"]), 1)

    def test_sync_turn_accepts_current_hermes_keyword_arguments(self) -> None:
        signature = inspect.signature(B1ackMemoryProvider.sync_turn)
        self.assertIn("messages", signature.parameters)
        self.assertTrue(
            any(item.kind is inspect.Parameter.VAR_KEYWORD for item in signature.parameters.values())
        )


class DistributionTests(unittest.TestCase):
    def test_packaged_dashboard_and_manifest_assets_exist(self) -> None:
        package = Path(b1ack_memory.__file__).parent
        required = [
            package / "plugin.yaml",
            package / "dashboard" / "manifest.json",
            package / "dashboard" / "plugin_api.py",
            package / "dashboard" / "dist" / "index.js",
            package / "static" / "index.html",
            package / "static" / "app.js",
            package / "static" / "style.css",
        ]
        self.assertTrue(all(path.is_file() for path in required), required)
        manifest = json.loads((package / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "b1ack-memory")
        self.assertEqual(manifest["version"], b1ack_memory.__version__)

    def test_hermes_cli_registration_uses_existing_parser(self) -> None:
        parser = argparse.ArgumentParser()
        register_cli(parser)
        args = parser.parse_args(["status"])
        self.assertEqual(args.command, "status")
        self.assertTrue(callable(args.func))


if __name__ == "__main__":
    unittest.main()
