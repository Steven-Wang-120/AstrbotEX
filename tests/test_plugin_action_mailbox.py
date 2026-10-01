from __future__ import annotations

import threading
import unittest
from concurrent.futures import Future
from types import SimpleNamespace

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.plugin_actor import ActionGuardRejected, ActionMailboxFull, PluginActor


class LatchPlugin:
    id = "action_mailbox_test"

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.called = threading.Event()
        self.steps = threading.Event()
        self.calls: list[tuple[str, object]] = []

    def block(self) -> None:
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("test latch not released")

    def on_action_command(self, command: dict) -> str:
        self.calls.append(("start", command))
        self.called.set()
        return "accepted"

    def on_action_cancel(self, command_id: str, reason: str) -> str:
        self.calls.append(("cancel", command_id))
        self.called.set()
        return "requested"

    def on_worker_step(self) -> None:
        self.steps.set()


class ActionMailboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = LatchPlugin()
        self.actor = PluginActor(
            self.plugin, action_max_count=2, action_max_bytes=128,
            cancel_max_count=2, cancel_max_bytes=40,
        )
        self.actor.set_action_guard(lambda command: True)
        self.actor.start()

    def tearDown(self) -> None:
        self.plugin.release.set()
        if self.actor.alive:
            self.actor.stop(timeout=3)

    def block_actor(self) -> None:
        self.assertTrue(self.actor.cast("block"))
        self.assertTrue(self.plugin.entered.wait(2))

    def test_count_limit_keeps_existing_starts(self) -> None:
        self.block_actor()
        first = self.actor.submit_action({"n": 1})
        second = self.actor.submit_action({"n": 2})
        with self.assertRaises(ActionMailboxFull):
            self.actor.submit_action({"n": 3})
        self.assertEqual(self.actor.action_mailbox_stats()["start_count"], 2)
        self.plugin.release.set()
        self.assertEqual(first.result(2), "accepted")
        self.assertEqual(second.result(2), "accepted")
        self.assertEqual([item[1]["n"] for item in self.plugin.calls], [1, 2])
        self.assertEqual(self.actor.action_mailbox_stats()["start_bytes"], 0)

    def test_byte_limits_on_both_queues(self) -> None:
        self.block_actor()
        with self.assertRaises(ActionMailboxFull):
            self.actor.submit_action({"payload": "a" * 129})
        with self.assertRaises(ActionMailboxFull):
            self.actor.submit_action_cancel("cmd", "x" * 50)
        self.assertEqual(self.actor.action_mailbox_stats()["cancel_count"], 0)
        self.assertEqual(self.actor.action_mailbox_stats()["start_count"], 0)

    def test_cancel_priority_without_interrupting_active_handler(self) -> None:
        self.block_actor()
        first = self.actor.submit_action({"n": 1})
        cancel_a = self.actor.submit_action_cancel("a", "stop")
        cancel_b = self.actor.submit_action_cancel("b", "stop")
        with self.assertRaises(ActionMailboxFull):
            self.actor.submit_action_cancel("c", "stop")
        self.assertFalse(cancel_a.done())
        self.assertFalse(first.done())
        self.plugin.release.set()
        self.assertEqual(cancel_a.result(2), "requested")
        self.assertEqual(cancel_b.result(2), "requested")
        self.assertEqual(first.result(2), "accepted")
        self.assertEqual([kind for kind, _ in self.plugin.calls], ["cancel", "cancel", "start"])
        self.assertEqual(self.actor.action_mailbox_stats()["cancel_bytes"], 0)

    def test_execution_guard_revoked_while_queued(self) -> None:
        self.block_actor()
        future = self.actor.submit_action({"n": 1})
        self.actor.set_action_guard(lambda command: False)
        self.plugin.release.set()
        with self.assertRaises(ActionGuardRejected):
            future.result(2)
        self.assertEqual(self.plugin.calls, [])

    def test_input_is_frozen_on_enqueue(self) -> None:
        self.block_actor()
        command = {"params": {"target": ["old"]}}
        future = self.actor.submit_action(command)
        command["params"]["target"].append("changed")
        self.plugin.release.set()
        self.assertEqual(future.result(2), "accepted")
        self.assertEqual(self.plugin.calls[0][1], {"params": {"target": ["old"]}})

    def test_cancel_queued_future_refunds_capacity_and_actor_survives(self) -> None:
        self.block_actor()
        canceled = self.actor.submit_action({"n": 1})
        self.assertTrue(canceled.cancel())
        self.assertEqual(self.actor.action_mailbox_stats()["start_bytes"], 0)
        next_action = self.actor.submit_action({"n": 2})
        canceled_stop = self.actor.submit_action_cancel("old", "stop")
        self.assertTrue(canceled_stop.cancel())
        self.assertEqual(self.actor.action_mailbox_stats()["cancel_count"], 0)
        next_stop = self.actor.submit_action_cancel("new", "stop")
        self.plugin.release.set()
        self.assertEqual(next_stop.result(2), "requested")
        self.assertEqual(next_action.result(2), "accepted")
        self.assertTrue(self.actor.alive)
        self.assertEqual([item[1] for item in self.plugin.calls], ["new", {"n": 2}])

    def test_stop_fails_pending_and_keeps_truthful_alive_on_timeout(self) -> None:
        self.block_actor()
        pending_start = self.actor.submit_action({"n": 1})
        pending_cancel = self.actor.submit_action_cancel("one", "stop")
        pending_call: Future = Future()
        # A generic call must also be resolved on close, without changing its public API.
        from astrbot_ex.core.plugin_actor import _Invocation
        self.actor._mailbox.put(_Invocation("on_enable", (), {}, pending_call))
        with self.assertRaises(TimeoutError):
            self.actor.stop(timeout=0.01)
        self.assertTrue(self.actor.alive)
        for future in (pending_start, pending_cancel, pending_call):
            with self.assertRaisesRegex(RuntimeError, "stopped"):
                future.result(1)
        self.assertEqual(self.actor.action_mailbox_stats()["start_bytes"], 0)
        self.assertEqual(self.actor.action_mailbox_stats()["cancel_bytes"], 0)
        with self.assertRaises(RuntimeError):
            self.actor.submit_action({"n": 2})
        self.plugin.release.set()
        self.actor.stop(timeout=2)
        self.assertFalse(self.actor.alive)
        self.assertEqual(self.plugin.calls, [])

    def test_drain_worker_when_runtime_stopped_without_enabling_starts(self) -> None:
        self.actor.set_action_guard(None)
        self.actor.set_action_draining(True)
        self.assertTrue(self.plugin.steps.wait(2))
        with self.assertRaises(ActionGuardRejected):
            self.actor.submit_action({"n": 1}).result(2)
        self.assertEqual(self.plugin.calls, [])
        self.actor.set_action_draining(False)

    def test_raising_diagnostic_subscriber_cannot_strand_actions(self) -> None:
        bus = EventBus()
        self.plugin.context = SimpleNamespace(event_bus=bus)
        called = threading.Event()

        def raising_subscriber(event: object) -> None:
            called.set()
            raise RuntimeError("diagnostic subscriber failed")

        bus.subscribe(raising_subscriber)
        self.plugin.on_action_command = lambda command: (_ for _ in ()).throw(ValueError("action failed"))
        with self.assertRaisesRegex(ValueError, "action failed"):
            self.actor.submit_action({"n": 1}).result(2)
        self.assertFalse(called.is_set())
        self.assertEqual(self.actor.action_mailbox_stats()["last_action_fault"], "action failed")
        self.assertEqual(self.actor.submit_action_cancel("cmd", "stop").result(2), "requested")
        self.plugin.on_action_command = lambda command: "accepted"
        self.assertEqual(self.actor.submit_action({"n": 2}).result(2), "accepted")
        self.assertTrue(self.actor.alive)

    def test_slow_diagnostic_subscriber_does_not_block_action_results(self) -> None:
        bus = EventBus()
        self.plugin.context = SimpleNamespace(event_bus=bus)
        subscriber_entered = threading.Event()
        subscriber_release = threading.Event()

        def slow_subscriber(event: object) -> None:
            subscriber_entered.set()
            if not subscriber_release.wait(2):
                raise TimeoutError("diagnostic latch was not released")

        bus.subscribe(slow_subscriber)
        self.plugin.on_action_cancel = lambda command_id, reason: (_ for _ in ()).throw(ValueError("cancel failed"))
        try:
            with self.assertRaisesRegex(ValueError, "cancel failed"):
                self.actor.submit_action_cancel("cmd", "stop").result(1)
            self.assertFalse(subscriber_entered.is_set())
            self.assertEqual(self.actor.submit_action({"n": 1}).result(1), "accepted")
            self.assertFalse(subscriber_entered.is_set())
        finally:
            subscriber_release.set()

    def test_legacy_call_failure_resolves_before_slow_diagnostic(self) -> None:
        bus = EventBus()
        self.plugin.context = SimpleNamespace(event_bus=bus)
        subscriber_entered = threading.Event()
        subscriber_release = threading.Event()

        def slow_subscriber(event: object) -> None:
            subscriber_entered.set()
            if not subscriber_release.wait(2):
                raise TimeoutError("diagnostic latch was not released")

        bus.subscribe(slow_subscriber)
        self.plugin.broken = lambda: (_ for _ in ()).throw(ValueError("legacy failed"))
        try:
            with self.assertRaisesRegex(ValueError, "legacy failed"):
                self.actor.call("broken", timeout=1)
            self.assertTrue(subscriber_entered.wait(1))
        finally:
            subscriber_release.set()
        self.assertEqual(self.actor.submit_action_cancel("cmd", "stop").result(2), "requested")

    def test_legacy_diagnostic_exception_does_not_kill_actor(self) -> None:
        bus = EventBus()
        self.plugin.context = SimpleNamespace(event_bus=bus)
        bus.subscribe(lambda event: (_ for _ in ()).throw(ValueError("subscriber failed")))
        self.plugin.broken = lambda: (_ for _ in ()).throw(RuntimeError("legacy failed"))
        with self.assertRaisesRegex(RuntimeError, "legacy failed"):
            self.actor.call("broken", timeout=1)
        self.assertTrue(self.actor.alive)
        self.assertEqual(self.actor.submit_action_cancel("cmd", "stop").result(2), "requested")
        self.assertEqual(self.actor.submit_action({"n": 1}).result(2), "accepted")

    def test_configuration_limits_reject_invalid_values(self) -> None:
        invalid_ints = (True, False, 0, -1, 1.0, float("nan"), float("inf"), 10_001)
        for field in ("action_max_count", "cancel_max_count"):
            for value in invalid_ints:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    PluginActor(self.plugin, **{field: value})
        for field in ("action_max_bytes", "cancel_max_bytes"):
            for value in (True, 0, 1.0, float("inf"), 67_108_865):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    PluginActor(self.plugin, **{field: value})
        for value in (True, False, 0, -1, float("nan"), float("inf"), 60_001, "20"):
            with self.subTest(budget=value), self.assertRaises(ValueError):
                PluginActor(self.plugin, action_callback_budget_ms=value)
        self.assertEqual(PluginActor(self.plugin, action_callback_budget_ms=1).action_mailbox_stats()["start_count"], 0)

    def test_drain_and_cancel_arguments_are_strict(self) -> None:
        for value in ("false", 1, None, [], 0):
            with self.subTest(drain=value), self.assertRaises(TypeError):
                self.actor.set_action_draining(value)
        self.assertFalse(self.actor.action_mailbox_stats()["draining"])
        for command_id, reason in (("", "stop"), ("c" * 257, "stop"), (1, "stop"), ("ok", "r" * 257), ("ok", None)):
            with self.subTest(command_id=command_id, reason=reason), self.assertRaises(ValueError):
                self.actor.submit_action_cancel(command_id, reason)
        self.assertEqual(self.actor.action_mailbox_stats()["cancel_count"], 0)
        self.assertEqual(self.actor.submit_action_cancel("ok", "").result(2), "requested")

    def test_queued_legacy_future_cancel_does_not_kill_actor(self) -> None:
        from astrbot_ex.core.plugin_actor import _Invocation

        self.block_actor()
        canceled: Future = Future()
        self.actor._mailbox.put(_Invocation("on_enable", (), {}, canceled))
        self.assertTrue(canceled.cancel())
        self.plugin.release.set()
        self.assertEqual(self.actor.submit_action({"n": 1}).result(2), "accepted")
        self.assertTrue(self.actor.alive)

    def test_guard_exception_prevents_plugin_invocation(self) -> None:
        self.actor.set_action_guard(lambda command: (_ for _ in ()).throw(ValueError("revoked")))
        with self.assertRaisesRegex(ValueError, "revoked"):
            self.actor.submit_action({"n": 1}).result(2)
        self.assertEqual(self.plugin.calls, [])

    def test_active_future_cannot_interrupt_callback_and_stats_are_readable(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def active_callback(command: dict) -> str:
            entered.set()
            self.assertTrue(release.wait(2))
            return "accepted"

        self.plugin.on_action_command = active_callback
        future = self.actor.submit_action({"n": 1})
        try:
            self.assertTrue(entered.wait(2))
            self.assertFalse(future.cancel())
            self.assertTrue(self.actor.alive)
            self.assertEqual(self.actor.action_mailbox_stats()["start_count"], 0)
        finally:
            release.set()
        self.assertEqual(future.result(2), "accepted")

    def test_missing_fixed_callback_fails_explicitly(self) -> None:
        self.plugin.on_action_command = None
        with self.assertRaisesRegex(RuntimeError, "missing on_action_command"):
            self.actor.submit_action({"n": 1}).result(2)

    def test_callback_budget_diagnostic(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def bounded_callback(command: dict) -> str:
            entered.set()
            self.assertTrue(release.wait(2))
            return "accepted"

        self.plugin.on_action_command = bounded_callback
        self.actor._action_callback_budget_ms = 0.00001
        future = self.actor.submit_action({"n": 1})
        self.assertTrue(entered.wait(2))
        release.set()
        self.assertEqual(future.result(2), "accepted")
        stats = self.actor.action_mailbox_stats()
        self.assertGreaterEqual(stats["action_budget_exceeded"], 1)
        self.assertIsNotNone(stats["last_action_duration_ms"])


if __name__ == "__main__":
    unittest.main()
