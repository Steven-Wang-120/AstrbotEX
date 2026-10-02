from __future__ import annotations

import copy
import json
import random
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from astrbot_ex.core.actions.dispatcher import ActionDispatcher, DispatcherBusy
from astrbot_ex.core.actions.ledger import ActionLedger, OwnerBinding, StopEvidence
from astrbot_ex.core.actions.models import ActionStatus, ContractError, ErrorCode
from astrbot_ex.core.plugin_actor import PluginActor


class MockController:
    def __init__(self, owner: str) -> None:
        self.id = owner
        self.commands = []
        self.stops = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.callback = None
        self.sleep_seconds = 0.0
        self.watchdog_parked = threading.Event()
        self._physical_lock = threading.Lock()
        self._physical_deadline = 0.0
        self._physical_running = False
        self._watchdog_closed = threading.Event()
        self._watchdog_thread = threading.Thread(target=self._physical_watch, daemon=True)
        self._watchdog_thread.start()

    def block_legacy(self):
        self.entered.set()
        self.release.wait(5)

    def _physical_watch(self):
        while not self._watchdog_closed.wait(0.005):
            with self._physical_lock:
                if self._physical_running and time.monotonic() >= self._physical_deadline:
                    self._physical_running = False
                    self.watchdog_parked.set()

    def close(self):
        self.release.set()
        self._watchdog_closed.set()
        self._watchdog_thread.join(2)
        if self._watchdog_thread.is_alive():
            raise RuntimeError("mock physical watchdog did not stop")

    def on_action_command(self, command):
        self.commands.append(command)
        with self._physical_lock:
            self._physical_running = True
            self._physical_deadline = time.monotonic() + command.lease_ms / 1000
        self.entered.set()
        if self.block:
            self.release.wait(5)
        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        if self.callback:
            self.callback(command)
        return "accepted"

    def on_action_cancel(self, command_id, reason):
        self.stops.append(command_id)
        return "requested"


def manifest(owner, *, resource=None, observe=False, max_duration=1000):
    return {
        "id": owner, "action_api_version": 2,
        "actions": [{
            "action_id": f"{owner}.move.v2", "description": "Move",
            "schema": {"type": "object", "properties": {"meters": {"type": "integer", "minimum": 0, "maximum": 100}},
                       "required": ["meters"], "additionalProperties": False},
            "resources": [resource] if resource else [], "operations": ["start", "cancel"],
            "cancel_timeout_ms": 60, "max_duration_ms": max_duration,
            "requires_observations": ["pose"] if observe else [],
        }],
        "observation_sources": {"pose": {"topic": "sensor.pose", "max_age_ms": 80,
                                          "required_fields": ["position"]}} if observe else {},
    }


def command(owner, cid, *, meters=1, lease=900):
    return {"schema_version": 1, "command_id": cid, "ex_session": "session", "goal_id": "goal",
            "goal_revision": 1, "decision_id": "decision", "owner": owner,
            "plugin_generation": 3, "action_id": f"{owner}.move.v2", "operation": "start",
            "params": {"meters": meters}, "lease_ms": lease}


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.sqlite"
        self.ledger = ActionLedger(self.path, queue_size=2048)
        self.plugins = {}
        self.actors = {}
        self.dispatcher = ActionDispatcher(self.ledger, queue_size=512)
        self.addCleanup(self.close)

    def close(self):
        for plugin in self.plugins.values():
            plugin.release.set()
        self.dispatcher.close()
        for actor in self.actors.values():
            actor.stop(timeout=3)
        for plugin in self.plugins.values():
            plugin.close()
        self.ledger.close()

    def owner(self, name="arm", *, resource=None, observe=False, queue_count=64,
              max_duration=1000):
        plugin = MockController(name)
        actor = PluginActor(plugin, action_max_count=queue_count, action_max_bytes=2_000_000)
        actor.start()
        self.plugins[name] = plugin
        self.actors[name] = actor
        self.dispatcher.register_owner(OwnerBinding(name, 3), actor,
                                       manifest(name, resource=resource, observe=observe,
                                                max_duration=max_duration))
        return plugin

    def context(self, *owners, ttl=5000, observe=False, meters=1):
        actions = [f"{name}.move.v2" for name in owners]
        self.dispatcher.update_context(
            ex_session="session", goal_id="goal", goal_revision=1, task_id="trusted-task",
            allowed_actions=actions, bound_params={name: {"meters": meters} for name in actions},
            runtime_state="ready", catalog_revision=1, config_revision=1,
            environment_revision=1, ttl_ms=ttl,
            observations={"pose": {"topic": "sensor.pose", "fields": {"position": 1},
                                   "age_ms": 0}} if observe else {})

    def enabled(self, *owners, **kwargs):
        self.dispatcher.set_gate(True)
        self.context(*owners, **kwargs)

    def wait_status(self, cid, status, timeout=3):
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            snapshot = self.dispatcher.query(cid).result(2)
            if snapshot and snapshot.status == status:
                return snapshot
            time.sleep(0.005)
        self.fail(f"{cid} never reached {status}: {snapshot}")

    def test_priority_saturation_retries_timeout_until_durable(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        plugin.block = True
        self.dispatcher.start(command("arm", "priority-expiry", lease=180)).result(3)
        self.assertTrue(plugin.entered.wait(2))
        entered, release = threading.Event(), threading.Event()
        def hold_control():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("control test latch")
        try:
            running = self.dispatcher._enqueue(hold_control, priority=True)
            self.assertTrue(entered.wait(2))
            self.dispatcher._priority_size = 1
            queued = self.dispatcher._enqueue(lambda: "priority-slot", priority=True)
            time.sleep(0.24)
            self.assertTrue(self.dispatcher.blocked)
            self.assertEqual(self.dispatcher.query("priority-expiry").result(3).status, ActionStatus.ADMITTED)
            self.assertIn("priority-expiry", self.dispatcher._timeout_pending)
        finally:
            release.set()
            plugin.release.set()
        running.result(3)
        self.assertEqual(queued.result(3), "priority-slot")
        snap = self.wait_status("priority-expiry", ActionStatus.TIMED_OUT)
        self.assertEqual(snap.held_resources, ("joint",))
        self.assertTrue(plugin.watchdog_parked.wait(2))
        self.assertEqual(self.ledger.get("priority-expiry").result(3).status, ActionStatus.TIMED_OUT)

    def _assert_durable_timeout(self, cid, plugin, reason):
        snapshot = self.dispatcher.query(cid).result(3)
        self.assertEqual((snapshot.status, snapshot.reason_code, snapshot.held_resources),
                         (ActionStatus.TIMED_OUT, reason, ("joint",)))
        self.assertTrue(self.dispatcher.blocked)
        self.assertFalse(self.dispatcher._gate)
        self.assertEqual([cmd.command_id for cmd in plugin.commands], [cid])
        events = [event for event in self.ledger.events().result(3) if event.command_id == cid]
        self.assertEqual(events[-1].status, ActionStatus.TIMED_OUT)
        self.assertEqual(events[-1].reason_code, reason)
        return snapshot

    def _budget_timeout_order(self, *, actor_first):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        entered, release = threading.Event(), threading.Event()
        slot_entered, slot_release = threading.Event(), threading.Event()
        retry_entered, retry_release = threading.Event(), threading.Event()
        reported, reports = threading.Event(), []
        original_enqueue = self.dispatcher._enqueue
        def enqueue_with_retry_latch(work, **kwargs):
            if threading.current_thread() is self.dispatcher._watchdog:
                try:
                    return original_enqueue(work, **kwargs)
                except DispatcherBusy:
                    retry_entered.set()
                    if not retry_release.wait(3):
                        raise TimeoutError("timeout retry latch")
                    raise
            return original_enqueue(work, **kwargs)
        def hold_control():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("initial control latch")
        def hold_slot():
            slot_entered.set()
            if not slot_release.wait(3):
                raise TimeoutError("priority slot latch")
        def callback(cmd):
            reports.append(self.dispatcher.report(cmd.command_id, OwnerBinding("arm", 3),
                                                  ActionStatus.SUCCEEDED))
            reported.set()
            if not plugin.release.wait(3):
                raise TimeoutError("ordered Actor latch")
        plugin.callback = callback
        plugin.after_action = lambda: None
        try:
            self.dispatcher.start(command("arm", "budget-order", lease=800)).result(3)
            self.assertTrue(reported.wait(2))
            busy = self.dispatcher._enqueue(hold_control, priority=True)
            self.assertTrue(entered.wait(2))
            with self.dispatcher._condition:
                self.dispatcher._priority_size = 1
            slot = self.dispatcher._enqueue(hold_slot, priority=True)
            self.dispatcher._enqueue = enqueue_with_retry_latch
            self.assertTrue(retry_entered.wait(2))
            self.assertEqual(self.dispatcher._timeout_pending["budget-order"],
                             "actor_callback_budget_exceeded")
            release.set()
            self.assertTrue(slot_entered.wait(2))
            busy.result(3)
            if actor_first:
                plugin.release.set()
                self.actors["arm"].call("after_action", timeout=3)
            else:
                retry_release.set()
                with self.dispatcher._condition:
                    self.assertTrue(self.dispatcher._condition.wait_for(
                        lambda: bool(self.dispatcher._priority), timeout=2))
            slot_release.set()
            slot.result(3)
            self.dispatcher._enqueue(lambda: None).result(3)
            self._assert_durable_timeout("budget-order", plugin, "actor_callback_budget_exceeded")
            with self.assertRaises(RuntimeError):
                reports[0].result(3)
            self.assertTrue(reports[0].done())
            retry_release.set()
            plugin.release.set()
            self.actors["arm"].call("after_action", timeout=3)
            self.dispatcher._enqueue(lambda: None).result(3)
            self._assert_durable_timeout("budget-order", plugin, "actor_callback_budget_exceeded")
        finally:
            self.dispatcher._enqueue = original_enqueue
            retry_release.set()
            release.set()
            slot_release.set()
            plugin.release.set()

    def test_expired_budget_actor_result_before_timeout_retry(self):
        self._budget_timeout_order(actor_first=True)

    def test_expired_budget_timeout_retry_before_actor_result(self):
        self._budget_timeout_order(actor_first=False)

    def _park_dispatch_watchdog(self):
        # Park the real watcher before its first observation, not the Actor or ledger.
        self.dispatcher.close()
        parked, release = threading.Event(), threading.Event()
        original_watch = ActionDispatcher._watch
        def watch(dispatcher, interval):
            parked.set()
            release.wait(5)
            original_watch(dispatcher, interval)
        from unittest.mock import patch
        with patch.object(ActionDispatcher, "_watch", watch):
            self.dispatcher = ActionDispatcher(self.ledger)
        self.addCleanup(release.set)
        self.assertTrue(parked.wait(2))
        return release

    def test_expired_budget_actor_result_before_watch_observation(self):
        watch_release = self._park_dispatch_watchdog()
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        reports = []
        def callback(cmd):
            reports.append(self.dispatcher.report(cmd.command_id, OwnerBinding("arm", 3),
                                                  ActionStatus.SUCCEEDED))
            plugin.release.wait(0.04)
        plugin.callback = callback
        plugin.after_action = lambda: None
        try:
            self.dispatcher.start(command("arm", "unobserved-budget", lease=800)).result(3)
            self.actors["arm"].call("after_action", timeout=3)
            self.assertGreater(self.actors["arm"].action_mailbox_stats()["last_action_duration_ms"], 20)
            self.dispatcher._enqueue(lambda: None).result(3)
            self._assert_durable_timeout("unobserved-budget", plugin, "actor_callback_budget_exceeded")
            with self.assertRaisesRegex(RuntimeError, "callback exceeded budget"):
                reports[0].result(3)
            self.assertTrue(reports[0].done())
        finally:
            watch_release.set()
            plugin.release.set()

    def _expired_queued_report(self, status, *, stop_proof=False):
        watch_release = self._park_dispatch_watchdog()
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        entered, release = threading.Event(), threading.Event()
        def hold_control():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("queued report latch")
        try:
            self.dispatcher.start(command("arm", "expired-report", lease=80)).result(3)
            self.wait_status("expired-report", ActionStatus.ACCEPTED)
            busy = self.dispatcher._enqueue(hold_control, priority=True)
            self.assertTrue(entered.wait(2))
            proof = StopEvidence("expired-report", True, "mock_controller", "after-expiry")
            if stop_proof:
                canceled = self.dispatcher.cancel("expired-report", OwnerBinding("arm", 3))
            report = self.dispatcher.report("expired-report", OwnerBinding("arm", 3), status,
                                            stop_evidence=proof if stop_proof else None)
            self.assertTrue(plugin.watchdog_parked.wait(2))
            self.assertFalse(report.done())
            release.set()
            busy.result(3)
            if stop_proof:
                canceled.result(3)
                result = report.result(3)
                self.assertEqual((result.status, result.reason_code, result.held_resources),
                                 (ActionStatus.TIMED_OUT, "action_deadline", ()))
                self.assertEqual(self.ledger.stop_proof("expired-report", OwnerBinding("arm", 3))
                                 .result(3), proof)
                self.assertFalse(self.dispatcher._gate)
                self.assertEqual(len(plugin.commands), 1)
                events = [event for event in self.ledger.events().result(3)
                          if event.command_id == "expired-report"]
                self.assertTrue(any(event.status == ActionStatus.TIMED_OUT for event in events))
                self.assertFalse(any(event.status == ActionStatus.CANCELED for event in events))
            else:
                with self.assertRaises(ContractError) as error:
                    report.result(3)
                self.assertEqual(error.exception.code, ErrorCode.ILLEGAL_TRANSITION)
                self._assert_durable_timeout("expired-report", plugin, "action_deadline")
                reconciled = self.dispatcher.reconcile_stop("expired-report", OwnerBinding("arm", 3),
                                                            proof).result(3)
                self.assertEqual((reconciled.status, reconciled.held_resources),
                                 (ActionStatus.TIMED_OUT, ()))
            self.assertTrue(report.done())
        finally:
            watch_release.set()
            release.set()
            plugin.release.set()

    def test_expired_lease_queued_success_cannot_beat_watchdog(self):
        self._expired_queued_report(ActionStatus.SUCCEEDED)

    def test_expired_lease_queued_failure_cannot_beat_watchdog(self):
        self._expired_queued_report(ActionStatus.FAILED)

    def test_expired_lease_queued_unknown_cannot_beat_watchdog(self):
        self._expired_queued_report(ActionStatus.UNKNOWN)

    def test_expired_cancel_proof_reconciles_without_replacing_timeout(self):
        self._expired_queued_report(ActionStatus.CANCELED, stop_proof=True)

    def test_timely_actor_failure_remains_unknown(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        def callback(cmd):
            raise RuntimeError("mock callback failure")
        plugin.callback = callback
        self.dispatcher.start(command("arm", "timely-failure")).result(3)
        snap = self.wait_status("timely-failure", ActionStatus.UNKNOWN)
        self.dispatcher._enqueue(lambda: None).result(1)
        self.assertEqual((snap.reason_code, snap.held_resources), ("actor_callback_failed", ("joint",)))
        self.assertEqual(len(plugin.commands), 1)
        self.assertTrue(self.dispatcher.blocked)
        self.assertFalse(self.dispatcher._gate)

    def test_running_report_then_callback_budget_is_durable_uncertainty(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        reported, release, report_future = threading.Event(), threading.Event(), []
        def callback(cmd):
            report_future.append(self.dispatcher.report(cmd.command_id, OwnerBinding("arm", 3),
                                                        ActionStatus.RUNNING))
            reported.set()
            release.wait(3)
        plugin.callback = callback
        try:
            self.dispatcher.start(command("arm", "running-slow", lease=800)).result(3)
            self.assertTrue(reported.wait(2))
            self.assertEqual(report_future[0].result(3).status, ActionStatus.RUNNING)
            self.assertEqual(self.dispatcher.query("running-slow").result(3).status, ActionStatus.RUNNING)
            time.sleep(0.05)
            release.set()
            snap = self.wait_status("running-slow", ActionStatus.TIMED_OUT)
            self.assertEqual(snap.reason_code, "actor_callback_budget_exceeded")
            self.assertEqual(snap.held_resources, ("joint",))
            self.assertTrue(self.dispatcher.blocked)
        finally:
            release.set()

    def test_a09_real_dispatcher_actor_crash_boundaries_no_replay(self):
        helper = Path(__file__).with_name("action_dispatcher_crash_helper.py")
        for boundary in ("before_admission", "inside_admission_transaction",
                         "after_commit_before_delivery", "after_business_start_before_terminal"):
            with self.subTest(boundary=boundary):
                db = Path(self.tmp.name) / f"{boundary}.sqlite"
                marker = Path(str(db) + ".starts")
                process = subprocess.run([sys.executable, "-m", "tests.action_dispatcher_crash_helper",
                                          str(db), boundary], capture_output=True, text=True, timeout=20)
                self.assertEqual(process.returncode, 0,
                                 msg=f"stdout={process.stdout!r} stderr={process.stderr!r}")
                starts_before = marker.read_bytes().count(b"start\n") if marker.exists() else 0
                ledger = ActionLedger(db)
                dispatcher = ActionDispatcher(ledger)
                fresh_plugin = MockController("arm")
                fresh_actor = PluginActor(fresh_plugin)
                fresh_actor.start()
                try:
                    rows = dispatcher.recover().result(10)
                    self.assertFalse(dispatcher._gate)
                    events = ledger.events().result(10)
                    if boundary in ("before_admission", "inside_admission_transaction"):
                        self.assertEqual(rows, ())
                        self.assertEqual(events, ())
                        self.assertEqual(starts_before, 0)
                        self.assertIsNone(ledger.get("crash-command").result(5))
                        conn = sqlite3.connect(db)
                        try:
                            self.assertEqual(conn.execute(
                                "SELECT COUNT(*) FROM resources WHERE command_id='crash-command'"
                            ).fetchone()[0], 0)
                            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
                        finally:
                            conn.close()
                        dispatcher.register_owner(OwnerBinding("arm", 3), fresh_actor,
                                                  manifest("arm", resource="joint", max_duration=30_000))
                        dispatcher.set_gate(True)
                        dispatcher.update_context(
                            ex_session="session", goal_id="goal", goal_revision=1,
                            task_id="trusted-task", allowed_actions=["arm.move.v2"],
                            bound_params={"arm.move.v2": {"meters": 1}}, runtime_state="ready",
                            catalog_revision=1, config_revision=1, environment_revision=1,
                            ttl_ms=30_000)
                        self.assertEqual(fresh_plugin.commands, [])
                        continue
                    self.assertEqual(len(rows), 1)
                    snapshot = rows[0]
                    self.assertEqual((snapshot.status, snapshot.reason_code,
                                      snapshot.held_resources),
                                     (ActionStatus.UNKNOWN, "resume_review", ("joint",)))
                    self.assertTrue(dispatcher.blocked)
                    self.assertFalse(dispatcher._gate)
                    self.assertEqual(starts_before, 0 if boundary == "after_commit_before_delivery" else 1)
                    self.assertEqual(events[-1].status, ActionStatus.UNKNOWN)
                    self.assertEqual(events[-1].reason_code, "resume_review")
                    dispatcher.register_owner(OwnerBinding("arm", 3), fresh_actor,
                                              manifest("arm", resource="joint", max_duration=30_000))
                    with self.assertRaises(RuntimeError):
                        dispatcher.set_gate(True)
                    proof = StopEvidence("crash-command", True, "restart-review", boundary)
                    dispatcher.reconcile_recovered_stop("crash-command", OwnerBinding("arm", 3), proof).result(10)
                    dispatcher.review_stops().result(10)
                    with self.assertRaises(RuntimeError):
                        dispatcher.start(command("arm", "crash-command", lease=30_000))
                    dispatcher.set_gate(True)
                    dispatcher.update_context(
                        ex_session="session", goal_id="goal", goal_revision=1,
                        task_id="trusted-task", allowed_actions=["arm.move.v2"],
                        bound_params={"arm.move.v2": {"meters": 1}}, runtime_state="ready",
                        catalog_revision=1, config_revision=1, environment_revision=1,
                        ttl_ms=30_000)
                    self.assertEqual(fresh_plugin.commands, [])
                    self.assertEqual(marker.read_bytes().count(b"start\n") if marker.exists() else 0,
                                     starts_before)
                finally:
                    fresh_actor.stop(timeout=3)
                    fresh_plugin.close()
                    dispatcher.close()
                    ledger.close()

    def test_1000_real_dispatcher_sequences_two_owners_no_duplicate_start(self):
        owners = {"arm": self.owner("arm", max_duration=30_000),
                  "wheel": self.owner("wheel", max_duration=30_000)}
        self.enabled("arm", "wheel", ttl=30_000)
        rng = random.Random(20260929)
        expected = set()
        for index in range(1000):
            owner = ("arm", "wheel")[rng.randrange(2)]
            cid = f"sequence-{index}"
            raw = command(owner, cid, lease=30_000)
            self.dispatcher.start(raw).result(15)
            expected.add((owner, cid))
            if rng.randrange(3) == 0:
                self.assertEqual(self.dispatcher.start(copy.deepcopy(raw)).result(15).command_id, cid)
        for owner, cid in expected:
            self.wait_status(cid, ActionStatus.ACCEPTED, 30)
        observed = [(owner, item.command_id) for owner, plugin in owners.items()
                    for item in plugin.commands]
        self.assertEqual(len(observed), 1000)
        self.assertEqual(len(set(observed)), 1000)
        self.assertEqual(set(observed), expected)
        self.assertTrue(all(item.owner == owner for owner, plugin in owners.items()
                            for item in plugin.commands))

    def test_1000_real_dispatcher_state_sequences_with_resources_and_rearm(self):
        owners = {"arm": self.owner("arm", resource="shared", max_duration=30_000),
                  "wheel": self.owner("wheel", resource="shared", max_duration=30_000)}
        self.enabled("arm", "wheel", ttl=30_000)
        rng = random.Random(20260929 ^ 0x5A5A)
        binding = {owner: OwnerBinding(owner, 3) for owner in owners}
        branch_counts = {"succeeded": 0, "failed": 0, "unknown": 0, "cancel": 0}
        expected_by_owner = {"arm": 0, "wheel": 0}
        completed = set()
        for index in range(1000):
            owner = ("arm", "wheel")[rng.randrange(2)]
            other = "wheel" if owner == "arm" else "arm"
            cid = f"state-sequence-{index}"
            raw = command(owner, cid, lease=30_000)
            expected_by_owner[owner] += 1
            self.dispatcher.start(raw).result(15)
            snapshot = self.wait_status(cid, ActionStatus.ACCEPTED, 15)
            self.assertEqual(snapshot.held_resources, ("shared",))
            admission_seq = snapshot.event_seq
            retry_snapshot = self.dispatcher.start(copy.deepcopy(raw)).result(10)
            self.assertEqual(retry_snapshot.command_id, cid)
            self.assertGreaterEqual(retry_snapshot.event_seq, admission_seq)
            self.assertEqual(len([item for item in owners[owner].commands if item.command_id == cid]), 1)
            self.assertTrue(all(item.owner == owner for item in owners[owner].commands
                                if item.command_id == cid))
            self.assertFalse(any(item.command_id == cid for item in owners[other].commands))

            conflict = command(other, f"conflict-{index}", lease=30_000)
            with self.assertRaises(ContractError):
                self.dispatcher.start(conflict).result(10)
            self.assertFalse(any(item.command_id == conflict["command_id"]
                                 for item in owners[other].commands))

            running = self.dispatcher.report(cid, binding[owner], ActionStatus.RUNNING).result(10)
            self.assertEqual(running.status, ActionStatus.RUNNING)
            self.assertEqual(self.dispatcher.report(cid, binding[owner], ActionStatus.RUNNING)
                             .result(10).event_seq, running.event_seq)
            with self.assertRaises(ContractError) as wrong_owner:
                self.dispatcher.report(cid, binding[other], ActionStatus.RUNNING).result(10)
            self.assertEqual(wrong_owner.exception.code, ErrorCode.OWNER_MISMATCH)
            branch = ("succeeded", "failed", "unknown", "cancel")[rng.randrange(4)]
            branch_counts[branch] += 1
            if branch == "succeeded":
                terminal = self.dispatcher.report(cid, binding[owner], ActionStatus.SUCCEEDED).result(10)
            elif branch == "failed":
                terminal = self.dispatcher.report(cid, binding[owner], ActionStatus.FAILED,
                                                   reason_code="seeded_failure").result(10)
            elif branch == "unknown":
                terminal = self.dispatcher.report(cid, binding[owner], ActionStatus.UNKNOWN,
                                                   reason_code="seeded_uncertain").result(10)
            else:
                self.dispatcher.cancel(cid, binding[owner], "seeded_cancel").result(10)
                terminal = self.dispatcher.report(
                    cid, binding[owner], ActionStatus.CANCELED,
                    stop_evidence=StopEvidence(cid, True, "seeded_controller", f"stop-{index}")
                ).result(10)
            self.assertIn(terminal.status, ActionStatus.__dict__.values())
            self.assertEqual(self.dispatcher.query(cid).result(10).status, terminal.status)
            terminal_events = self.ledger.events(after_seq=running.event_seq, limit=2).result(10)
            self.assertEqual(len(terminal_events), 1, msg=f"{cid}: missing or duplicate terminal event")
            terminal_event = terminal_events[0]
            self.assertEqual((terminal_event.command_id, terminal_event.event_seq, terminal_event.status),
                             (cid, terminal.event_seq, terminal.status))
            repeat = self.dispatcher.report(
                cid, binding[owner], terminal.status, reason_code=terminal.reason_code,
                stop_evidence=StopEvidence(cid, True, "seeded_controller", f"stop-{index}")
                if branch == "cancel" else None,
            ).result(10)
            self.assertEqual(repeat, terminal)
            self.assertEqual(self.ledger.events(after_seq=terminal_event.event_seq, limit=1).result(10), ())
            with self.assertRaises(ContractError) as illegal_state:
                self.dispatcher.report(cid, binding[owner], ActionStatus.RUNNING).result(10)
            self.assertEqual(illegal_state.exception.code, ErrorCode.ILLEGAL_TRANSITION)
            self.assertEqual(self.ledger.events(after_seq=terminal_event.event_seq, limit=1).result(10), ())
            if branch in ("failed", "unknown"):
                uncertain = self.dispatcher.query(cid).result(10)
                self.assertEqual(uncertain.held_resources, ("shared",))
                proof = StopEvidence(cid, True, "seeded_controller", f"reconcile-{index}")
                self.dispatcher.reconcile_stop(cid, binding[owner], proof).result(10)
                self.assertEqual(self.dispatcher.query(cid).result(10).held_resources, ())
                self.dispatcher.review_stops().result(10)
                self.assertFalse(self.dispatcher.blocked)
                self.assertFalse(self.dispatcher._gate)
                self.enabled("arm", "wheel", ttl=30_000)
            else:
                self.assertEqual(self.dispatcher.query(cid).result(10).held_resources, ())
            completed.add(cid)
            self.assertEqual(len(completed), index + 1)
        self.assertTrue(all(branch_counts[name] > 0 for name in branch_counts), branch_counts)
        for owner, plugin in owners.items():
            delivered = [item.command_id for item in plugin.commands]
            self.assertEqual(len(delivered), expected_by_owner[owner])
            self.assertEqual(len(delivered), len(set(delivered)))
            self.assertTrue(all(item.owner == owner for item in plugin.commands))
        self.assertEqual(sum(len(plugin.commands) for plugin in owners.values()), 1000)

    def test_canceled_deferred_report_does_not_strand_terminal_cleanup(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        reports, reported = [], threading.Event()
        def callback(cmd):
            reports.extend(self.dispatcher.report(cmd.command_id, OwnerBinding("arm", 3),
                                                   ActionStatus.SUCCEEDED) for _ in range(2))
            reports[0].cancel()
            reported.set()
            if not plugin.release.wait(3):
                raise TimeoutError("deferred report latch")
        plugin.callback = callback
        plugin.after_action = lambda: None
        try:
            self.dispatcher.start(command("arm", "canceled-terminal", lease=800)).result(3)
            self.assertTrue(reported.wait(2))
            snap = self.wait_status("canceled-terminal", ActionStatus.TIMED_OUT)
            self.assertEqual(snap.reason_code, "actor_callback_budget_exceeded")
            plugin.release.set()
            self.actors["arm"].call("after_action", timeout=3)
            self.assertTrue(reports[0].cancelled())
            with self.assertRaisesRegex(RuntimeError, "terminal before callback completed"):
                reports[1].result(3)
            self.assertTrue(reports[1].done())
            self.assertTrue(self.dispatcher.blocked)
            self.assertFalse(self.dispatcher._gate)
            with self.dispatcher._lock:
                self.assertEqual(self.dispatcher._live["canceled-terminal"].deferred_reports, [])
            self.assertEqual(self.dispatcher.query("canceled-terminal").result(3).held_resources,
                             ("joint",))
            self.assertEqual(len(plugin.commands), 1)
        finally:
            plugin.release.set()

    def _slow_deferred_cleanup(self, *, cancel_race=False):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        create, reported = threading.Event(), threading.Event()
        reports = []
        def callback(cmd):
            if not create.wait(3):
                raise TimeoutError("post-timeout report latch")
            reports.extend(self.dispatcher.report(cmd.command_id, OwnerBinding("arm", 3),
                                                   ActionStatus.SUCCEEDED) for _ in range(2))
            if not cancel_race:
                reports[0].cancel()
            reported.set()
            if not plugin.release.wait(3):
                raise TimeoutError("slow callback latch")
        plugin.callback = callback
        plugin.after_action = lambda: None
        try:
            self.dispatcher.start(command("arm", "canceled-slow", lease=800)).result(3)
            self.assertTrue(plugin.entered.wait(2))
            snap = self.wait_status("canceled-slow", ActionStatus.TIMED_OUT)
            self.assertEqual((snap.reason_code, snap.held_resources),
                             ("actor_callback_budget_exceeded", ("joint",)))
            # Reports created after the durable timeout exercise _actor_done, not _persist.
            create.set()
            self.assertTrue(reported.wait(2))
            raced = threading.Event()
            if cancel_race:
                original = reports[0].set_exception
                def cancel_before_completion(exc):
                    reports[0].cancel()
                    raced.set()
                    original(exc)
                reports[0].set_exception = cancel_before_completion
            plugin.release.set()
            self.actors["arm"].call("after_action", timeout=3)
            self.assertTrue(reports[0].cancelled())
            if cancel_race:
                self.assertTrue(raced.is_set())
            with self.assertRaisesRegex(RuntimeError, "callback exceeded budget"):
                reports[1].result(3)
            self.assertTrue(reports[1].done())
            self.assertTrue(self.dispatcher.blocked)
            self.assertFalse(self.dispatcher._gate)
            with self.dispatcher._lock:
                self.assertEqual(self.dispatcher._live["canceled-slow"].deferred_reports, [])
            final = self.dispatcher.query("canceled-slow").result(3)
            self.assertEqual((final.status, final.reason_code, final.held_resources),
                             (ActionStatus.TIMED_OUT, "actor_callback_budget_exceeded", ("joint",)))
            self.assertEqual(len(plugin.commands), 1)
        finally:
            create.set()
            plugin.release.set()

    def test_canceled_deferred_report_does_not_strand_slow_actor_cleanup(self):
        self._slow_deferred_cleanup()

    def test_deferred_report_cancel_race_does_not_strand_slow_actor_cleanup(self):
        self._slow_deferred_cleanup(cancel_race=True)

    def _deferred_queue_full_cleanup(self, *, result_full):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        control_entered, control_release = threading.Event(), threading.Event()
        callback_done = threading.Event()
        reports, control = [], []
        def hold_control():
            control_entered.set()
            if not control_release.wait(3):
                raise TimeoutError("queue saturation latch")
        def callback(cmd):
            # Partial saturation: actor result + one UNKNOWN report occupy both slots.
            reports.extend(self.dispatcher.report(cmd.command_id, OwnerBinding("arm", 3),
                                                   ActionStatus.UNKNOWN, reason_code="queue_test")
                           for _ in range(2 if result_full else 4))
            reports[0 if result_full else 1].cancel()
        plugin.callback = callback
        plugin.after_action = callback_done.set
        try:
            self.assertTrue(self.actors["arm"].cast("block_legacy"))
            self.assertTrue(plugin.entered.wait(2))
            self.dispatcher.start(command("arm", "canceled-full", lease=800)).result(3)
            control.append(self.dispatcher._enqueue(hold_control, priority=True))
            self.assertTrue(control_entered.wait(2))
            with self.dispatcher._condition:
                self.dispatcher._priority_size = 2
            if result_full:
                control.extend(self.dispatcher._enqueue(lambda: None, priority=True)
                               for _ in range(2))
            self.assertTrue(self.actors["arm"].cast("after_action"))
            plugin.release.set()
            self.assertTrue(callback_done.wait(2))
            if not result_full:
                self.assertLessEqual(self.actors["arm"].action_mailbox_stats()["last_action_duration_ms"], 20)
            canceled = 0 if result_full else 1
            self.assertTrue(reports[canceled].cancelled())
            failed = reports[1:] if result_full else reports[2:]
            for future in failed:
                with self.assertRaisesRegex(DispatcherBusy, "actor result queue full" if result_full
                                            else "priority report queue full"):
                    future.result(1)
                self.assertTrue(future.done())
            self.assertTrue(self.dispatcher.blocked)
            self.assertFalse(self.dispatcher._gate)
            with self.dispatcher._lock:
                self.assertLessEqual(len(self.dispatcher._priority), self.dispatcher._priority_size)
                self.assertEqual(self.dispatcher._live["canceled-full"].deferred_reports, [])
            expected_fault = "actor_result_not_queued" if result_full else "deferred_report_delivery_full"
            self.assertIn(expected_fault, self.dispatcher.faults)
            control_release.set()
            for future in control:
                future.result(3)
            if not result_full:
                self.assertEqual(reports[0].result(3).status, ActionStatus.UNKNOWN)
            snap = self.wait_status("canceled-full", ActionStatus.TIMED_OUT if result_full
                                    else ActionStatus.UNKNOWN)
            self.assertEqual(snap.held_resources, ("joint",))
            self.assertEqual(len(plugin.commands), 1)
        finally:
            control_release.set()
            plugin.release.set()

    def test_canceled_deferred_report_does_not_strand_actor_result_queue_full(self):
        self._deferred_queue_full_cleanup(result_full=True)

    def test_canceled_deferred_report_does_not_strand_priority_report_queue_full(self):
        self._deferred_queue_full_cleanup(result_full=False)

    def test_pending_report_fails_if_handler_never_returns_before_timeout(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        binding = OwnerBinding("arm", 3)
        reports = []
        def callback(cmd):
            reports.append(self.dispatcher.report(cmd.command_id, binding, ActionStatus.SUCCEEDED))
            plugin.release.wait(5)
        plugin.callback = callback
        try:
            self.dispatcher.start(command("arm", "pending-report", lease=160)).result(3)
            self.wait_status("pending-report", ActionStatus.TIMED_OUT)
            self.assertEqual(len(reports), 1)
            with self.assertRaises(RuntimeError):
                reports[0].result(3)
            self.assertEqual(self.dispatcher.query("pending-report").result(3).held_resources, ("joint",))
        finally:
            plugin.release.set()

    def test_rejected_callback_wins_over_sync_success_report(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        binding = OwnerBinding("arm", 3)
        reported = []
        def callback(cmd):
            reported.append(self.dispatcher.report(cmd.command_id, binding, ActionStatus.SUCCEEDED))
        plugin.callback = callback
        plugin.on_action_command = lambda cmd: (plugin.commands.append(cmd), callback(cmd), "rejected")[-1]
        self.dispatcher.start(command("arm", "reject-first")).result(3)
        self.wait_status("reject-first", ActionStatus.REJECTED)
        with self.assertRaises(ContractError):
            reported[0].result(3)
        self.assertEqual(self.dispatcher.query("reject-first").result(3).held_resources, ())
        self.assertEqual(len(plugin.commands), 1)

    def test_sync_success_report_cannot_release_resource_after_slow_callback(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        binding = OwnerBinding("arm", 3)
        report_future = []
        def callback(cmd):
            report_future.append(self.dispatcher.report(cmd.command_id, binding, ActionStatus.SUCCEEDED))
            time.sleep(0.08)
        plugin.callback = callback
        self.dispatcher.start(command("arm", "late-budget", lease=800)).result(3)
        self.wait_status("late-budget", ActionStatus.TIMED_OUT)
        self.assertTrue(self.dispatcher.blocked)
        self.assertEqual(self.dispatcher.query("late-budget").result(3).held_resources, ("joint",))
        self.assertEqual(len(report_future), 1)
        with self.assertRaises(RuntimeError):
            report_future[0].result(3)

    def test_report_priority_over_normal_admission_backlog(self):
        plugin = self.owner(max_duration=30_000)
        self.enabled("arm")
        self.dispatcher.start(command("arm", "live", lease=30_000)).result(3)
        self.wait_status("live", ActionStatus.ACCEPTED)
        entered = threading.Event()
        release = threading.Event()
        original_admit = self.ledger.admit
        def paused_admit(*args, **kwargs):
            if args[0].command_id == "first-backlog":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("admission latch")
            return original_admit(*args, **kwargs)
        self.ledger.admit = paused_admit
        try:
            first = self.dispatcher.start(command("arm", "first-backlog", lease=30_000))
            self.assertTrue(entered.wait(2))
            backlog = [self.dispatcher.start(command("arm", f"backlog-{i}", lease=30_000)) for i in range(30)]
            terminal = self.dispatcher.report("live", OwnerBinding("arm", 3), ActionStatus.SUCCEEDED)
        finally:
            release.set()
            self.ledger.admit = original_admit
        first.result(3)
        self.assertEqual(terminal.result(3).status, ActionStatus.SUCCEEDED)
        self.assertFalse(backlog[0].done())
        for future in backlog:
            future.result(10)
        self.wait_status("first-backlog", ActionStatus.ACCEPTED)
        for i in range(30):
            self.wait_status(f"backlog-{i}", ActionStatus.ACCEPTED)
        self.assertEqual(len(plugin.commands), 32)

    def test_emergency_callback_not_under_gate_lock_and_failure_visible(self):
        self.owner()
        self.enabled("arm")
        entered = threading.Event()
        release = threading.Event()
        def slow_failure(reason):
            entered.set()
            release.wait(2)
            raise RuntimeError("mock stop service unavailable")
        self.dispatcher._emergency_stop = slow_failure
        try:
            self.dispatcher._block("test_notification")
            self.assertTrue(entered.wait(2))
            before = time.monotonic()
            self.dispatcher.set_gate(False)
            self.assertLess(time.monotonic() - before, 0.1)
            self.assertTrue(self.dispatcher.blocked)
        finally:
            release.set()
        limit = time.monotonic() + 2
        while not any("emergency_stop_failed" in item for item in self.dispatcher.faults) and time.monotonic() < limit:
            time.sleep(0.005)
        self.assertTrue(any("emergency_stop_failed" in item for item in self.dispatcher.faults))

    def test_slow_callback_is_uncertain_not_accepted(self):
        plugin = self.owner(resource="joint")
        plugin.sleep_seconds = 0.09
        self.enabled("arm")
        self.dispatcher.start(command("arm", "slow", lease=800)).result(3)
        self.wait_status("slow", ActionStatus.TIMED_OUT)
        self.assertTrue(self.dispatcher.blocked)
        self.assertEqual(self.dispatcher.query("slow").result(3).held_resources, ("joint",))
        with self.assertRaises(RuntimeError):
            self.dispatcher.start(command("arm", "later"))

    def test_lease_measured_from_ingress_not_writer_commit(self):
        plugin = self.owner()
        self.enabled("arm")
        control_entered = threading.Event()
        control_release = threading.Event()
        def occupy():
            control_entered.set()
            control_release.wait(3)
        try:
            work = self.dispatcher._enqueue(occupy)
            self.assertTrue(control_entered.wait(2))
            pending = self.dispatcher.start(command("arm", "expired-ingress", lease=60))
            time.sleep(0.09)
        finally:
            control_release.set()
        work.result(3)
        with self.assertRaises(TimeoutError):
            pending.result(3)
        self.assertIsNone(self.dispatcher.query("expired-ingress").result(3))
        self.assertEqual(plugin.commands, [])

    def test_normal_control_queue_full_keeps_cancel_lane_available(self):
        plugin = self.owner(resource="joint", max_duration=30_000)
        self.enabled("arm")
        self.dispatcher.start(command("arm", "active", lease=30_000)).result(3)
        self.wait_status("active", ActionStatus.ACCEPTED)
        entered = threading.Event()
        release = threading.Event()
        def occupy():
            entered.set()
            release.wait(3)
        self.dispatcher._queue_size = 1
        try:
            worker = self.dispatcher._enqueue(occupy)
            self.assertTrue(entered.wait(2))
            queued = self.dispatcher.start(command("arm", "queued-normal", lease=30_000))
            with self.assertRaises(DispatcherBusy):
                self.dispatcher.start(command("arm", "queue-overflow", lease=30_000))
            self.assertTrue(self.dispatcher.blocked)
            stop = self.dispatcher.cancel("active", OwnerBinding("arm", 3))
        finally:
            release.set()
        worker.result(3)
        stop.result(3)
        with self.assertRaises(RuntimeError):
            queued.result(3)
        limit = time.monotonic() + 2
        while not plugin.stops and time.monotonic() < limit:
            time.sleep(0.005)
        self.assertEqual(plugin.stops, ["active"])
        self.assertEqual([item.command_id for item in plugin.commands], ["active"])
        self.assertIsNone(self.dispatcher.query("queue-overflow").result(3))

    def test_cancel_queued_future_does_not_kill_worker(self):
        plugin = self.owner()
        self.enabled("arm")
        control_entered = threading.Event()
        control_release = threading.Event()
        def occupy_control():
            control_entered.set()
            if not control_release.wait(3):
                raise TimeoutError("test control latch")
        try:
            busy = self.dispatcher._enqueue(occupy_control)
            self.assertTrue(control_entered.wait(2))
            canceled = self.dispatcher.start(command("arm", "cancel-future"))
            self.assertTrue(canceled.cancel())
            following = self.dispatcher.start(command("arm", "following"))
        finally:
            control_release.set()
        busy.result(3)
        following.result(3)
        self.wait_status("following", ActionStatus.ACCEPTED)
        self.assertIsNone(self.dispatcher.query("cancel-future").result(3))
        self.assertEqual([c.command_id for c in plugin.commands], ["following"])
        self.assertTrue(self.dispatcher._worker.is_alive())

    def test_cancel_prioritizes_stop_and_revokes_actor_queued_start(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        self.assertTrue(self.actors["arm"].cast("block_legacy"))
        self.assertTrue(plugin.entered.wait(2))
        self.dispatcher.start(command("arm", "queued")).result(3)
        try:
            self.dispatcher.cancel("queued", OwnerBinding("arm", 3)).result(3)
            self.assertEqual(self.actors["arm"].action_mailbox_stats()["start_count"], 0)
        finally:
            plugin.release.set()
        limit = time.monotonic() + 2
        while not plugin.stops and time.monotonic() < limit:
            time.sleep(0.005)
        self.assertEqual(plugin.stops, ["queued"])
        self.assertEqual(plugin.commands, [])
        self.wait_status("queued", ActionStatus.TIMED_OUT)
        self.assertEqual(self.dispatcher.query("queued").result(3).held_resources, ("joint",))

    def test_terminal_unknown_can_still_request_stop(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        self.dispatcher.start(command("arm", "unknown-stop")).result(3)
        self.wait_status("unknown-stop", ActionStatus.ACCEPTED)
        self.dispatcher.report("unknown-stop", OwnerBinding("arm", 3), ActionStatus.UNKNOWN).result(3)
        self.dispatcher.cancel("unknown-stop", OwnerBinding("arm", 3)).result(3)
        limit = time.monotonic() + 2
        while not plugin.stops and time.monotonic() < limit:
            time.sleep(0.005)
        self.assertEqual(plugin.stops, ["unknown-stop"])
        self.assertEqual(self.dispatcher.query("unknown-stop").result(3).status, ActionStatus.UNKNOWN)
        with self.assertRaises(RuntimeError):
            self.dispatcher.review_stops().result(3)
        evidence = StopEvidence("unknown-stop", True, "mock_controller", "stop-after-unknown")
        self.dispatcher.reconcile_stop("unknown-stop", OwnerBinding("arm", 3), evidence).result(3)
        self.assertEqual(self.dispatcher.query("unknown-stop").result(3).status, ActionStatus.UNKNOWN)
        self.dispatcher.review_stops().result(3)

    def test_independent_physical_lease_with_control_worker_blocked(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        plugin.block = True
        release_control = threading.Event()
        entered_control = threading.Event()
        try:
            self.dispatcher.start(command("arm", "physical", lease=160)).result(3)
            self.assertTrue(plugin.entered.wait(2))
            def occupy_control():
                entered_control.set()
                release_control.wait(3)
            blocked = self.dispatcher._enqueue(occupy_control)
            self.assertTrue(entered_control.wait(2))
            self.assertTrue(plugin.watchdog_parked.wait(2))
            self.assertTrue(plugin.block and not plugin.release.is_set())
            self.assertFalse(blocked.done())
            self.assertTrue(self.dispatcher.blocked)
        finally:
            release_control.set()
            plugin.release.set()
        blocked.result(3)
        self.wait_status("physical", ActionStatus.TIMED_OUT)

    def test_a01_two_owners_100_each_isolated(self):
        self.owner("arm", max_duration=30_000)
        self.owner("wheel", max_duration=30_000)
        self.enabled("arm", "wheel")
        futures = []
        for i in range(100):
            for owner in ("arm", "wheel"):
                futures.append(self.dispatcher.start(command(owner, f"{owner}-{i}", lease=30_000)))
        for future in futures:
            future.result(15)
        for owner in ("arm", "wheel"):
            for i in range(100):
                self.wait_status(f"{owner}-{i}", ActionStatus.ACCEPTED, 10)
            self.assertEqual(len(self.plugins[owner].commands), 100)
            self.assertTrue(all(c.owner == owner for c in self.plugins[owner].commands))

    def test_a02_retry_ten_times_and_conflict(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        self.dispatcher.start(command("arm", "one")).result(3)
        self.wait_status("one", ActionStatus.ACCEPTED)
        for _ in range(10):
            self.assertEqual(self.dispatcher.start(command("arm", "one")).result(3).command_id, "one")
        self.assertEqual(len(plugin.commands), 1)
        altered = command("arm", "one")
        altered["decision_id"] = "other"
        with self.assertRaises(ContractError) as caught:
            self.dispatcher.start(altered).result(3)
        self.assertEqual(caught.exception.code, ErrorCode.DUPLICATE_REQUEST_ID_CONFLICT)

    def test_a03_actor_queue_full_and_oversize(self):
        plugin = self.owner(queue_count=2)
        self.enabled("arm")
        self.assertTrue(self.actors["arm"].cast("block_legacy"))
        self.assertTrue(plugin.entered.wait(2))
        self.dispatcher.start(command("arm", "first")).result(3)
        self.dispatcher.start(command("arm", "second")).result(3)
        self.assertEqual(self.dispatcher.start(command("arm", "third")).result(3).status, ActionStatus.REJECTED)
        self.assertEqual(self.dispatcher.query("third").result(2).reason_code, "actor_busy")
        huge = command("arm", "huge")
        huge["params"]["payload"] = "x" * 1_100_000
        with self.assertRaises(ContractError):
            self.dispatcher.start(huge)
        plugin.release.set()
        self.wait_status("second", ActionStatus.ACCEPTED)
        self.assertEqual([c.command_id for c in plugin.commands], ["first", "second"])

    def test_a04_concurrent_resource_exclusion(self):
        self.owner("arm", resource="shared")
        self.owner("wheel", resource="shared")
        self.enabled("arm", "wheel")
        barrier = threading.Barrier(2)
        results = []
        def run(owner):
            barrier.wait()
            try:
                results.append(self.dispatcher.start(command(owner, owner)).result(3))
            except ContractError as exc:
                results.append(exc)
        threads = [threading.Thread(target=run, args=(owner,)) for owner in ("arm", "wheel")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(not isinstance(result, Exception) for result in results), 1)
        self.assertEqual(sum(len(plugin.commands) for plugin in self.plugins.values()), 1)

    def test_a05_blocked_handler_cancel_timeout_and_independent_watchdog(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        plugin.block = True
        try:
            self.dispatcher.start(command("arm", "stuck", lease=500)).result(3)
            self.assertTrue(plugin.entered.wait(2))
            self.dispatcher.cancel("stuck", OwnerBinding("arm", 3)).result(3)
            stopped = self.wait_status("stuck", ActionStatus.TIMED_OUT)
            self.assertEqual(stopped.held_resources, ("joint",))
            self.assertTrue(plugin.watchdog_parked.wait(2))
            self.assertTrue(self.dispatcher.blocked)
            with self.assertRaises(RuntimeError):
                self.dispatcher.start(command("arm", "nope"))
        finally:
            plugin.release.set()
        proof = StopEvidence("stuck", True, "mock_controller", "parked-1")
        self.assertEqual(self.dispatcher.reconcile_stop("stuck", OwnerBinding("arm", 3), proof)
                         .result(3).held_resources, ())
        self.assertEqual(self.dispatcher.query("stuck").result(3).status, ActionStatus.TIMED_OUT)
        self.dispatcher.review_stops().result(3)
        self.assertFalse(self.dispatcher.blocked)
        with self.assertRaises(RuntimeError):
            self.dispatcher.start(command("arm", "still-gated"))

    def test_a06_sync_report_and_out_of_order(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        binding = OwnerBinding("arm", 3)
        plugin.callback = lambda cmd: self.dispatcher.report(cmd.command_id, binding, ActionStatus.SUCCEEDED)
        self.dispatcher.start(command("arm", "sync")).result(3)
        self.wait_status("sync", ActionStatus.SUCCEEDED)
        self.assertEqual(self.dispatcher.query("sync").result(3).held_resources, ())
        with self.assertRaises(ContractError):
            self.dispatcher.report("sync", binding, ActionStatus.RUNNING).result(3)
        self.assertEqual(self.dispatcher.query("sync").result(3).status, ActionStatus.SUCCEEDED)
        self.assertEqual(len(plugin.commands), 1)

    def test_a10_expired_while_actor_queued_and_observation_ages(self):
        plugin = self.owner(observe=True)
        self.enabled("arm", observe=True)
        plugin.block = True
        self.dispatcher.start(command("arm", "first")).result(3)
        self.assertTrue(plugin.entered.wait(2))
        self.dispatcher.start(command("arm", "second")).result(3)
        time.sleep(0.11)
        plugin.release.set()
        self.wait_status("second", ActionStatus.REJECTED)
        self.assertEqual(len(plugin.commands), 1)
        with self.assertRaises(TimeoutError):
            self.dispatcher.start(command("arm", "third"))

    def test_context_ttl_expires_while_actor_queued(self):
        plugin = self.owner()
        self.enabled("arm", ttl=70)
        plugin.block = True
        self.dispatcher.start(command("arm", "first")).result(3)
        self.assertTrue(plugin.entered.wait(2))
        self.dispatcher.start(command("arm", "second")).result(3)
        time.sleep(0.09)
        plugin.release.set()
        self.wait_status("second", ActionStatus.REJECTED)
        self.assertEqual(len(plugin.commands), 1)

    def test_a12_schema_extra_range_nan_depth_and_unsupported(self):
        plugin = self.owner()
        self.enabled("arm")
        for value in ({"meters": 1, "extra": 1}, {"meters": 101},
                      {"meters": True}, {"meters": float("nan")},
                      {"meters": {"x": []}}):
            raw = command("arm", f"bad-{len(str(value))}")
            raw["params"] = value
            with self.assertRaises(ContractError):
                self.dispatcher.start(raw)
        nested = {}
        cursor = nested
        for _ in range(40):
            cursor["x"] = {}
            cursor = cursor["x"]
        raw = command("arm", "deep")
        raw["params"] = nested
        with self.assertRaises(ContractError):
            self.dispatcher.start(raw)
        bad_manifest = manifest("wheel")
        bad_manifest["actions"][0]["schema"]["oneOf"] = []
        with self.assertRaises(ContractError):
            self.dispatcher.register_owner(OwnerBinding("wheel", 3), self.actors["arm"], bad_manifest)
        self.assertFalse(plugin.commands)

    def test_version_update_revokes_queued_start(self):
        plugin = self.owner()
        self.enabled("arm")
        plugin.block = True
        self.dispatcher.start(command("arm", "first")).result(3)
        self.assertTrue(plugin.entered.wait(2))
        self.dispatcher.start(command("arm", "second")).result(3)
        self.dispatcher.update_versions(catalog_revision=2, config_revision=1,
                                        environment_revision=1, runtime_state="ready")
        plugin.release.set()
        self.wait_status("second", ActionStatus.REJECTED)
        self.assertEqual(len(plugin.commands), 1)
        with self.assertRaises(RuntimeError):
            self.dispatcher.start(command("arm", "third"))

    def test_no_resource_unknown_proof_persists_across_restart(self):
        self.owner()
        self.enabled("arm")
        self.dispatcher.start(command("arm", "unreserved")).result(3)
        self.wait_status("unreserved", ActionStatus.ACCEPTED)
        self.dispatcher.close()
        self.actors["arm"].stop()
        self.ledger.close()
        self.ledger = ActionLedger(self.path)
        self.dispatcher = ActionDispatcher(self.ledger)
        proof = StopEvidence("unreserved", True, "mock_controller", "parked-2")
        with self.assertRaises(RuntimeError):
            self.dispatcher.review_stops().result(3)
        self.assertEqual(self.dispatcher.reconcile_recovered_stop(
            "unreserved", OwnerBinding("arm", 3), proof).result(3).status, ActionStatus.UNKNOWN)
        with self.assertRaises(ValueError):
            self.ledger.reconcile_stop("unreserved", OwnerBinding("arm", 3),
                                       StopEvidence("unreserved", True, "mock_controller", "different")).result(3)
        self.dispatcher.close()
        self.ledger.close()
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(conn.execute(
                "SELECT source,reference FROM stop_evidence WHERE command_id='unreserved'"
            ).fetchone(), ("mock_controller", "parked-2"))
        finally:
            conn.close()
        self.ledger = ActionLedger(self.path)
        self.dispatcher = ActionDispatcher(self.ledger)
        self.assertEqual(len(self.dispatcher.review_stops().result(3)), 1)
        self.assertEqual(self.ledger.reconcile_stop("unreserved", OwnerBinding("arm", 3), proof)
                         .result(3).status, ActionStatus.UNKNOWN)
        self.assertEqual(self.ledger.stop_proof("unreserved", OwnerBinding("arm", 3)).result(3), proof)
        self.assertEqual(len(self.dispatcher.review_stops().result(3)), 1)
        self.assertFalse(self.dispatcher.blocked)
        self.plugins["arm"].close()
        fresh = self.owner("arm")
        self.enabled("arm")
        self.dispatcher.start(command("arm", "after-review")).result(3)
        self.wait_status("after-review", ActionStatus.ACCEPTED)
        self.assertEqual([item.command_id for item in fresh.commands], ["after-review"])

    def test_default_gate_closed_and_recovery_no_replay(self):
        self.owner(resource="joint")
        self.context("arm")
        with self.assertRaises(RuntimeError):
            self.dispatcher.start(command("arm", "closed"))
        self.enabled("arm")
        self.dispatcher.start(command("arm", "unknown")).result(3)
        self.wait_status("unknown", ActionStatus.ACCEPTED)
        self.dispatcher.close()
        self.actors["arm"].stop()
        self.ledger.close()
        self.ledger = ActionLedger(self.path)
        self.dispatcher = ActionDispatcher(self.ledger)
        recovered = self.dispatcher.recover().result(3)
        self.assertEqual(recovered[0].status, ActionStatus.UNKNOWN)
        self.assertTrue(self.dispatcher.blocked)
        self.assertEqual(len(self.plugins["arm"].commands), 1)


if __name__ == "__main__":
    unittest.main()
