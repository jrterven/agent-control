"""Run directly with the external Hermes Python, without connector dependencies.

The connector runs on bundled Python 3.12, but copies this plugin's source into
an existing Hermes installation. Importing that copy with the older interpreter
is essential: newer ast.parse(feature_version=...) does not reject every newer
syntax feature, including backslashes in f-string expressions (PEP 701).
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / "packages/connector/agent_control_connector/hermes_media_plugin.py"
BACKGROUND_SOURCE = SOURCE.with_name("hermes_background_plugin.py")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a4J8AAAAASUVORK5CYII=")


class NativeContext:
    def __init__(self):
        self.tools = {}
        self.hooks = {}
        self.sections = {}

    def register_tool(self, name, toolset, schema, handler, **options):
        self.tools[name] = (toolset, schema, handler)
        return object()

    def register_hook(self, name, handler):
        self.hooks[name] = handler

    def register_system_prompt_section(self, name, content, **options):
        self.sections[name] = content


class CopiedHermesPluginRuntimeTest(unittest.TestCase):
    def test_background_plugin_uses_native_delegation_and_preserves_finite_runs(self):
        with tempfile.TemporaryDirectory(prefix="native-background-plugin-") as temporary:
            home = Path(temporary)
            entry = home / "plugins/agent-control-background/__init__.py"
            entry.parent.mkdir(parents=True)
            shutil.copyfile(BACKGROUND_SOURCE, entry)
            entry.with_name("installation.json").write_text(json.dumps({"installationId": "test-installation",
                "sourceSha": "939e45c91d751fadd94dcd1b873ac3cb44846213"}))
            spec = importlib.util.spec_from_file_location("isolated_background_plugin", entry)
            plugin = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(plugin)
            config_value = {"plugins": {"enabled": [plugin.PLUGIN_NAME]}}
            constants = types.ModuleType("hermes_constants")
            constants.get_hermes_home = lambda: home
            config = types.ModuleType("hermes_cli.config")
            config.load_config = lambda: config_value
            tools_config = types.ModuleType("hermes_cli.tools_config")
            tools_config._get_platform_tools = lambda value, platform: {"web", "delegation"}
            toolsets = types.ModuleType("toolsets")
            toolsets.resolve_toolset = lambda name: ["delegate_task"] if name == "delegation" else ["web_search"]
            async_delegation = types.ModuleType("tools.async_delegation")
            async_delegation.dispatch_async_delegation_batch = lambda **_: self.fail("must not dispatch")
            delegate_tool = types.ModuleType("tools.delegate_tool")
            delegate_tool.delegate_task = lambda **_: self.fail("must not delegate")
            context = NativeContext()
            with patch.dict(sys.modules, {"hermes_constants": constants,
                    "hermes_cli.config": config, "hermes_cli.tools_config": tools_config,
                    "toolsets": toolsets, "tools.async_delegation": async_delegation,
                    "tools.delegate_tool": delegate_tool}), patch.dict("os.environ", {"HERMES_TUI_TOOLSETS": ""}):
                plugin.register(context)
                self.assertEqual(context.tools, {})  # uses the real built-in, never replaces it
                hook = context.hooks["pre_llm_call"]
                self.assertIn("END YOUR TURN", hook(platform="tui", session_id="existing")['context'])
                self.assertIn("Do not reuse ac-media references", hook(platform="tui")['context'])
                self.assertLessEqual(len(plugin.INSTRUCTIONS), 4000)
                self.assertIn("uncertain", context.sections[plugin.PLUGIN_NAME]({"platform": "desktop"}))
                for platform in ("cron", "subagent", "api_server", ""):
                    self.assertIsNone(hook(platform=platform))
                self.assertIsNone(hook(platform="tui", parent_session_id="parent"))
                marker = json.loads((home / ".agent-control/background/runtime.json").read_text())
                self.assertTrue(marker["delegationAvailable"])
                self.assertEqual(marker["sha256"], hashlib.sha256(entry.read_bytes()).hexdigest())
                self.assertEqual(marker["installationId"], "test-installation")
                self.assertIsNone(marker["profileDeliveryShim"])  # CLI cannot prove Serve activation.
                self.assertIsNone(marker["profileDeliveryMode"])
                gateway = types.ModuleType("gateway.run")
                with patch.dict(sys.modules, {"gateway.run": gateway}), patch.dict("os.environ", {"_HERMES_GATEWAY": "1"}):
                    plugin.register(context)
                gateway_marker = json.loads((home / ".agent-control/background/runtime.json").read_text())
                self.assertEqual(gateway_marker["profileDeliveryMode"], "native-gateway")
                self.assertIsNone(gateway_marker["profileDeliveryShim"])
                config_value["agent"] = {"disabled_toolsets": ["delegation"]}
                self.assertIsNone(hook(platform="tui"))
                del config_value["agent"]
                with patch.dict("os.environ", {"HERMES_TUI_TOOLSETS": "web"}):
                    self.assertIsNone(hook(platform="tui"))

    def test_copied_plugin_registers_and_publishes_with_external_python(self):
        with tempfile.TemporaryDirectory(prefix="native-hermes-plugin-") as temporary:
            home = Path(temporary)
            entry = home / "plugins/agent-control-media/__init__.py"
            entry.parent.mkdir(parents=True)
            shutil.copyfile(SOURCE, entry)
            # Import the actual copied artifact as Hermes does, never through
            # the connector package (whose interpreter floor is newer).
            spec = importlib.util.spec_from_file_location("isolated_media_plugin", entry)
            plugin = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(plugin)
            constants = types.ModuleType("hermes_constants")
            constants.get_hermes_home = lambda: home
            hermes_cli = types.ModuleType("hermes_cli")
            hermes_cli.__path__ = []
            config = types.ModuleType("hermes_cli.config")
            config.load_config = lambda: {"plugins": {"enabled": ["agent-control-media"]}}
            context = NativeContext()
            with patch.dict(sys.modules, {"hermes_constants": constants,
                    "hermes_cli": hermes_cli, "hermes_cli.config": config}):
                plugin.register(context)
                marker = json.loads((home / ".agent-control/media/runtime.json").read_text())
                self.assertEqual(marker["sha256"], hashlib.sha256(entry.read_bytes()).hexdigest())
                toolset, schema, publish = context.tools["publish_images"]
                self.assertEqual(toolset, "agent-control-media")
                self.assertEqual(set(schema["parameters"]["properties"]), {"images"})
                self.assertIn("publish_images", context.sections["agent-control-media"]({}))
                turn = context.hooks["pre_llm_call"](session_id="native-session", turn_id="native-turn")
                self.assertIn("publish_images", turn["context"])
                source_image = home / "figure.png"
                source_image.write_bytes(PNG)
                args = {"images": [{"path": str(source_image), "alt": "Plot [one]\\\nsource", "provenance": "generated"}]}
                # No running connector is necessary for durable pending output.
                # Skip only the optional two-second wait for a cloud receipt.
                with patch.object(plugin.time, "monotonic", side_effect=[0.0, 3.0]):
                    result = json.loads(publish(args, session_id="native-session"))
                row = result["images"][0]
                self.assertEqual(row["status"], "pending")
                self.assertRegex(row["markdown"], r"^!\[[^\[\]\\\n\r]+\]\(ac-media:[a-f0-9]{32}\)$")
                self.assertEqual(json.loads(publish({**args, "session_id": "forged"}, session_id="native-session")),
                                 {"error": "MEDIA_INVALID_ARGUMENTS"})
            # A fresh connection proves publication and trusted routing survive
            # the handler returning, not merely a mocked in-memory response.
            with sqlite3.connect(home / ".agent-control/media/outbox.sqlite3") as db:
                stored = db.execute("SELECT id,session_id,turn_id,status,content FROM images").fetchall()
                self.assertEqual(stored, [(row["id"], "native-session", "native-turn", "pending", PNG)])
                self.assertEqual(db.execute("SELECT image_count FROM turns").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
