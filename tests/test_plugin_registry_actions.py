from __future__ import annotations

import threading
import unittest

from astrbot_ex.core.plugin_actor import ActionGuardRejected
from astrbot_ex.core.plugin_registry import PluginRegistry


class MockRos:
    def __init__(self) -> None:
        self.closed = False
        self.actor = None
        self._hook_local = threading.local()

    def close(self) -> None:
        self.closed = True


class ActionPlugin:
    id = "device"

    def __init__(self) -> None:
        self.events: list[str] = []
        self._astrbotex_ros = MockRos()

    def on_load(self) -> None:
        self.events.append("load")

    def on_enable(self) -> None:
        self.events.append("enable")

    def on_runtime_start(self) -> None:
        self.events.append("runtime_start")

    def on_runtime_stop(self, reason: str) -> None:
        self.events.append("runtime_stop")

    def on_disable(self) -> None:
        self.events.append("disable")

    def on_unload(self) -> None:
        self.events.append("unload")

    def on_worker_step(self) -> None:
        pass

    def on_action_cancel(self, command_id: str, reason: str) -> str:
        return "cancel queued"


class PluginRegistryActionTest(unittest.TestCase):
    def test_generation_binding_before_load_and_failed_generation_not_reused(self) -> None:
        registry = PluginRegistry()
        first = ActionPlugin()
        bindings: list[tuple[int, bool]] = []

        def bind(slot):
            bindings.append((slot.generation, "load" not in slot.plugin.events))
            slot.plugin.bound_generation = slot.generation

        def fail(slot):
            bind(slot)
            raise ValueError("binding failed")

        registry.set_action_lifecycle_guard(lambda slot, reason: True)
        with self.assertRaisesRegex(ValueError, "binding failed"):
            registry.register("action", first, before_load=fail)
        self.assertIsNone(registry.get(first.id))
        replacement = ActionPlugin()
        slot = registry.register("action", replacement, before_load=bind)
        self.assertEqual(bindings, [(1, True), (2, True)])
        self.assertEqual(replacement.bound_generation, slot.generation)
        self.assertEqual(slot.generation, 2)
        registry.set_action_lifecycle_guard(lambda slot, reason: True)
        registry.unregister(slot.id)

    def test_failed_load_without_proof_retains_blocked_instance(self) -> None:
        registry = PluginRegistry()
        plugin = ActionPlugin()

        def fail(slot):
            raise ValueError("binding failed")

        with self.assertRaisesRegex(RuntimeError, "not proven"):
            registry.register("action", plugin, before_load=fail)
        old = registry.get(plugin.id)
        self.assertEqual(old.generation, 1)
        self.assertEqual(old.state, "blocked")
        self.assertTrue(old.actor.alive)
        self.assertFalse(plugin._astrbotex_ros.closed)
        with self.assertRaises(ValueError):
            registry.register("action", ActionPlugin())
        registry.set_action_lifecycle_guard(lambda slot, reason: True)
        registry.unregister(plugin.id)
        replacement = registry.register("action", ActionPlugin())
        self.assertEqual(replacement.generation, 2)
        registry.unregister(plugin.id)

    def test_concurrent_registration_reserves_owner_before_loading(self) -> None:
        registry = PluginRegistry()
        entered = threading.Event()
        release = threading.Event()
        first = ActionPlugin()
        outcome: list[object] = []

        def bind(slot):
            entered.set()
            release.wait(2)

        thread = threading.Thread(target=lambda: outcome.append(registry.register("action", first, before_load=bind)))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(registry.get(first.id).state, "loading")
            self.assertIsNone(registry.get_slot("action"))
            with self.assertRaisesRegex(ValueError, "already registered"):
                registry.register("action", ActionPlugin())
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertEqual(first.events[:2], ["load", "enable"])
        registry.set_action_lifecycle_guard(lambda slot, reason: True)
        registry.unregister(first.id)

    def test_blocked_unload_retains_actor_ros_and_allows_proof_retry(self) -> None:
        registry = PluginRegistry()
        plugin = ActionPlugin()
        slot = registry.register("action", plugin)
        registry.start_runtime()
        with self.assertRaisesRegex(RuntimeError, "not proven"):
            registry.unregister(plugin.id)
        self.assertIs(registry.get(plugin.id), slot)
        self.assertIsNone(registry.get_slot("action"))
        self.assertEqual(slot.state, "blocked")
        self.assertTrue(slot.stop_error)
        self.assertTrue(slot.actor.alive)
        self.assertFalse(plugin._astrbotex_ros.closed)
        self.assertNotIn("unload", plugin.events)
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register("action", ActionPlugin())
        self.assertEqual(slot.actor.submit_action_cancel("cmd", "stop").result(1), "cancel queued")
        registry.set_action_lifecycle_guard(lambda item, reason: item is slot and reason == "plugin unregistered")
        registry.unregister(plugin.id)
        self.assertIsNone(registry.get(plugin.id))
        self.assertTrue(plugin._astrbotex_ros.closed)
        self.assertFalse(slot.actor.alive)

    def test_disable_retries_and_cancel_drain_with_runtime_stopped(self) -> None:
        registry = PluginRegistry()
        plugin = ActionPlugin()
        slot = registry.register("action", plugin)
        registry.start_runtime()
        registry.set_action_lifecycle_guard(lambda slot, reason: False)
        with self.assertRaisesRegex(RuntimeError, "not proven"):
            registry.disable(plugin.id)
        self.assertEqual(slot.state, "blocked")
        self.assertTrue(slot.actor.action_mailbox_stats()["draining"])
        self.assertEqual(slot.actor.submit_action_cancel("cmd", "stop").result(1), "cancel queued")
        registry.set_action_lifecycle_guard(lambda slot, reason: True)
        registry.disable(plugin.id)
        self.assertFalse(slot.enabled)
        self.assertFalse(slot.actor.action_mailbox_stats()["draining"])
        registry.stop_runtime("runtime stopped")
        registry.unregister(plugin.id)

    def test_runtime_stop_revokes_all_before_waiting_for_proof(self) -> None:
        registry = PluginRegistry()
        first = ActionPlugin()
        second = ActionPlugin()
        second.id = "second"
        first_slot = registry.register("action", first)
        second_slot = registry.register("action", second)
        registry.start_runtime()
        entered = threading.Event()
        release = threading.Event()

        def proof(slot, reason):
            if slot is first_slot:
                entered.set()
                release.wait(2)
                return False
            return False

        registry.set_action_lifecycle_guard(proof)
        errors: list[Exception] = []

        def stop():
            try:
                registry.stop_runtime("stopping")
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=stop)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertIs(registry.get(second.id), second_slot)
            self.assertEqual(len(registry.list()), 2)
            self.assertIsNone(registry.get_slot("action"))
            self.assertEqual(second_slot.state, "stopping")
            self.assertTrue(second_slot.actor.action_mailbox_stats()["draining"])
        finally:
            release.set()
            thread.join(3)
        self.assertTrue(errors)
        self.assertEqual(first_slot.state, "blocked")
        self.assertEqual(second_slot.state, "blocked")
        self.assertNotIn("runtime_stop", first.events)
        self.assertNotIn("runtime_stop", second.events)
        registry.set_action_lifecycle_guard(lambda slot, reason: True)
        registry.stop_runtime("retry")
        self.assertIn("runtime_stop", first.events)
        self.assertIn("runtime_stop", second.events)
        registry.unregister(first.id)
        registry.unregister(second.id)


class LifecycleRaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = PluginRegistry()
        self.registry.set_action_lifecycle_guard(lambda slot, reason: True)
        self.plugin = ActionPlugin()
        self.threads: list[threading.Thread] = []
        self.errors: list[Exception] = []

    def tearDown(self) -> None:
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), thread.name)
        slot = self.registry.get(self.plugin.id)
        if slot is not None:
            self.registry.unregister(self.plugin.id)
        self.assertFalse(any(thread.name == "astrbotex-plugin-device" for thread in threading.enumerate()))
        self.assertEqual(self.errors, [])

    def spawn(self, name: str, callback) -> threading.Thread:
        def run() -> None:
            try:
                callback()
            except Exception as exc:
                self.errors.append(exc)

        thread = threading.Thread(target=run, name=name)
        self.threads.append(thread)
        thread.start()
        return thread

    def test_start_stop_callback_is_not_lost(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        original_start = self.plugin.on_runtime_start

        def slow_start() -> None:
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test start gate")
            original_start()

        self.plugin.on_runtime_start = slow_start
        slot = self.registry.register("action", self.plugin)
        try:
            start = self.spawn("start", self.registry.start_runtime)
            self.assertTrue(entered.wait(2))
            stop = self.spawn("stop", lambda: self.registry.stop_runtime("race"))
            self.assertTrue(self._wait_for(lambda: slot.state == "stopping"))
            self.assertIsNone(self.registry.get_slot("action"))
            with self.assertRaisesRegex(RuntimeError, "stop in progress"):
                self.registry.start_runtime()
        finally:
            release.set()
        start.join(3)
        stop.join(3)
        self.assertEqual(self.plugin.events.count("runtime_start"), 1)
        self.assertEqual(self.plugin.events.count("runtime_stop"), 1)
        self.assertFalse(slot.actor._runtime_active)
        self.assertEqual(slot.state, "ready")

    def test_enable_stop_revokes_pending_runtime_start(self) -> None:
        slot = self.registry.register("action", self.plugin, enabled=False)
        self.registry.start_runtime()
        entered = threading.Event()
        release = threading.Event()
        original_start = self.plugin.on_runtime_start

        def slow_start() -> None:
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test enable gate")
            original_start()

        self.plugin.on_runtime_start = slow_start
        try:
            enable = self.spawn("enable", lambda: self.registry.enable(self.plugin.id))
            self.assertTrue(entered.wait(2))
            stop = self.spawn("stop", lambda: self.registry.stop_runtime("race"))
            self.assertTrue(self._wait_for(lambda: slot.state == "stopping"))
        finally:
            release.set()
        enable.join(3)
        stop.join(3)
        self.assertEqual(self.plugin.events.count("runtime_start"), 1)
        self.assertEqual(self.plugin.events.count("runtime_stop"), 1)
        self.assertFalse(slot.actor._runtime_active)
        self.assertTrue(slot.enabled)
        self.assertNotIn("disable", self.plugin.events)
        self.assertEqual(slot.state, "ready")

    def test_unregister_waits_for_start_and_stops_before_unload(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        original_start = self.plugin.on_runtime_start

        def slow_start() -> None:
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test unregister gate")
            original_start()

        self.plugin.on_runtime_start = slow_start
        slot = self.registry.register("action", self.plugin)
        try:
            start = self.spawn("start", self.registry.start_runtime)
            self.assertTrue(entered.wait(2))
            teardown = self.spawn("unregister", lambda: self.registry.unregister(self.plugin.id))
            self.assertTrue(self._wait_for(lambda: slot.teardown_requested))
            self.assertIs(self.registry.get(self.plugin.id), slot)
            self.assertNotIn("unload", self.plugin.events)
        finally:
            release.set()
        start.join(3)
        teardown.join(3)
        self.assertIsNone(self.registry.get(self.plugin.id))
        self.assertEqual(self.plugin.events.count("runtime_stop"), 1)
        self.assertLess(self.plugin.events.index("runtime_stop"), self.plugin.events.index("unload"))
        self.assertFalse(slot.actor._runtime_active)
        self.assertFalse(slot.actor.alive)

    def test_disable_during_enable_waits_and_stops_before_disable(self) -> None:
        slot = self.registry.register("action", self.plugin, enabled=False)
        self.registry.start_runtime()
        entered = threading.Event()
        release = threading.Event()
        original_enable = self.plugin.on_enable

        def slow_enable() -> None:
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test enable gate")
            original_enable()

        self.plugin.on_enable = slow_enable
        try:
            enable = self.spawn("enable", lambda: self.registry.enable(self.plugin.id))
            self.assertTrue(entered.wait(2))
            disable = self.spawn("disable", lambda: self.registry.disable(self.plugin.id))
            self.assertTrue(self._wait_for(lambda: slot.stop_requested))
        finally:
            release.set()
        enable.join(3)
        disable.join(3)
        self.assertFalse(slot.enabled)
        self.assertEqual(slot.state, "ready")
        self.assertNotIn("runtime_start", self.plugin.events)
        self.assertEqual(self.plugin.events.count("disable"), 1)

    def test_uncertain_stop_keeps_worker_and_old_owner_blocked(self) -> None:
        steps = 0
        progressed = threading.Event()

        def worker_step() -> None:
            nonlocal steps
            steps += 1
            if steps > 1:
                progressed.set()

        self.plugin.on_worker_step = worker_step
        slot = self.registry.register("action", self.plugin)
        self.registry.start_runtime()
        self.registry.set_action_lifecycle_guard(lambda item, reason: False)
        with self.assertRaisesRegex(RuntimeError, "not proven"):
            self.registry.stop_runtime("uncertain")
        self.assertTrue(progressed.wait(2))
        self.assertEqual(slot.state, "blocked")
        self.assertTrue(slot.actor.alive)
        self.assertFalse(self.plugin._astrbotex_ros.closed)
        self.assertNotIn("unload", self.plugin.events)
        self.assertIsNone(self.registry.get_slot("action"))
        self.registry.set_action_lifecycle_guard(lambda item, reason: True)
        self.registry.stop_runtime("retry")
        self.assertEqual(self.plugin.events.count("runtime_stop"), 1)

    def test_unregister_during_registration_retains_owner_until_teardown(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def before_load(slot) -> None:
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test registration gate")

        try:
            registration = self.spawn(
                "register", lambda: self.registry.register("action", self.plugin, before_load=before_load)
            )
            self.assertTrue(entered.wait(2))
            slot = self.registry.get(self.plugin.id)
            teardown = self.spawn("unregister", lambda: self.registry.unregister(self.plugin.id))
            self.assertTrue(self._wait_for(lambda: slot.teardown_requested))
            self.assertIs(self.registry.get(self.plugin.id), slot)
            self.assertFalse(self.plugin._astrbotex_ros.closed)
        finally:
            release.set()
        registration.join(3)
        teardown.join(3)
        self.assertIsNone(self.registry.get(self.plugin.id))
        self.assertEqual(self.plugin.events.count("unload"), 1)
        self.assertFalse(slot.actor.alive)

    def test_runtime_stop_during_registration_does_not_publish_stale_start(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        self.registry.start_runtime()

        def before_load(slot) -> None:
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test registration gate")

        try:
            registration = self.spawn(
                "register", lambda: self.registry.register("action", self.plugin, before_load=before_load)
            )
            self.assertTrue(entered.wait(2))
            slot = self.registry.get(self.plugin.id)
            stop = self.spawn("stop", lambda: self.registry.stop_runtime("during load"))
            self.assertTrue(self._wait_for(lambda: slot.stop_requested))
        finally:
            release.set()
        registration.join(3)
        stop.join(3)
        self.assertEqual(self.plugin.events.count("runtime_start"), 0)
        self.assertEqual(slot.state, "ready")
        self.assertFalse(slot.actor._runtime_active)

    def test_failed_stop_proof_after_raced_start_retains_owner(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        original_start = self.plugin.on_runtime_start

        def slow_start() -> None:
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test proof gate")
            original_start()

        self.plugin.on_runtime_start = slow_start
        slot = self.registry.register("action", self.plugin)
        self.registry.set_action_lifecycle_guard(lambda item, reason: False)
        try:
            start = self.spawn("start", self.registry.start_runtime)
            self.assertTrue(entered.wait(2))
            stop = self.spawn("stop", lambda: self.registry.stop_runtime("unproven"))
            self.assertTrue(self._wait_for(lambda: slot.stop_requested))
        finally:
            release.set()
        start.join(3)
        stop.join(3)
        self.assertEqual(len(self.errors), 1)
        self.assertIn("not proven", str(self.errors.pop()))
        self.assertEqual(slot.state, "blocked")
        self.assertTrue(slot.runtime_started)
        self.assertTrue(slot.actor.alive)
        self.assertFalse(self.plugin._astrbotex_ros.closed)
        self.assertNotIn("runtime_stop", self.plugin.events)
        self.registry.set_action_lifecycle_guard(lambda item, reason: True)
        self.registry.stop_runtime("retry")
        self.assertEqual(self.plugin.events.count("runtime_stop"), 1)

    def test_stop_revokes_next_owner_before_first_start_returns(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        second = ActionPlugin()
        second.id = "second"
        original_start = self.plugin.on_runtime_start

        def slow_start() -> None:
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test multi-owner gate")
            original_start()

        self.plugin.on_runtime_start = slow_start
        first_slot = self.registry.register("action", self.plugin)
        second_slot = self.registry.register("action", second)
        try:
            start = self.spawn("start", self.registry.start_runtime)
            self.assertTrue(entered.wait(2))
            stop = self.spawn("stop", lambda: self.registry.stop_runtime("multi-owner stop"))
            self.assertTrue(self._wait_for(lambda: not self.registry._runtime_active))
            self.assertFalse(second_slot.actor._runtime_active)
        finally:
            release.set()
        start.join(3)
        stop.join(3)
        self.assertNotIn("runtime_start", second.events)
        self.assertFalse(first_slot.actor._runtime_active)
        self.registry.unregister(second.id)

    def test_timed_out_start_keeps_instance_blocked_until_real_stop(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        original_start = self.plugin.on_runtime_start

        def slow_start() -> None:
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test timeout gate")
            original_start()

        self.plugin.on_runtime_start = slow_start
        slot = self.registry.register("action", self.plugin)
        slot.actor.call_timeout = 0.05
        try:
            self.registry.start_runtime()
            self.assertTrue(entered.is_set())
            self.assertEqual(slot.state, "blocked")
            self.assertTrue(slot.runtime_started)
            self.assertIsNone(self.registry.get_slot("action"))
            self.assertNotIn("runtime_stop", self.plugin.events)
        finally:
            release.set()
        self.assertTrue(self._wait_for(lambda: "runtime_start" in self.plugin.events))
        self.registry.stop_runtime("late start reconciliation")
        self.assertEqual(self.plugin.events.count("runtime_stop"), 1)
        self.assertFalse(slot.actor._runtime_active)
        self.assertEqual(slot.state, "ready")

    def test_old_stop_waiter_cannot_claim_replacement(self) -> None:
        from threading import Condition

        class PausedCondition(Condition):
            def __init__(self, lock) -> None:
                super().__init__(lock)
                self.waiting = threading.Event()
                self.resume = threading.Event()
                self.pause_thread: threading.Thread | None = None

            def wait(self, timeout=None):
                if threading.current_thread() is not self.pause_thread:
                    return super().wait(timeout=timeout)
                self.waiting.set()
                self.release()
                try:
                    if not self.resume.wait(3):
                        raise TimeoutError("test condition gate")
                finally:
                    self.acquire()
                return True

        for operation in ("disable", "unregister"):
            with self.subTest(operation=operation):
                registry = PluginRegistry()
                registry.set_action_lifecycle_guard(lambda slot, reason: True)
                original = ActionPlugin()
                old = registry.register("action", original)
                replacement = ActionPlugin()
                unload_entered = threading.Event()
                release_unload = threading.Event()
                original_unload = original.on_unload

                def slow_unload() -> None:
                    unload_entered.set()
                    if not release_unload.wait(3):
                        raise TimeoutError("test unload gate")
                    original_unload()

                original.on_unload = slow_unload
                condition = PausedCondition(registry._lock)
                registry._condition = condition
                stop_errors: list[Exception] = []

                def late_stop() -> None:
                    try:
                        getattr(registry, operation)(original.id)
                    except Exception as exc:
                        stop_errors.append(exc)

                removal = threading.Thread(target=lambda: registry.unregister(original.id), name="old-unregister")
                waiter = threading.Thread(target=late_stop, name="old-stop-waiter")
                condition.pause_thread = waiter
                removal.start()
                try:
                    self.assertTrue(unload_entered.wait(2))
                    waiter.start()
                    self.assertTrue(condition.waiting.wait(2))
                    self.assertIs(registry.get(original.id), old)
                finally:
                    release_unload.set()
                removal.join(3)
                self.assertFalse(removal.is_alive())
                try:
                    new = registry.register("action", replacement)
                    self.assertEqual(new.generation, old.generation + 1)
                finally:
                    condition.resume.set()
                    waiter.join(3)
                self.assertFalse(waiter.is_alive())
                self.assertEqual(len(stop_errors), 1)
                self.assertIsInstance(stop_errors[0], KeyError)
                self.assertIs(registry.get(original.id), new)
                self.assertEqual(new.state, "ready")
                self.assertTrue(new.enabled)
                self.assertNotIn("disable", replacement.events)
                self.assertNotIn("unload", replacement.events)
                self.assertFalse(old.actor.alive)
                registry.unregister(new.id)

    def test_partial_action_start_failure_blocks_until_proven_stop(self) -> None:
        effects: list[str] = []

        def partial_start() -> None:
            effects.append("started device")
            raise ValueError("start failed after side effect")

        self.plugin.on_runtime_start = partial_start
        slot = self.registry.register("action", self.plugin)
        slot.actor.set_action_guard(lambda command: True)
        self.registry.start_runtime()
        self.assertEqual(effects, ["started device"])
        self.assertEqual(slot.state, "blocked")
        self.assertTrue(slot.runtime_started)
        self.assertIn("start failed after side effect", slot.stop_error)
        self.assertIsNone(self.registry.get_slot("action"))
        self.assertFalse(self.plugin._astrbotex_ros.closed)
        self.assertTrue(slot.actor.alive)
        self.assertNotIn("runtime_stop", self.plugin.events)
        with self.assertRaises(ActionGuardRejected):
            slot.actor.submit_action({"id": "forbidden"}).result(1)
        self.registry.set_action_lifecycle_guard(lambda item, reason: False)
        with self.assertRaisesRegex(RuntimeError, "not proven"):
            self.registry.stop_runtime("first proof fails")
        self.assertEqual(slot.state, "blocked")
        self.assertTrue(slot.runtime_started)
        self.assertNotIn("runtime_stop", self.plugin.events)
        self.registry.set_action_lifecycle_guard(lambda item, reason: True)
        self.registry.stop_runtime("proven stop")
        self.assertEqual(self.plugin.events.count("runtime_stop"), 1)
        self.assertFalse(slot.runtime_started)
        self.assertEqual(slot.state, "ready")

    @staticmethod
    def _wait_for(predicate) -> bool:
        for _ in range(200):
            if predicate():
                return True
            threading.Event().wait(0.01)
        return False


if __name__ == "__main__":
    unittest.main()
