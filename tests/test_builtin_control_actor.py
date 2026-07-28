from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.local_plugins import LocalPluginManager
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.runtime import AstrBotEXRuntime
from astrbot_ex.core.topic_bus import TopicBus


class BuiltinControlActorTest(unittest.TestCase):
    def test_dry_run_can_plugin_uses_only_its_actor_thread(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        registry = PluginRegistry()
        event_bus = EventBus()
        topic_bus = TopicBus()

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = LocalPluginManager(
                plugins_root=project_root / "plugins",
                state_path=Path(temp_dir) / "plugins_state.json",
                registry=registry,
                event_bus=event_bus,
                topic_bus=topic_bus,
            )
            manager.discover()
            manager.set_enabled("rescue_topic_can_controller_plugin", True)
            runtime = AstrBotEXRuntime(registry, event_bus=event_bus, topic_bus=topic_bus)
            try:
                runtime.start()
                runtime.tick()
                time.sleep(0.03)

                plugin_threads = {
                    thread.name
                    for thread in threading.enumerate()
                    if thread.name.startswith("astrbotex-plugin-rescue_topic_can_controller_plugin")
                    or thread.name == "astrbotex-can-rx"
                }
                self.assertEqual(
                    plugin_threads,
                    {"astrbotex-plugin-rescue_topic_can_controller_plugin"},
                )
            finally:
                runtime.stop("test complete")
                manager.set_enabled("rescue_topic_can_controller_plugin", False)


if __name__ == "__main__":
    unittest.main()
