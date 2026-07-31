from __future__ import annotations

import unittest

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.models import RuntimeState
from astrbot_ex.core.perception_config import CameraConfig, FusionConfig, PerceptionConfig
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.runtime import AstrBotEXRuntime
from astrbot_ex.core.scene_fusion import SceneFusion
from mock_plugins import (
    ApproachEntitySkill,
    BasicRulePlugin,
    FailingScanProvider,
    MockMotionBridge,
    MockScanProvider,
    MockVisionProvider,
    NearestEntityPolicy,
)


class RuntimeActorIntegrationTest(unittest.TestCase):
    def fusion(self) -> SceneFusion:
        return SceneFusion(
            PerceptionConfig(
                camera=CameraConfig(
                    hfov_deg=90.0,
                    image_width_px=640,
                    image_height_px=480,
                    to_lidar_yaw_offset_deg=0.0,
                    x_to_lidar_angle_sign=-1.0,
                ),
                fusion=FusionConfig(
                    bearing_tolerance_deg=8.0,
                    time_window_ms=150.0,
                    range_min_m=0.05,
                    range_max_m=12.0,
                    range_window_deg=3.0,
                    range_select_method="min",
                ),
            )
        )

    def unregister_all(self, registry: PluginRegistry) -> None:
        for slot in reversed(registry.list()):
            registry.unregister(slot.id)

    def test_runtime_calls_each_plugin_through_its_actor(self) -> None:
        registry = PluginRegistry()
        plugins = [
            ("vision", MockVisionProvider()),
            ("motion", MockMotionBridge()),
            ("rule", BasicRulePlugin()),
            ("policy", NearestEntityPolicy()),
            ("skill", ApproachEntitySkill()),
        ]
        for kind, plugin in plugins:
            registry.register(kind, plugin)

        runtime = AstrBotEXRuntime(registry)
        try:
            runtime.start()
            for _ in range(3):
                runtime.tick()

            self.assertEqual(len(runtime.world.entities), 1)
            self.assertIsNone(runtime.active_skill)
            self.assertTrue(all(slot.actor.alive for slot in registry.list()))
            self.assertEqual(
                {slot.actor.thread_name for slot in registry.list()},
                {
                    "astrbotex-plugin-mock_vision",
                    "astrbotex-plugin-mock_motion",
                    "astrbotex-plugin-basic_rules",
                    "astrbotex-plugin-nearest_entity_policy",
                    "astrbotex-plugin-approach_entity",
                },
            )
        finally:
            runtime.stop("test complete")
            for _, plugin in reversed(plugins):
                registry.unregister(plugin.id)

    def test_runtime_tick_degrades_without_scan_provider(self) -> None:
        registry = PluginRegistry()
        registry.register("vision", MockVisionProvider())
        event_bus = EventBus()
        runtime = AstrBotEXRuntime(registry, event_bus=event_bus, fusion=self.fusion())
        try:
            runtime.start()
            runtime.tick()

            self.assertEqual(runtime.world.task_state["scan_frame_id"], None)
            self.assertTrue(runtime.world.perception_degraded)
            self.assertTrue(
                any(event.message == "scan provider unavailable" for event in event_bus.recent())
            )
        finally:
            runtime.stop("test complete")
            self.unregister_all(registry)

    def test_runtime_tick_fuses_scan_provider_result(self) -> None:
        registry = PluginRegistry()
        registry.register("vision", MockVisionProvider())
        registry.register("scan", MockScanProvider())
        runtime = AstrBotEXRuntime(registry, fusion=self.fusion())
        try:
            runtime.start()
            for _ in range(3):
                runtime.tick()

            self.assertTrue(any(entity.range_m is not None for entity in runtime.world.entities))
            self.assertIsNotNone(runtime.world.task_state["scan_frame_id"])
            self.assertFalse(runtime.world.perception_degraded)
        finally:
            runtime.stop("test complete")
            self.unregister_all(registry)

    def test_runtime_tick_continues_when_scan_provider_fails(self) -> None:
        registry = PluginRegistry()
        registry.register("vision", MockVisionProvider())
        registry.register("scan", FailingScanProvider())
        event_bus = EventBus()
        runtime = AstrBotEXRuntime(registry, event_bus=event_bus, fusion=self.fusion())
        try:
            runtime.start()
            runtime.tick()

            self.assertEqual(runtime.state, RuntimeState.RUNNING)
            self.assertEqual(runtime.world.task_state["scan_frame_id"], None)
            self.assertTrue(
                any(
                    event.type == "plugin_fault" and event.message == "scan read failed"
                    for event in event_bus.recent()
                )
            )
        finally:
            runtime.stop("test complete")
            self.unregister_all(registry)


if __name__ == "__main__":
    unittest.main()
