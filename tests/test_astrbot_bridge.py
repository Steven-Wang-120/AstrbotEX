from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from astrbot_ex.core.astrbot_bridge import AstrBotBridge
from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.local_plugins import LocalPluginManager
from astrbot_ex.core.models import RuntimeState, WorldState
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.runtime import AstrBotEXRuntime
from astrbot_ex.core.topic_bus import TopicBus


class FakeController:
    def __init__(self, runtime: AstrBotEXRuntime) -> None:
        self.runtime = runtime
        self.started = False
        self.stopped_reason = ""

    def start(self) -> None:
        self.started = True
        self.runtime.state = RuntimeState.RUNNING

    def stop(self, reason: str = "stopped") -> None:
        self.stopped_reason = reason
        self.runtime.state = RuntimeState.IDLE

    def status(self) -> dict[str, Any]:
        return {
            "runtime_state": self.runtime.state.value,
            "tick_hz": 20,
            "active_skill": None,
            "active_goal": None,
            "world": {
                "timestamp": self.runtime.world.timestamp,
                "entities": [],
                "zones": [],
                "robot": self.runtime.world.robot,
            },
            "recent_events": [],
        }


class AstrBotBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.plugin_root = self.root / "plugins" / "decision" / "mission_controller"
        self.plugin_root.mkdir(parents=True)
        (self.plugin_root / "plugin.json").write_text(
            json.dumps(
                {
                    "id": "mission_controller",
                    "name": "Mission Controller",
                    "version": "1.0.0",
                    "entry": "main.py",
                    "provides": ["tool_plugin"],
                    "enabled_default": True,
                    "publishes": [
                        {"topic": "mission_controller.phase", "schema": "phase"}
                    ],
                    "actions": [
                        {
                            "action_id": "mission_controller.set_phase.v1",
                            "topic": "mission_controller.commands.set_phase",
                            "description": "Set mission phase.",
                            "schema": {
                                "type": "object",
                                "required": ["phase"],
                                "properties": {"phase": {"type": "string"}},
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.plugin_root / "main.py").write_text("class Plugin:\n    pass\n", encoding="utf-8")
        self.event_bus = EventBus()
        self.topic_bus = TopicBus()
        self.registry = PluginRegistry()
        self.manager = LocalPluginManager(
            plugins_root=self.root / "plugins",
            state_path=self.root / "profiles" / "default" / "plugins_state.json",
            registry=self.registry,
            event_bus=self.event_bus,
            topic_bus=self.topic_bus,
        )
        self.manager.discover()
        self.runtime = AstrBotEXRuntime(
            PluginRegistry(),
            event_bus=self.event_bus,
            topic_bus=self.topic_bus,
        )
        self.runtime.world = WorldState()
        self.controller = FakeController(self.runtime)
        self.bridge = AstrBotBridge(
            controller=self.controller,
            local_plugins=self.manager,
            event_bus=self.event_bus,
            topic_bus=self.topic_bus,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_context_contains_fresh_plugin_blocks(self) -> None:
        self.topic_bus.publish_payload(
            "mission_controller.phase",
            timestamp=time.time(),
            source="mission_controller",
            payload={"phase": "search_target"},
            seq=7,
            ttl_ms=99_999_999,
        )

        context = self.bridge.build_context()

        plugin_blocks = [
            block
            for block in context["blocks"]
            if block["topic"] == "mission_controller.phase"
        ]
        self.assertEqual(len(plugin_blocks), 1)
        self.assertTrue(plugin_blocks[0]["fresh"])
        self.assertEqual(plugin_blocks[0]["seq"], 7)
        self.assertIn("contract_id", plugin_blocks[0])

    def test_plugin_action_proposal_is_validated_and_published(self) -> None:
        context = self.bridge.build_context()
        result = self.bridge.handle_proposal(
            {
                "context_id": context["context_id"],
                "commands": [
                    {
                        "action_id": "mission_controller.set_phase.v1",
                        "params": {"phase": "return_home"},
                        "reason": "target captured",
                    }
                ],
            }
        )

        self.assertTrue(result["ok"])
        command = self.topic_bus.get_latest("mission_controller.commands.set_phase")
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command.payload["params"]["phase"], "return_home")
        self.assertEqual(command.payload["context_id"], context["context_id"])

    def test_invalid_action_params_are_rejected(self) -> None:
        context = self.bridge.build_context()
        result = self.bridge.handle_proposal(
            {
                "context_id": context["context_id"],
                "commands": [
                    {
                        "action_id": "mission_controller.set_phase.v1",
                        "params": {},
                        "reason": "missing phase",
                    }
                ],
            }
        )

        self.assertFalse(result["ok"])
        self.assertIn("params.phase is required", result["error"])


if __name__ == "__main__":
    unittest.main()
