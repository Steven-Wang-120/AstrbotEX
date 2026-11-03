"""B03: bounded, ordered log stream over the EventBus ring.

The stream is the only place that decides delivery order. These tests prove the
three things a log console depends on: nothing is lost at the open boundary,
nothing is delivered twice, and an unavoidable hole is always visible as a
control frame rather than a silent jump.
"""
from __future__ import annotations

import json
import threading
import unittest

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.event_stream import (
    DETAIL_LIMIT,
    MAX_LIVE_FRAMES,
    PROJECTION_BUDGET,
    TRUNCATION_MARK,
    EventStream,
    control_frame,
    estimate_frame_bytes,
    event_frame,
    parse_cursor,
    project_event,
)
from astrbot_ex.core.models import RuntimeEvent


class StubBus:
    """Minimal bus stand-in so ordering can be driven directly, out of order."""

    def __init__(self):
        self.detached = []

    def _detach(self, callback):
        self.detached.append(callback)


def stamped(sequence, stream_id="s", message=None, level="INFO", **data):
    event = RuntimeEvent(type="probe", message=message or f"m{sequence}", data=dict(data))
    event.data["log"] = {"level": level, "source": "probe", "stream_id": stream_id,
                         "sequence": sequence, "event_id": f"{stream_id}:{sequence}"}
    return event


def stream_for(head=0, history=(), controls=(), **kwargs):
    return EventStream(StubBus(), stream_id="s", head=head,
                       history=list(history), controls=list(controls), **kwargs)


def drain(stream, timeout=0.15, limit=5000):
    frames = []
    while len(frames) < limit:
        frame = stream.get(timeout=timeout)
        if frame is None:
            break
        frames.append(frame)
    return frames


def sequences(frames):
    return [frame["event"].data["log"]["sequence"] for frame in frames if frame["kind"] == "event"]


def controls(frames):
    return [frame["control"] for frame in frames if frame["kind"] == "control"]


class CursorTest(unittest.TestCase):
    def test_accepts_a_well_formed_cursor(self):
        self.assertEqual(parse_cursor("abc:12"), ("abc", 12))
        self.assertEqual(parse_cursor("  abc:12  "), ("abc", 12))
        self.assertEqual(parse_cursor("a:b:7"), ("a:b", 7))

    def test_absent_cursor_means_no_replay(self):
        self.assertIsNone(parse_cursor(None))
        self.assertIsNone(parse_cursor("   "))

    def test_rejects_malformed_cursors(self):
        for value in ("abc", "abc:", ":12", "abc:0", "abc:-1", "abc:1.5", "abc:12x",
                      "abc: 12", "abc:+12", "abc:١٢", 12, b"abc:12", ["abc:12"]):
            with self.assertRaises(ValueError, msg=repr(value)):
                parse_cursor(value)

    def test_rejects_overlong_cursor(self):
        with self.assertRaises(ValueError):
            parse_cursor("s" * 130 + ":1")
        self.assertEqual(parse_cursor("s" * 120 + ":1"), ("s" * 120, 1))


class FrameShapeTest(unittest.TestCase):
    def test_event_frame_shape(self):
        frame = event_frame(stamped(3))
        self.assertEqual(set(frame), {"kind", "event", "event_id"})
        self.assertEqual(frame["kind"], "event")
        self.assertIsInstance(frame["event"], RuntimeEvent)
        self.assertEqual(frame["event_id"], "s:3")

    def test_control_frame_shape(self):
        frame = control_frame("gap", "queue_overflow", stream_id="s", oldest_sequence=4,
                              newest_sequence=9, dropped_count=3)
        self.assertEqual(set(frame), {"kind", "reason", "stream_id", "oldest_sequence",
                                      "newest_sequence", "dropped_count"})
        self.assertEqual(frame["kind"], "gap")

    def test_control_frame_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            control_frame("recovered", "x", stream_id="s", oldest_sequence=None,
                          newest_sequence=None, dropped_count=None)


class StreamOrderingTest(unittest.TestCase):
    def test_out_of_order_arrival_is_released_in_sequence_order(self):
        stream = stream_for(reorder_grace=0.05)
        for sequence in (3, 1, 5, 2, 4):
            stream._on_event(stamped(sequence))
        self.assertEqual(sequences(drain(stream)), [1, 2, 3, 4, 5])
        self.assertEqual(controls(drain(stream)), [])

    def test_duplicate_sequences_are_delivered_once(self):
        stream = stream_for()
        for sequence in (1, 1, 2, 2, 2):
            stream._on_event(stamped(sequence))
        frames = drain(stream)
        self.assertEqual(sequences(frames), [1, 2])
        self.assertEqual(len(frames), 2)

    def test_identical_content_with_distinct_ids_is_kept(self):
        stream = stream_for()
        stream._on_event(stamped(1, message="same text"))
        stream._on_event(stamped(2, message="same text"))
        self.assertEqual(sequences(drain(stream)), [1, 2])

    def test_events_below_the_snapshot_head_are_ignored(self):
        stream = stream_for(head=4)
        stream._on_event(stamped(3))          # already part of history
        stream._on_event(stamped(5))
        self.assertEqual(sequences(drain(stream)), [5])

    def test_a_permanent_hole_becomes_a_visible_gap(self):
        stream = stream_for(reorder_grace=0.05)
        stream._on_event(stamped(1))
        stream._on_event(stamped(3))
        frames = drain(stream)
        self.assertEqual(sequences(frames), [1, 3])
        gaps = controls(frames)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["kind"], "gap")
        self.assertEqual(gaps[0]["reason"], "sequence_hole")
        self.assertIsNone(gaps[0]["dropped_count"], "an unknown drop count must stay null")

    def test_a_hole_within_the_grace_is_not_reported(self):
        stream = stream_for(reorder_grace=1.0)
        stream._on_event(stamped(1))
        stream._on_event(stamped(3))
        self.assertEqual(stream.get(timeout=0.05)["event"].message, "m1")
        stream._on_event(stamped(2))
        frames = drain(stream, timeout=0.1)
        self.assertEqual(sequences(frames), [2, 3])
        self.assertEqual(controls(frames), [])

    def test_frame_budget_overflow_drops_oldest_with_an_exact_count(self):
        stream = stream_for(max_frames=3)
        for sequence in range(1, 7):
            stream._on_event(stamped(sequence))
        frames = drain(stream)
        self.assertEqual(sequences(frames), [4, 5, 6])
        gaps = controls(frames)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["reason"], "queue_overflow")
        self.assertEqual(gaps[0]["dropped_count"], 3)
        self.assertEqual(gaps[0]["oldest_sequence"], 4)
        self.assertEqual(gaps[0]["newest_sequence"], 6)

    def test_byte_budget_overflow_drops_oldest(self):
        stream = stream_for(max_bytes=1000)
        for sequence in range(1, 4):
            stream._on_event(stamped(sequence, message="x" * 400))
        frames = drain(stream)
        self.assertEqual(sequences(frames), [3])
        self.assertEqual(controls(frames)[0]["dropped_count"], 2)

    def test_default_budget_matches_the_contract(self):
        stream = stream_for()
        self.assertEqual(stream.max_frames, MAX_LIVE_FRAMES)
        self.assertEqual(stream.max_bytes, 2 * 1024 * 1024)

    def test_closed_stream_ignores_late_events(self):
        stream = stream_for()
        stream.close()
        stream._on_event(stamped(1))
        self.assertIsNone(stream.get(timeout=0.01))
        self.assertEqual(stream.stats()["buffered"], 0)


class OpenStreamTest(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus(stream_id="bus-a")

    def test_without_a_cursor_only_the_recent_tail_is_replayed(self):
        for index in range(80):
            self.bus.emit("fault", f"e{index}")
        stream = self.bus.open_stream()
        self.addCleanup(stream.close)
        frames = stream.recent()
        self.assertEqual(len(frames), 50)
        self.assertEqual([frame["event"].data["log"]["sequence"] for frame in frames],
                         list(range(31, 81)))
        self.assertEqual(controls(frames), [])

    def test_a_valid_cursor_replays_everything_after_it(self):
        for index in range(10):
            self.bus.emit("fault", f"e{index}")
        stream = self.bus.open_stream("bus-a:4")
        self.addCleanup(stream.close)
        self.assertEqual([frame["event"].data["log"]["sequence"] for frame in stream.recent()],
                         [5, 6, 7, 8, 9, 10])

    def test_a_cursor_at_head_replays_nothing(self):
        for index in range(3):
            self.bus.emit("fault", f"e{index}")
        stream = self.bus.open_stream("bus-a:3")
        self.addCleanup(stream.close)
        self.assertEqual(stream.recent(), [])

    def test_a_cursor_beyond_head_resets_instead_of_waiting(self):
        for index in range(3):
            self.bus.emit("fault", f"e{index}")
        stream = self.bus.open_stream("bus-a:99")
        self.addCleanup(stream.close)
        control = controls(drain(stream))[0]
        self.assertEqual((control["kind"], control["reason"]), ("reset", "cursor_ahead"))
        self.assertIsNone(control["dropped_count"])

    def test_a_cursor_from_another_instance_resets(self):
        for index in range(3):
            self.bus.emit("fault", f"e{index}")
        stream = self.bus.open_stream("other:2")
        self.addCleanup(stream.close)
        control = controls(drain(stream))[0]
        self.assertEqual((control["kind"], control["reason"]), ("reset", "cross_stream"))
        self.assertEqual(control["stream_id"], "bus-a")
        self.assertEqual(len(stream.recent()), 3, "a reset still replays what is retained")

    def test_an_expired_cursor_reports_an_exact_gap(self):
        bus = EventBus(max_events=5, stream_id="ring")
        for index in range(10):
            bus.emit("fault", f"e{index}")
        stream = bus.open_stream("ring:2")
        self.addCleanup(stream.close)
        control = controls(drain(stream))[0]
        self.assertEqual((control["kind"], control["reason"]), ("gap", "history_expired"))
        self.assertEqual(control["dropped_count"], 3)      # sequences 3, 4, 5 are gone
        self.assertEqual(control["oldest_sequence"], 6)
        self.assertEqual(control["newest_sequence"], 10)

    def test_a_contiguous_cursor_reports_no_gap(self):
        bus = EventBus(max_events=5, stream_id="ring")
        for index in range(10):
            bus.emit("fault", f"e{index}")
        stream = bus.open_stream("ring:5")     # 5 is the last one before the ring start
        self.addCleanup(stream.close)
        self.assertEqual(controls(drain(stream)), [])
        self.assertEqual([frame["event"].data["log"]["sequence"] for frame in stream.recent()],
                         [6, 7, 8, 9, 10])

    def test_malformed_cursor_raises(self):
        for value in ("bus-a", "bus-a:0", "bus-a:x", "x" * 200 + ":1"):
            with self.assertRaises(ValueError, msg=repr(value)):
                self.bus.open_stream(value)

    def test_live_events_follow_the_history(self):
        self.bus.emit("fault", "before")
        stream = self.bus.open_stream("bus-a:1")
        self.addCleanup(stream.close)
        self.bus.emit("fault", "after")
        frame = stream.get(timeout=0.5)
        self.assertEqual(frame["kind"], "event")
        self.assertEqual(frame["event"].message, "after")
        self.assertEqual(frame["event_id"], "bus-a:2")
        self.assertEqual(stream.get(timeout=0.01), None)

    def test_close_is_idempotent_and_detaches(self):
        stream = self.bus.open_stream()
        self.assertEqual(len(self.bus._subscribers), 1)
        stream.close()
        stream.close()
        self.assertEqual(self.bus._subscribers, [])
        self.bus.emit("fault", "after close")
        self.assertIsNone(stream.get(timeout=0.01))
        self.assertTrue(stream.closed)

    def test_history_frames_survive_repeated_reads(self):
        self.bus.emit("fault", "one")
        stream = self.bus.open_stream()
        self.addCleanup(stream.close)
        first = stream.recent()
        second = stream.recent()
        self.assertEqual([frame["event_id"] for frame in first],
                         [frame["event_id"] for frame in second])
        self.assertIsNot(first, second)


class StreamConcurrencyTest(unittest.TestCase):
    def test_open_snapshot_and_emit_never_lose_the_boundary_event(self):
        bus = EventBus(stream_id="bus-b")
        for index in range(20):
            bus.emit("fault", f"pre{index}")
        barrier = threading.Barrier(3)
        opened = {}

        def opener():
            barrier.wait()
            opened["stream"] = bus.open_stream()

        def emitter():
            barrier.wait()
            for index in range(20):
                bus.emit("fault", f"live{index}")

        def reader():
            barrier.wait()
            bus.recent(50)

        threads = [threading.Thread(target=opener), threading.Thread(target=emitter),
                   threading.Thread(target=reader)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())

        stream = opened["stream"]
        self.addCleanup(stream.close)
        head = bus.head
        history = [frame["event"].data["log"]["sequence"] for frame in stream.recent()]
        live = sequences(drain(stream, timeout=0.2))
        delivered = history + live
        self.assertEqual(delivered, list(range(1, head + 1)))
        self.assertEqual(len(set(delivered)), len(delivered), "no event may be delivered twice")

    def test_reverse_order_callbacks_from_many_threads_stay_ordered(self):
        stream = stream_for(reorder_grace=1.0)
        events = [stamped(sequence) for sequence in range(1, 101)]
        chunks = [events[index:index + 25] for index in range(0, 100, 25)]
        barrier = threading.Barrier(len(chunks))

        def feed(chunk):
            barrier.wait()
            for event in reversed(chunk):
                stream._on_event(event)

        threads = [threading.Thread(target=feed, args=(chunk,)) for chunk in chunks]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())

        frames = drain(stream, timeout=0.2)
        self.assertEqual(sequences(frames), list(range(1, 101)))
        self.assertEqual(controls(frames), [])

    def test_close_racing_emit_does_not_deadlock(self):
        bus = EventBus()
        stream = bus.open_stream()
        errors = []

        def churn():
            try:
                for index in range(300):
                    bus.emit("fault", f"x{index}")
            except Exception as exc:                     # pragma: no cover - failure path
                errors.append(exc)

        worker = threading.Thread(target=churn)
        worker.start()
        stream.close()
        worker.join(5)
        self.assertFalse(worker.is_alive(), "emit must not block on a closing stream")
        self.assertEqual(errors, [])
        self.assertEqual(bus._subscribers, [])

    def test_many_streams_each_get_their_own_copy(self):
        bus = EventBus()
        streams = [bus.open_stream() for _ in range(5)]
        for stream in streams:
            self.addCleanup(stream.close)
        bus.emit("fault", "shared")
        for stream in streams:
            self.assertEqual(stream.get(timeout=0.5)["event"].message, "shared")


class ProjectionTest(unittest.TestCase):
    def test_projection_keeps_the_event_id(self):
        event = stamped(7, message="m" * (DETAIL_LIMIT * 2))
        projected = project_event(event)
        self.assertEqual(projected["event_id"], "s:7")
        self.assertEqual(projected["log"]["event_id"], "s:7")
        self.assertIn(TRUNCATION_MARK, projected["message"])
        self.assertLessEqual(len(projected["message"]), DETAIL_LIMIT + len(TRUNCATION_MARK))

    def test_projection_never_mutates_the_retained_event(self):
        payload = {"image": "x" * 10_000, "nested": {"k": "y" * 10_000}}
        event = stamped(1, **payload)
        before = json.dumps(event.data, ensure_ascii=False, default=str)
        project_event(event)
        self.assertEqual(json.dumps(event.data, ensure_ascii=False, default=str), before)

    def test_projection_is_bounded_and_json_safe(self):
        event = stamped(2, blob="z" * 200_000, items=list(range(500)))
        projected = project_event(event, budget=4096)
        text = json.dumps(projected, ensure_ascii=False, default=str)
        self.assertLessEqual(len(text), 4096 + 512)
        self.assertEqual(projected["event_id"], "s:2")

    def test_estimate_is_capped(self):
        event = stamped(1, blob="z" * 500_000)
        self.assertEqual(estimate_frame_bytes(event), PROJECTION_BUDGET)
        self.assertLessEqual(estimate_frame_bytes(stamped(1)), PROJECTION_BUDGET)


if __name__ == "__main__":
    unittest.main()
