from __future__ import annotations

import unittest

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.models import FusedScene, RobotState, ScanResult, VisionResult, WorldState
from astrbot_ex.core.perception_core import PerceptionCore
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.runtime import AstrBotEXRuntime
from mock_plugins import FailingScanProvider, MockScanProvider, MockVisionProvider


class StaticFusion:
    def fuse(self, vision: VisionResult, scan: ScanResult | None) -> FusedScene:
        return FusedScene(
            timestamp=vision.timestamp,
            entities=vision.entities,
            obstacles=[],
            degraded=False,
            notes=["static fusion used"],
        )


class FailingVisionProvider(MockVisionProvider):
    id = "failing_vision"
    name = "Failing Vision Provider"

    def get_result(self) -> VisionResult:
        raise RuntimeError("vision boom")


class PerceptionCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = PluginRegistry()
        self.event_bus = EventBus()

    def tearDown(self) -> None:
        for slot in reversed(self.registry.list()):
            self.registry.unregister(slot.id)

    def core(self, fusion: StaticFusion | None = None) -> PerceptionCore:
        return PerceptionCore(
            registry=self.registry,
            event_bus=self.event_bus,
            fusion=fusion,
        )

    def test_without_fusion_preserves_legacy_world_fields(self) -> None:
        self.registry.register("vision", MockVisionProvider())
        robot = RobotState(link_ok=True, metadata={"source": "test"})

        world = self.core().update(robot, WorldState(timestamp=10.0))

        self.assertEqual(len(world.entities), 1)
        self.assertIsNone(world.entities[0].bearing_deg)
        self.assertIsNone(world.entities[0].range_m)
        self.assertIs(world.robot, robot)
        self.assertEqual(world.obstacles, [])
        self.assertFalse(world.perception_degraded)

    def test_missing_vision_reuses_previous_world_timestamp(self) -> None:
        previous_world = WorldState(timestamp=123.5)

        world = self.core().update(RobotState(), previous_world)

        self.assertEqual(world.timestamp, previous_world.timestamp)
        self.assertEqual(world.task_state["frame_id"], 0)

    def test_scan_failure_emits_plugin_fault_and_uses_none(self) -> None:
        self.registry.register("vision", MockVisionProvider())
        self.registry.register("scan", FailingScanProvider())

        world = self.core().update(RobotState(), WorldState())

        self.assertIsNone(world.task_state["scan_frame_id"])
        self.assertTrue(
            any(
                event.type == "plugin_fault" and event.message == "scan read failed"
                for event in self.event_bus.recent()
            )
        )

    def test_injected_fusion_provider_is_used(self) -> None:
        self.registry.register("vision", MockVisionProvider())
        self.registry.register("scan", MockScanProvider())

        world = self.core(StaticFusion()).update(RobotState(), WorldState())

        self.assertEqual(world.task_state["perception_notes"], ["static fusion used"])
        self.assertFalse(world.perception_degraded)

    def test_vision_failure_still_propagates(self) -> None:
        self.registry.register("vision", FailingVisionProvider())

        with self.assertRaisesRegex(RuntimeError, "vision boom"):
            self.core().update(RobotState(), WorldState())

    def test_runtime_accepts_injected_perception_core(self) -> None:
        self.registry.register("vision", MockVisionProvider())
        perception = self.core()
        runtime = AstrBotEXRuntime(
            self.registry,
            event_bus=self.event_bus,
            perception=perception,
        )
        try:
            runtime.start()
            runtime.tick()

            self.assertIs(runtime.perception_core, perception)
            self.assertEqual(len(runtime.world.entities), 1)
        finally:
            runtime.stop("test complete")


if __name__ == "__main__":
    unittest.main()
