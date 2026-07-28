from __future__ import annotations

import unittest

from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.runtime import AstrBotEXRuntime
from astrbot_ex.plugins.mock import (
    ApproachEntitySkill,
    BasicRulePlugin,
    MockMotionBridge,
    MockVisionProvider,
    NearestEntityPolicy,
)


class RuntimeActorIntegrationTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
