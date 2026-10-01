from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.local_plugins import LocalPluginManager, load_observation_guide
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.topic_bus import TopicBus


class LocalPluginConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.plugin_root = self.root / "plugins" / "special" / "test_plugin"
        self.plugin_root.mkdir(parents=True)
        (self.plugin_root / "plugin.json").write_text(
            json.dumps(
                {
                    "id": "test_plugin",
                    "name": "Test Plugin",
                    "version": "1.0.0",
                    "entry": "main.py",
                    "provides": ["trace_plugin"],
                    "config_schema": "config.schema.json",
                    "publishes": [
                        {"topic": "test_plugin.events", "schema": "event"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.plugin_root / "config.schema.json").write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {
                        "rate": {
                            "type": "number",
                            "minimum": 1,
                            "maximum": 50,
                        },
                        "colors": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "pubsub": {
                            "type": "object",
                            "properties": {
                                "publish_enabled": {"type": "boolean"},
                                "enabled_topics": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        self.initial_config = {
            "rate": 10,
            "colors": ["yellow", "black"],
            "pubsub": {
                "publish_enabled": True,
                "enabled_topics": ["test_plugin.events"],
                "subscriptions": [],
            },
        }
        self.config_path = self.plugin_root / "config.json"
        self.config_path.write_text(
            json.dumps(self.initial_config), encoding="utf-8"
        )
        (self.plugin_root / "main.py").write_text(
            "class Plugin:\n    pass\n", encoding="utf-8"
        )
        self.manager = LocalPluginManager(
            plugins_root=self.root / "plugins",
            state_path=self.root / "profiles" / "default" / "plugins_state.json",
            registry=PluginRegistry(),
            event_bus=EventBus(),
            topic_bus=TopicBus(),
        )
        self.manager.discover()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def read_config(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_partial_update_preserves_arrays_and_pubsub(self) -> None:
        result = self.manager.update_config("test_plugin", {"rate": 20})

        saved = self.read_config()
        self.assertEqual(saved["rate"], 20)
        self.assertEqual(saved["colors"], ["yellow", "black"])
        self.assertEqual(saved["pubsub"], self.initial_config["pubsub"])
        self.assertEqual(result["config"], saved)

    def test_invalid_update_does_not_change_file(self) -> None:
        with self.assertRaisesRegex(ValueError, "config.rate must be <= 50"):
            self.manager.update_config("test_plugin", {"rate": 100})

        self.assertEqual(self.read_config(), self.initial_config)

    def test_invalid_array_item_type_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, r"config.colors\[1\] must be a string"):
            self.manager.update_config(
                "test_plugin", {"colors": ["yellow", 123]}
            )

        self.assertEqual(self.read_config(), self.initial_config)

    def test_failed_enable_rolls_back_enabled_state(self) -> None:
        (self.plugin_root / "main.py").write_text(
            'raise RuntimeError("load failed")\n', encoding="utf-8"
        )

        with self.assertRaisesRegex(ValueError, "failed to enable plugin: load failed"):
            self.manager.set_enabled("test_plugin", True)

        plugin = self.manager.get_plugin("test_plugin")
        self.assertFalse(plugin["enabled"])
        self.assertEqual(plugin["status"], "fault")
        state = json.loads(self.manager.state_path.read_text(encoding="utf-8"))
        self.assertFalse(state["enabled_plugins"]["test_plugin"])

    def test_v2_manifest_is_not_legacy_topic_action(self) -> None:
        raw = json.loads((self.plugin_root / "plugin.json").read_text(encoding="utf-8"))
        raw.update({"provides": ["action_owner"], "action_api_version": 2,
                    "actions": [{"action_id": "test_plugin.check.v2", "description": "Check",
                                 "schema": {"type": "object", "properties": {}},
                                 "operations": ["start"]}]})
        manifest = self.manager._manifest_from_bytes(json.dumps(raw).encode())
        self.manager._validate_manifest(manifest)
        self.assertEqual(manifest.actions, [])
        self.assertEqual(manifest.action_manifest_v2.actions[0].action_id, "test_plugin.check.v2")
        self.assertEqual(self.manager._runtime_kind(manifest), "action")
        for broken in ({**raw, "action_api_version": True},
                       {**raw, "actions": raw["actions"] * 2},
                       {**raw, "actions": [{**raw["actions"][0], "schema": {"type": "object", "bad": 1}}]}):
            with self.assertRaises(ValueError):
                self.manager._manifest_from_bytes(json.dumps(broken).encode())
        with self.assertRaisesRegex(ValueError, "duplicate manifest key"):
            self.manager._manifest_from_bytes(
                b'{"id":"test_plugin","id":"test_plugin","name":"Duplicate"}')
        with self.assertRaisesRegex(ValueError, "duplicate manifest key"):
            self.manager._manifest_from_bytes(
                b'{"id":"test_plugin","action_api_version":2,"actions":[],"observation_sources":{"x":{},"x":{}}}')
        raw["actions"] = [{"action_id": "test_plugin.check.v2", "description": "Check",
                           "topic": "test_plugin.optional", "schema": {"type": "object"},
                           "operations": ["start"]}]
        raw["action_api_version"] = 2
        optional = self.manager._manifest_from_bytes(json.dumps(raw).encode())
        self.assertEqual(optional.actions, [])
        self.assertEqual(optional.v2_optional_topics["test_plugin.check.v2"], "test_plugin.optional")
        raw.pop("action_api_version")
        raw["actions"] = [{"action_id": "test_plugin.check.v1", "topic": "test_plugin.command"}]
        legacy = self.manager._manifest_from_bytes(json.dumps(raw).encode())
        self.assertEqual(legacy.actions[0].topic, "test_plugin.command")
        raw["actions"] = [{"action_id": "test_plugin.check.v1"}]
        with self.assertRaisesRegex(ValueError, "legacy action topic is required"):
            self.manager._manifest_from_bytes(json.dumps(raw).encode())

    def test_context_exposes_closed_action_facade(self) -> None:
        record = self.manager.records["test_plugin"]
        from types import ModuleType
        module = ModuleType("test_context")
        seen = []

        def create_plugin(context):
            seen.append(context)
            return type("MockPlugin", (), {})()

        module.create_plugin = create_plugin
        plugin = self.manager._create_plugin(module, record)
        self.assertIs(plugin._astrbotex_context, seen[0])
        with self.assertRaises(RuntimeError):
            seen[0].actions.report("cmd", "running")
        seen[0].ros.close()

    def test_real_b01_guides_allow_markdown_comparisons(self) -> None:
        fixture_root = Path(__file__).parent / "fixtures" / "decision"
        for filename in ("yolo-front-detections.md", "simulated-base-status.md", "simulated-arm-status.md"):
            guide = load_observation_guide(fixture_root, filename)
            expected = (fixture_root / filename).read_bytes()
            self.assertEqual(guide["status"], "available", filename)
            self.assertEqual(guide["text"], expected.decode("utf-8"), filename)
            self.assertEqual(guide["content_hash"], hashlib.sha256(expected).hexdigest(), filename)
        detection = load_observation_guide(fixture_root, "yolo-front-detections.md")["text"]
        self.assertIn("front-<seq>", detection)
        self.assertIn("0<=left<right<=image_width", detection)

    def test_guide_bytes_and_paths(self) -> None:
        root = self.plugin_root
        target = root / "guide.txt"
        target.write_bytes("hello\n".encode())
        guide = load_observation_guide(root, "guide.txt")
        self.assertEqual(guide["status"], "available")
        self.assertEqual(guide["content_hash"], hashlib.sha256(b"hello\n").hexdigest())
        for path in ("../outside", "sub/../outside", "/tmp/outside", "C:\\outside",
                     "C:relative.txt", "//server/share", "\\\\server\\share", "sub\\..\\outside"):
            self.assertEqual(load_observation_guide(root, path)["status"], "rejected")
        self.assertEqual(load_observation_guide(root, "missing.txt")["status"], "unavailable")
        for data in (b"x" * 8193, b"\xff", b"hello\x00world", b"a\x7fb",
                     b"a" + bytes((0xC2, 0x85)) + b"b"):
            target.write_bytes(data)
            self.assertEqual(load_observation_guide(root, "guide.txt")["status"], "rejected")
        target.write_bytes(b"x" * 8192)
        self.assertEqual(load_observation_guide(root, "guide.txt")["status"], "available")
        with patch.object(Path, "resolve", side_effect=RuntimeError("symlink loop")):
            self.assertEqual(load_observation_guide(root, "guide.txt")["status"], "rejected")
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = root / "linked.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            print(f"real symlink unavailable in test environment: {exc}")
            original_resolve = Path.resolve

            def escaping_resolve(path, *args, **kwargs):
                if path == link:
                    return outside
                return original_resolve(path, *args, **kwargs)

            with patch.object(Path, "resolve", escaping_resolve):
                self.assertEqual(load_observation_guide(root, "linked.txt")["status"], "rejected")
        else:
            self.assertEqual(load_observation_guide(root, "linked.txt")["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
