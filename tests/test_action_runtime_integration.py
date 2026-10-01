from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astrbot_ex.core.actions.ledger import ActionLedger, OwnerBinding, StopEvidence
from astrbot_ex.core.actions.models import ActionCommand, ActionStatus
from astrbot_ex.core.actions.service import ActionService
from astrbot_ex.core.api_server import RuntimeController, build_server
from astrbot_ex.core.astrbot_bridge import AstrBotBridge
from astrbot_ex.core.backup import SNAPSHOT_ROOTS
from astrbot_ex.core.decision.catalog import CapabilityCatalog
from astrbot_ex.core.environments.manager import EnvironmentManager
from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.models import RobotState, RuntimeState
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.runtime import AstrBotEXRuntime
from astrbot_ex.core.topic_bus import TopicBus


class FakeDispatcher:
    def __init__(self, ledger):
        self.ledger = ledger
        self._lock = threading.RLock()
        self._gate = False
        self.blocked = False
        self.faults = ()
        self.canceled = []
        self.versions = None

    def set_gate(self, enabled):
        with self._lock:
            self._gate = enabled

    def update_versions(self, **versions):
        self.versions = versions
        self.set_gate(False)

    def cancel(self, command_id, binding, reason):
        self.canceled.append((command_id, binding, reason))
        result = Future()
        result.set_result(self.ledger.get(command_id).result(timeout=1))
        return result

    def close(self):
        pass


class LifecycleProbePlugin:
    def __init__(self, plugin_id, counters):
        self.id = plugin_id
        self.name = plugin_id
        self.counters = counters

    def on_runtime_start(self):
        self.counters["start"] += 1

    def read_state(self):
        return RobotState(link_ok=True)

    def on_worker_step(self):
        self.counters["worker"] += 1

    def on_tick(self, world):
        self.counters["tick"] += 1

    def on_runtime_stop(self, reason):
        self.counters["stop"] += 1


class ActionRuntimeIntegrationTest(unittest.TestCase):
    def test_build_server_wires_persistent_components_and_closes(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"ASTRBOTEX_DATA_DIR": root,
                                      "ASTRBOTEX_STT_ENABLED": "", "ASTRBOTEX_TTS_ENABLED": ""}):
                with patch("astrbot_ex.core.api_server.ActionDispatcher", FakeDispatcher):
                    server = build_server("127.0.0.1", 0, 20)
                try:
                    self.assertIs(server.local_plugins.action_dispatcher, server.action_dispatcher)
                    self.assertIs(server.local_plugins.capability_catalog, server.capability_catalog)
                    self.assertIs(server.environment_manager.action_service, server.action_service)
                    self.assertIs(server.controller.runtime.action_service, server.action_service)
                    ledger_path = Path(server.action_ledger._path).resolve()
                    self.assertEqual(ledger_path, (Path(root) / "execution/actions.sqlite3").resolve())
                    self.assertTrue(ledger_path.exists())
                    self.assertNotIn(ledger_path.relative_to(Path(root).resolve()).parts[0], SNAPSHOT_ROOTS)
                    self.assertEqual(server.controller.status()["control_mode"], "legacy")
                finally:
                    server.server_close()
                self.assertTrue(server.action_ledger.health.closed)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = ActionLedger(Path(self.temp.name) / "actions.sqlite3")
        self.dispatcher = FakeDispatcher(self.ledger)
        self.service = ActionService(self.ledger, self.dispatcher, CapabilityCatalog(), stop_timeout=0.05)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def command(self, command_id="test-command"):
        return ActionCommand.parse({
            "schema_version": 1, "command_id": command_id,
            "ex_session": "test-session", "goal_id": "test-goal", "goal_revision": 1,
            "decision_id": "test-decision", "owner": "test-owner", "plugin_generation": 1,
            "action_id": "test-owner.move.v1", "operation": "start", "params": {},
            "lease_ms": 1000,
        })

    def test_unknown_with_no_resources_remains_blocked_until_committed_proof(self):
        binding = OwnerBinding("test-owner", 1)
        self.ledger.admit(self.command(), (), binding, task_id="test-task").result(timeout=1)
        self.ledger.report("test-command", binding, ActionStatus.UNKNOWN).result(timeout=1)
        self.assertFalse(self.service.stop_actions("stop"))
        self.assertTrue(self.service.status()["blocked"])
        self.assertEqual(self.service.status()["unresolved"][0]["status"], "unknown")
        self.ledger.reconcile_stop("test-command", binding,
                                   StopEvidence("test-command", True, "mock-controller", "stopped-1")).result(timeout=1)
        self.assertTrue(self.service.stop_actions("review"))
        self.assertEqual(self.service.status()["unresolved"][0]["status"], "unknown")
        self.assertFalse(self.service.status()["blocked"])

    def test_accepted_cancel_request_is_not_stop_proof(self):
        binding = OwnerBinding("test-owner", 1)
        self.ledger.admit(self.command(), ("test-resource",), binding, task_id="test-task").result(timeout=1)
        self.ledger.report("test-command", binding, ActionStatus.ACCEPTED).result(timeout=1)
        self.assertFalse(self.service.stop_actions("stop"))
        self.assertEqual(len(self.dispatcher.canceled), 1)
        self.assertEqual(self.service.status()["unresolved"][0]["held_resources"], ["test-resource"])

    def test_environment_switch_failure_retains_old_adapter(self):
        manager = EnvironmentManager(data_root=Path(self.temp.name),
                                     event_bus=EventBus(), topic_bus=TopicBus())
        manager.action_service = self.service
        binding = OwnerBinding("test-owner", 1)
        self.ledger.admit(self.command(), (), binding, task_id="test-task").result(timeout=1)
        self.ledger.report("test-command", binding, ActionStatus.UNKNOWN).result(timeout=1)
        old_adapter = manager._adapter
        try:
            manager.select("ros2")
            manager._worker.join(2)
            self.assertEqual(manager.snapshot()["phase"], "failed")
            self.assertIs(manager._adapter, old_adapter)
            self.assertTrue(self.service.status()["blocked"])
        finally:
            self.ledger.reconcile_stop("test-command", binding,
                                       StopEvidence("test-command", True, "mock", "stopped")).result(timeout=1)
            manager.close()

    def test_controller_revokes_gate_before_waiting_for_tick_lock(self):
        runtime = AstrBotEXRuntime(PluginRegistry(), action_service=self.service)
        controller = RuntimeController(runtime)
        controller._lock.acquire()
        try:
            self.dispatcher.set_gate(True)
            done = threading.Event()
            worker = threading.Thread(target=lambda: (controller.stop("test"), done.set()), daemon=True)
            worker.start()
            deadline = time.monotonic() + 0.5
            while self.dispatcher._gate and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertFalse(self.dispatcher._gate)
        finally:
            controller._lock.release()
        worker.join(1)
        self.assertTrue(done.is_set())
        self.assertEqual(controller.status()["control_mode"], "legacy")

    def test_decision_tick_never_selects_legacy_goal_or_sends_motion(self):
        class Policy:
            id = "policy"
            name = "policy"
            def select_goal(self, world):
                raise AssertionError("legacy policy ran")
        class Motion:
            id = "motion"
            name = "motion"
            def read_state(self):
                raise AssertionError("legacy motion read")
            def send(self, intent):
                raise AssertionError("legacy motion sent")
            def stop(self, reason):
                pass
        registry = PluginRegistry()
        registry.register("policy", Policy())
        registry.register("motion", Motion())
        runtime = AstrBotEXRuntime(registry, action_service=self.service)
        controller = RuntimeController(runtime)
        try:
            runtime.start()
            controller.change_mode("decision")
            self.assertEqual(runtime.state, RuntimeState.IDLE)
            runtime.start()
            runtime.tick()
            self.assertEqual(self.service.control_mode, "decision")
            controller.change_mode("legacy")
            self.assertEqual(runtime.state, RuntimeState.IDLE)
            self.assertFalse(self.dispatcher._gate)
        finally:
            runtime.stop("test")
            registry.unregister("motion")
            registry.unregister("policy")

    def test_decision_mode_suppresses_legacy_start_worker_and_tick(self):
        registry = PluginRegistry()
        counters = {kind: {name: 0 for name in ("start", "worker", "tick", "stop")}
                    for kind in ("policy", "skill", "motion")}
        for kind in counters:
            registry.register(kind, LifecycleProbePlugin(kind, counters[kind]))
        runtime = AstrBotEXRuntime(registry, action_service=self.service)
        self.service.change_mode("decision")
        try:
            runtime.start()
            for _ in range(5):
                runtime.tick()
            time.sleep(0.05)
            for counter in counters.values():
                self.assertEqual(counter["start"], 0)
                self.assertEqual(counter["worker"], 0)
                self.assertEqual(counter["tick"], 0)
            runtime.stop("decision test")
            self.service.change_mode("legacy")
            runtime.start()
            for _ in range(10):
                runtime.tick()
            time.sleep(0.2)
            self.assertTrue(any(counter["start"] for counter in counters.values()))
            self.assertTrue(any(counter["worker"] for counter in counters.values()))
            self.assertTrue(any(counter["tick"] for counter in counters.values()))
        finally:
            runtime.stop("test")
            for kind in counters:
                registry.unregister(kind)

    def test_controller_start_stop_race_does_not_commit_running(self):
        registry = PluginRegistry()
        entered = threading.Event()
        release = threading.Event()
        counters = {"start": 0, "worker": 0, "tick": 0, "stop": 0}
        plugin = LifecycleProbePlugin("motion", counters)
        original = plugin.on_runtime_start
        def delayed_start():
            entered.set()
            release.wait(2)
            original()
        plugin.on_runtime_start = delayed_start
        registry.register("motion", plugin)
        runtime = AstrBotEXRuntime(registry, action_service=self.service)
        controller = RuntimeController(runtime)
        start = threading.Thread(target=controller.start)
        start.start()
        try:
            self.assertTrue(entered.wait(1))
            stop = threading.Thread(target=lambda: controller.stop("race"))
            stop.start()
            self.assertTrue(self._wait_for(lambda: runtime._stopping.is_set()))
            release.set()
            start.join(3)
            stop.join(3)
            self.assertEqual(runtime.state, RuntimeState.IDLE)
            self.assertGreaterEqual(counters["stop"], 1)
            before = counters["worker"] + counters["tick"]
            time.sleep(0.05)
            self.assertEqual(before, counters["worker"] + counters["tick"])
        finally:
            release.set()
            start.join(3)
            registry.unregister("motion")

    def test_registry_preserves_dispatcher_guard_across_revoke_and_reenable(self):
        registry = PluginRegistry()
        plugin = LifecycleProbePlugin("action", {"start": 0, "worker": 0, "tick": 0, "stop": 0})
        slot = registry.register("action", plugin)
        calls = []
        guard = lambda command: calls.append(command) is not None
        slot.actor.set_action_guard(guard)
        registry.set_action_lifecycle_guard(lambda slot, reason: True)
        registry.start_runtime()
        registry.disable("action")
        self.assertIsNotNone(slot.actor._action_guard)
        registry.enable("action")
        self.assertIsNotNone(slot.actor._action_guard)
        self.assertEqual(slot.actor.lifecycle_ready(), True)
        registry.unregister("action")

    @staticmethod
    def _wait_for(predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return False
    def test_bridge_rejects_existing_context_after_mode_change(self):
        runtime = AstrBotEXRuntime(PluginRegistry(), action_service=self.service)
        controller = RuntimeController(runtime)
        manager = SimpleNamespace(records={}, list_publishers=lambda: [])
        bridge = AstrBotBridge(controller=controller, local_plugins=manager,
                              event_bus=EventBus(), topic_bus=TopicBus())
        context = bridge.build_context()
        self.service.change_mode("decision")
        self.assertEqual(bridge.list_actions(), [])
        self.assertFalse(bridge.handle_proposal({"context_id": context["context_id"], "commands": [
            {"action_id": "runtime.start.v1", "params": {}, "reason": "old context"}]} )["ok"])
        self.assertEqual(runtime.state, RuntimeState.IDLE)



if __name__ == "__main__":
    unittest.main()
