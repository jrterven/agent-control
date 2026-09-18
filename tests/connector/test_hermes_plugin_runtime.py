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
