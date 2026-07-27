from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.local_plugins import LocalPluginManager
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


if __name__ == "__main__":
    unittest.main()
