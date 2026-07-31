from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from astrbot_ex.core.astrbot_bridge import AstrBotBridge, build_scene_summary
from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.local_plugins import LocalPluginManager
from astrbot_ex.core.models import Entity, RuntimeState, ScanCluster, WorldState
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
                "entities": self.runtime.world.entities,
                "zones": self.runtime.world.zones,
                "robot": self.runtime.world.robot,
                "obstacles": self.runtime.world.obstacles,
                "perception_degraded": self.runtime.world.perception_degraded,
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

    def test_context_contains_perception_scene_block(self) -> None:
        self.runtime.world = WorldState(
            entities=[
                Entity(
                    id="matched",
                    type="rescue_target",
                    semantic="own_normal",
                    confidence=0.9,
                    bearing_deg=-6.7,
                    range_m=1.2,
                    range_quality=0.8,
                ),
                Entity(
                    id="vision_only",
                    type="rescue_target",
                    semantic="own_normal",
                    confidence=0.7,
                    bearing_deg=-15.0,
                    range_m=None,
                    range_quality=None,
                ),
                Entity(
                    id="low_quality",
                    type="rescue_target",
                    semantic="own_normal",
                    confidence=0.6,
                    bearing_deg=20.0,
                    range_m=1.2,
                    range_quality=0.2,
                ),
            ],
            obstacles=[
                ScanCluster(
                    id="obstacle_0",
                    bearing_deg=35.0,
                    range_m=0.9,
                    width_deg=4.0,
                    point_count=3,
                    quality=1.0,
                )
            ],
            perception_degraded=True,
            task_state={"perception_notes": ["scan unavailable"]},
        )

        context = self.bridge.build_context()

        perception_blocks = [
            block
            for block in context["blocks"]
            if block["block_id"] == "perception.scene.v1"
        ]
        self.assertEqual(len(perception_blocks), 1)
        payload = perception_blocks[0]["payload"]
        self.assertEqual(
            set(payload),
            {"timestamp", "degraded", "summary", "targets", "obstacles", "notes"},
        )
        self.assertTrue(payload["degraded"])
        self.assertEqual(payload["notes"], ["scan unavailable"])
        self.assertEqual([target["id"] for target in payload["targets"]], ["matched", "vision_only", "low_quality"])
        self.assertIn("range_quality", payload["targets"][0])
        self.assertEqual(payload["targets"][0]["range_quality"], 0.8)
        self.assertIn("ahead", payload["summary"])
        self.assertIn("-6.7", payload["summary"])
        self.assertIn("1.2", payload["summary"])
        self.assertIn("range unknown", payload["summary"])
        self.assertIn("~1.2m (low confidence)", payload["summary"])
        self.assertTrue(payload["summary"].startswith("[DEGRADED"))
        self.assertIn("scan unavailable", payload["summary"])
        self.assertEqual(payload["obstacles"][0]["id"], "obstacle_0")

    def test_build_scene_summary_is_deterministic_and_handles_empty_scene(self) -> None:
        summary1 = build_scene_summary([], [], False, [])
        summary2 = build_scene_summary([], [], False, [])

        self.assertEqual(summary1, summary2)
        self.assertIn("No targets", summary1)
        self.assertIn("No obstacles", summary1)

    def test_build_scene_summary_truncates_obstacles_with_count(self) -> None:
        obstacles = [
            {
                "id": f"obstacle_{index}",
                "bearing_deg": float(index * 10),
                "range_m": float(index + 1),
                "width_deg": 1.0,
                "point_count": 1,
                "quality": 1.0,
            }
            for index in range(7)
        ]

        summary = build_scene_summary([], obstacles, False, [])

        self.assertIn("and 2 more obstacles", summary)


if __name__ == "__main__":
    unittest.main()
