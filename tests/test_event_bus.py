from __future__ import annotations

import unittest

from astrbot_ex.core.event_bus import EventBus


class EventBusLogFilterTest(unittest.TestCase):
    def test_filters_high_frequency_runtime_noise(self) -> None:
        bus = EventBus()
        delivered = []
        bus.subscribe(delivered.append)

        bus.emit("vision_plugin", "ipc vision packet received", frame_id=1, entities=2)
        bus.emit("trace", "demo trace tick", ticks=20)
        bus.emit("motion", "intent sent", status="running")

        self.assertEqual(delivered, [])
        self.assertEqual(bus.recent(), [])

    def test_publishes_lifecycle_and_errors(self) -> None:
        bus = EventBus()
        delivered = []
        bus.subscribe(delivered.append)

        bus.emit("plugin", "demo trace plugin loaded", plugin="demo_trace_plugin")
        bus.emit("runtime_state", "runtime started", state="running")
        bus.emit("vision_plugin", "ipc vision packet parse failed", severity="error", error="bad json")
        bus.emit("plugin_fault", "plugin worker failed", plugin="demo_trace_plugin", error="boom")

        self.assertEqual([event.message for event in delivered], [
            "demo trace plugin loaded",
            "runtime started",
            "ipc vision packet parse failed",
            "plugin worker failed",
        ])
        self.assertEqual(delivered, bus.recent())


if __name__ == "__main__":
    unittest.main()
