from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
import types
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
    def test_directory_plugin_is_discoverable_and_loadable(self) -> None:
        root = Path(__file__).resolve().parents[1]
        entrypoint = (root / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("register_memory_provider", entrypoint)
        self.assertIn("MemoryProvider", entrypoint)

        package_name = "_hermes_user_memory.b1ack-memory"
        parent_name = package_name.rpartition(".")[0]
        parent = types.ModuleType(parent_name)
        parent.__path__ = []
        sys.modules[parent_name] = parent
        previous_home = os.environ.get("B1ACK_MEMORY_HOME")
        try:
            with tempfile.TemporaryDirectory() as memory_home:
                os.environ["B1ACK_MEMORY_HOME"] = memory_home
                spec = importlib.util.spec_from_file_location(
                    package_name,
                    root / "__init__.py",
                    submodule_search_locations=[str(root)],
                )
                self.assertIsNotNone(spec)
                self.assertIsNotNone(spec.loader)
                module = importlib.util.module_from_spec(spec)
                sys.modules[package_name] = module
                spec.loader.exec_module(module)

                class Collector:
                    provider = None

                    def register_memory_provider(self, provider) -> None:
                        self.provider = provider

                collector = Collector()
                module.register(collector)
                self.assertIsNotNone(collector.provider)
                self.assertEqual(collector.provider.name, "b1ack-memory")
                collector.provider.service.shutdown(timeout=1)
        finally:
            if previous_home is None:
                os.environ.pop("B1ACK_MEMORY_HOME", None)
            else:
                os.environ["B1ACK_MEMORY_HOME"] = previous_home
            for name in list(sys.modules):
                if name == parent_name or name.startswith(f"{package_name}.") or name == package_name:
                    sys.modules.pop(name, None)

    def test_directory_manifest_matches_release(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = (root / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("manifest_version: 1", manifest)
        self.assertIn("kind: exclusive", manifest)
        self.assertIn(f'version: "{b1ack_memory.__version__}"', manifest)

    def test_dashboard_iframe_uses_authenticated_sdk_bridge(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for script in (
            root / "dashboard" / "dist" / "index.js",
            root / "b1ack_memory" / "dashboard" / "dist" / "index.js",
        ):
            source = script.read_text(encoding="utf-8")
            self.assertIn("SDK.fetchJSON", source)
            self.assertIn("SDK.authedFetch", source)
            self.assertIn('API + "/ui-bundle"', source)
            self.assertIn("srcDoc: documentHtml", source)
            self.assertNotIn('src: dashboardBasePath', source)

    def test_dashboard_api_can_be_loaded_as_a_standalone_module(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (
            "import importlib.util; "
            f"p={str(root / 'dashboard' / 'plugin_api.py')!r}; "
            "s=importlib.util.spec_from_file_location('hermes_dashboard_plugin_b1ack_memory', p); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "assert m.router.prefix == ''"
        )
        with tempfile.TemporaryDirectory() as memory_home:
            environment = os.environ.copy()
            environment["B1ACK_MEMORY_HOME"] = memory_home
            environment.pop("PYTHONPATH", None)
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=memory_home,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

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
