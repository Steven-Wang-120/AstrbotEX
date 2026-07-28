from __future__ import annotations

import threading
import time
import unittest

from astrbot_ex.core.plugin_registry import PluginRegistry


class RecordingPlugin:
    id = "recording_plugin"
    name = "Recording Plugin"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.worker_steps = 0

    def _record(self, method: str) -> None:
        self.calls.append((method, threading.current_thread().name))

    def on_load(self) -> None:
        self._record("on_load")

    def on_enable(self) -> None:
        self._record("on_enable")

    def on_runtime_start(self) -> None:
        self._record("on_runtime_start")

    def on_worker_step(self) -> None:
        self._record("on_worker_step")
        self.worker_steps += 1
        time.sleep(0.005)

    def echo(self, value: str) -> str:
        self._record("echo")
        return value

    def on_tick(self, world: object) -> None:
        self._record("on_tick")

    def on_runtime_stop(self, reason: str) -> None:
        self._record("on_runtime_stop")

    def on_disable(self) -> None:
        self._record("on_disable")

    def on_unload(self) -> None:
        self._record("on_unload")


class PluginActorTest(unittest.TestCase):
    def test_all_plugin_callbacks_run_on_one_managed_thread(self) -> None:
        registry = PluginRegistry()
        plugin = RecordingPlugin()
        registry.register("device", plugin)
        slot = registry.get_slot("device")
        self.assertIsNotNone(slot)

        try:
            registry.start_runtime()
            deadline = time.monotonic() + 1.0
            while plugin.worker_steps == 0 and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertEqual(slot.call("echo", "ok"), "ok")
            slot.cast("on_tick", object(), coalesce_key="runtime_tick")
            deadline = time.monotonic() + 1.0
            while not any(method == "on_tick" for method, _ in plugin.calls) and time.monotonic() < deadline:
                time.sleep(0.01)
            registry.stop_runtime("test complete")
        finally:
            registry.unregister(plugin.id)

        callback_threads = {thread_name for _, thread_name in plugin.calls}
        self.assertEqual(callback_threads, {"astrbotex-plugin-recording_plugin"})
        self.assertGreater(plugin.worker_steps, 0)
        self.assertFalse(any(thread.name == "astrbotex-plugin-recording_plugin" for thread in threading.enumerate()))

    def test_cast_coalesces_pending_ticks(self) -> None:
        registry = PluginRegistry()
        plugin = RecordingPlugin()
        registry.register("device", plugin)
        slot = registry.get_slot("device")
        self.assertIsNotNone(slot)

        gate = threading.Event()

        def blocking_call() -> None:
            gate.wait(timeout=0.2)

        plugin.blocking_call = blocking_call
        try:
            slot.cast("blocking_call")
            self.assertTrue(slot.cast("on_tick", object(), coalesce_key="runtime_tick"))
            self.assertFalse(slot.cast("on_tick", object(), coalesce_key="runtime_tick"))
            gate.set()
            slot.call("echo", "drained")
        finally:
            registry.unregister(plugin.id)

        self.assertEqual(sum(method == "on_tick" for method, _ in plugin.calls), 1)

    def test_runtime_start_failure_is_isolated_to_one_plugin(self) -> None:
        registry = PluginRegistry()
        first = RecordingPlugin()
        first.id = "first_plugin"
        second = RecordingPlugin()
        second.id = "second_plugin"

        def fail_start() -> None:
            second._record("on_runtime_start")
            raise RuntimeError("start failed")

        second.on_runtime_start = fail_start
        registry.register("device", first)
        registry.register("device", second)
        try:
            registry.start_runtime()
            first_slot = registry.get(first.id)
            second_slot = registry.get(second.id)
            self.assertIsNone(first_slot.actor.last_error)
            self.assertEqual(second_slot.actor.last_error, "start failed")
            registry.stop_runtime("test complete")
        finally:
            registry.unregister(first.id)
            registry.unregister(second.id)


if __name__ == "__main__":
    unittest.main()
