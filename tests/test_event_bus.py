from __future__ import annotations

import threading
import unittest

from astrbot_ex.core.event_bus import (
    LEVELS,
    MAX_THROTTLE_KEYS,
    EventBus,
    normalize_level,
    resolve_source,
)
from astrbot_ex.core.models import RuntimeEvent


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

    def test_publishes_degraded_unavailable_events_without_error_severity(self) -> None:
        bus = EventBus()
        delivered = []
        bus.subscribe(delivered.append)

        bus.emit("scan", "scan provider unavailable", severity="warning")
        bus.emit("vision", "vision provider unavailable")

        self.assertEqual(
            [event.message for event in delivered],
            ["scan provider unavailable", "vision provider unavailable"],
        )
        self.assertEqual(delivered, bus.recent())


class EventLevelNormalisationTest(unittest.TestCase):
    def test_chinese_warning_without_lifecycle_keyword_is_kept(self) -> None:
        bus = EventBus()
        bus.emit("vision.yolo", "未收到图像", severity="warning", port_id="image")

        retained = bus.recent()
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].message, "未收到图像")
        self.assertEqual(retained[0].data["log"]["level"], "WARNING")

    def test_warning_and_above_always_survive_the_filter(self) -> None:
        bus = EventBus()
        for level in ("warning", "warn", "error", "fatal", "critical"):
            bus.emit("custom.noise", "plain text", severity=level)
        self.assertEqual([event.data["log"]["level"] for event in bus.recent()],
                         ["WARNING", "WARNING", "ERROR", "CRITICAL", "CRITICAL"])

    def test_error_types_are_errors_whatever_the_producer_claims(self) -> None:
        for event_type in ("fault", "plugin_fault", "rule_rejected"):
            bus = EventBus()
            bus.emit(event_type, "something", severity="debug")
            self.assertEqual(bus.recent()[0].data["log"]["level"], "ERROR")

    def test_non_empty_error_field_forces_error(self) -> None:
        bus = EventBus()
        bus.emit("custom", "opaque", severity="debug", error="socket closed")
        self.assertEqual(bus.recent()[0].data["log"]["level"], "ERROR")

        bus = EventBus()
        bus.emit("custom", "opaque", error="   ")
        self.assertEqual(bus.recent(), [], "a blank error field is not an error")

    def test_explicit_level_aliases(self) -> None:
        cases = {"debug": "DEBUG", "trace": "DEBUG", "info": "INFO", "notice": "INFO",
                 "warn": "WARNING", "warning": "WARNING", "err": "ERROR",
                 "fatal": "CRITICAL", "bogus": "INFO"}
        for value, expected in cases.items():
            bus = EventBus()
            bus.emit("ros_diagnostic", "text", severity=value)
            self.assertEqual(bus.recent()[0].data["log"]["level"], expected, value)

    def test_level_field_is_honoured_after_severity(self) -> None:
        bus = EventBus()
        bus.emit("ros_diagnostic", "text", level="warning")
        self.assertEqual(bus.recent()[0].data["log"]["level"], "WARNING")

    def test_ros_diagnostic_recovery_info_is_kept(self) -> None:
        bus = EventBus()
        bus.emit("ros_diagnostic", "ROS_DIAGNOSTIC_RECOVERED", severity="info",
                 code="ROS_DIAGNOSTIC_RECOVERED", state="recovered")
        retained = bus.recent()
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].data["log"]["level"], "INFO")

    def test_vision_health_info_is_kept(self) -> None:
        bus = EventBus()
        bus.emit("vision_health", "inference healthy", severity="info", code="VISION_OK")
        self.assertEqual(len(bus.recent()), 1)

    def test_frame_and_tick_noise_stays_filtered(self) -> None:
        bus = EventBus()
        bus.emit("frame", "frame 41", frame_id=41)
        bus.emit("tick", "runtime tick", ticks=900)
        bus.emit("vision_plugin", "frame decoded", frame_id=42, entities=3)
        self.assertEqual(bus.recent(), [])

    def test_filtered_events_do_not_consume_a_sequence(self) -> None:
        bus = EventBus()
        bus.emit("tick", "runtime tick")
        bus.emit("frame", "frame 1")
        kept = bus.emit("fault", "boom")
        self.assertEqual(kept.data["log"]["sequence"], 1)
        self.assertEqual(kept.data["log"]["event_id"], f"{bus.stream_id}:1")
        self.assertEqual(bus.head, 1)

    def test_normalize_level_is_total(self) -> None:
        for event_type in ("fault", "plugin_fault", "rule_rejected", "environment",
                           "ros_diagnostic", "vision_health", "frame", "unknown"):
            event = RuntimeEvent(type=event_type, message="text")
            self.assertIn(normalize_level(event), LEVELS)


class EventLogMetadataTest(unittest.TestCase):
    def test_retained_events_carry_the_reserved_block(self) -> None:
        bus = EventBus(stream_id="fixed")
        event = bus.emit("fault", "boom", severity="error", plugin_id="demo")
        block = event.data["log"]
        self.assertEqual(set(block), {"level", "source", "stream_id", "sequence", "event_id"})
        self.assertEqual(block["stream_id"], "fixed")
        self.assertEqual(block["sequence"], 1)
        self.assertEqual(block["event_id"], "fixed:1")
        self.assertEqual(block["source"], "demo")

    def test_producer_cannot_forge_the_reserved_block(self) -> None:
        bus = EventBus(stream_id="fixed")
        event = bus.emit("ros_diagnostic", "text", severity="info",
                         log={"level": "CRITICAL", "event_id": "dead:999", "sequence": 999})
        block = event.data["log"]
        self.assertEqual(block["level"], "INFO")
        self.assertEqual(block["event_id"], "fixed:1")
        self.assertEqual(block["sequence"], 1)

    def test_source_prefers_explicit_source_then_identifiers(self) -> None:
        bus = EventBus()
        cases = [
            ({"source": "ros.preflight", "plugin_id": "demo"}, "ros.preflight"),
            ({"plugin_id": "demo", "environment": "ros2"}, "demo"),
            ({"plugin": "trace_plugin"}, "trace_plugin"),
            ({"environment": "ros2"}, "ros2"),
            ({}, "fault"),
        ]
        for data, expected in cases:
            bus = EventBus()
            event = bus.emit("fault", "boom", **data)
            self.assertEqual(event.data["log"]["source"], expected, data)

    def test_source_is_bounded_and_single_line(self) -> None:
        bus = EventBus()
        event = bus.emit("fault", "boom", source="\x1b[31m  bad\n\rname  " + "x" * 500)
        source = event.data["log"]["source"]
        self.assertLessEqual(len(source), 64)
        self.assertNotIn("\n", source)
        self.assertNotIn("\x1b", source)

    def test_source_never_echoes_a_secret(self) -> None:
        event = RuntimeEvent(type="fault", message="boom", data={"source": "token=abc123"})
        self.assertEqual(resolve_source(event), "token=abc123")
        # Only the identifier field is read; unrelated secret fields never reach the log.
        event = RuntimeEvent(type="fault", message="boom",
                             data={"token": "abc123", "password": "hunter2"})
        self.assertEqual(resolve_source(event), "fault")

    def test_sequences_are_strictly_increasing_per_instance(self) -> None:
        bus = EventBus()
        for index in range(5):
            bus.emit("fault", f"e{index}")
        self.assertEqual([event.data["log"]["sequence"] for event in bus.recent()], [1, 2, 3, 4, 5])

    def test_each_instance_has_its_own_stream_id(self) -> None:
        first, second = EventBus(), EventBus()
        self.assertNotEqual(first.stream_id, second.stream_id)
        self.assertEqual(first.stream_id, first.stream_id)

    def test_identical_content_keeps_distinct_ids(self) -> None:
        bus = EventBus()
        first = bus.emit("fault", "same text")
        second = bus.emit("fault", "same text")
        self.assertNotEqual(first.data["log"]["event_id"], second.data["log"]["event_id"])

    def test_recent_still_returns_runtime_events(self) -> None:
        bus = EventBus()
        bus.emit("fault", "boom")
        recent = bus.recent()
        self.assertIsInstance(recent[0], RuntimeEvent)
        self.assertEqual((recent[0].type, recent[0].message), ("fault", "boom"))


class EventBusConcurrencyTest(unittest.TestCase):
    def test_subscriber_callbacks_run_outside_the_bus_lock(self) -> None:
        bus = EventBus()
        entered = threading.Event()
        release = threading.Event()

        def blocking(event):
            entered.set()
            release.wait(2)

        bus.subscribe(blocking)
        producer = threading.Thread(target=lambda: bus.emit("fault", "boom"))
        producer.start()
        self.assertTrue(entered.wait(2), "subscriber never ran")

        observed = {}

        def read_state():
            observed["head"] = bus.head
            observed["recent"] = len(bus.recent())

        reader = threading.Thread(target=read_state)
        reader.start()
        reader.join(2)
        self.assertFalse(reader.is_alive(), "the bus lock must not be held during callbacks")
        self.assertEqual(observed, {"head": 1, "recent": 1})

        release.set()
        producer.join(2)
        self.assertFalse(producer.is_alive())

    def test_throttle_key_store_stays_bounded(self) -> None:
        bus = EventBus()
        for index in range(MAX_THROTTLE_KEYS + 200):
            bus.emit_throttled("fault", "boom", key=f"k{index}")
        self.assertLessEqual(len(bus._last_emit_at), MAX_THROTTLE_KEYS)

    def test_emit_throttled_signature_and_behaviour_are_unchanged(self) -> None:
        bus = EventBus()
        self.assertIsNotNone(bus.emit_throttled("fault", "boom", interval_sec=60))
        self.assertIsNone(bus.emit_throttled("fault", "boom", interval_sec=60))
        self.assertIsNotNone(bus.emit_throttled("fault", "boom", interval_sec=0))


if __name__ == "__main__":
    unittest.main()
