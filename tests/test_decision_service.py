from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import Future

from astrbot_ex.core.actions.dispatcher import ActionDispatcher
from astrbot_ex.core.actions.ledger import ActionLedger, OwnerBinding, StopEvidence
from astrbot_ex.core.actions.models import ActionStatus
from astrbot_ex.core.actions.service import ActionService
from astrbot_ex.core.decision.backends import MockBackend
from astrbot_ex.core.decision.models import BackendDecision
from astrbot_ex.core.decision.service import DecisionService
from astrbot_ex.core.plugin_actor import PluginActor
from tests.test_goal_manager import goal_payload, make_catalog


def wait_for(predicate, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate(): return True
        threading.Event().wait(0.003)
    return False


class ActionOwner:
    def __init__(self, owner, dispatcher, *, reject=False, stop_proof=True):
        self.id = owner
        self.dispatcher = dispatcher
        self.reject, self.stop_proof = reject, stop_proof
        self.commands, self.cancels = [], []
        self.started = threading.Event()

    def on_action_command(self, command):
        self.commands.append(command)
        self.started.set()
        return "rejected" if self.reject else "accepted"

    def on_action_cancel(self, command_id, reason):
        self.cancels.append(command_id)
        if self.stop_proof:
            self.dispatcher.report(command_id, OwnerBinding(self.id, 1), ActionStatus.CANCELED,
                stop_evidence=StopEvidence(command_id, True, self.id, command_id))
        return "requested"


class DecisionServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = ActionLedger(Path(self.tmp.name) / "actions.sqlite")
        self.dispatcher = ActionDispatcher(self.ledger)
        self.actors, self.plugins = {}, {}
        self.service = None

    def tearDown(self):
        if self.service: self.service.close()
        self.dispatcher.close()
        for actor in self.actors.values(): actor.stop(2)
        self.ledger.close()
        self.tmp.cleanup()

    def create(self, *, owners=("arm",), resource=None, backend=None, reject_owner=None,
               stop_proof=True, observe=False, decision_mode="execute", **kwargs):
        catalog = make_catalog(owners, resource=resource, observe=observe)
        for entry in catalog.snapshot().entries:
            owner = entry["owner"]
            plugin = ActionOwner(owner, self.dispatcher, reject=owner == reject_owner, stop_proof=stop_proof)
            actor = PluginActor(plugin)
            actor.start()
            self.dispatcher.register_owner(OwnerBinding(owner, 1), actor, entry["manifest"])
            self.actors[owner], self.plugins[owner] = actor, plugin
        self.actions = ActionService(self.ledger, self.dispatcher, catalog, stop_timeout=0.12)
        self.actions.control_mode = "decision"
        self.actions.update_versions(runtime_state="running")
        self.service = DecisionService(self.actions, backend=backend or MockBackend(kind="start"),
                                       max_hz=50, **kwargs)
        self.service.set_mode(decision_mode)
        self.assertTrue(wait_for(lambda: self.service.goals.phase == "idle"))
        self.assertTrue(wait_for(lambda: self.service._last_framework_versions is not None))
        self.assertTrue(wait_for(lambda: self.service._last_catalog_revision == catalog.snapshot().revision))
        return self.service

    def submit(self, n=1, owners=("arm",), **extra):
        return self.service.submit_goal(goal_payload(self.service.goals, n, owners, **extra))

    def test_disabled_unowned_uncertainty_does_not_revoke_sdk_context(self):
        service = self.create(decision_mode="disabled", resource="shared")
        entered, release, progressed = threading.Event(), threading.Event(), threading.Event()
        original_poll, original_progress = service._poll_rows, service._progress
        def parked():
            entered.set()
            release.wait(2)
            original_poll()
        def progress():
            original_progress()
            progressed.set()
        try:
            with patch.object(service, "_poll_rows", side_effect=parked), \
                    patch.object(service, "_progress", side_effect=progress), \
                    patch.object(self.actions, "stop_actions", wraps=self.actions.stop_actions) as stops:
                self.assertTrue(entered.wait(1))
                from astrbot_ex.core.actions.models import ActionCommand
                command = ActionCommand.parse({"schema_version": 1, "command_id": "sdk-uncertain",
                    "ex_session": "sdk-session", "goal_id": "sdk-goal", "goal_revision": 1,
                    "decision_id": "sdk-decision", "owner": "arm", "plugin_generation": 1,
                    "action_id": "arm.move.v1", "operation": "start", "params": {"meters": 1}, "lease_ms": 1000})
                binding = OwnerBinding("arm", 1)
                self.ledger.admit(command, ("shared",), binding, task_id="sdk-task").result(1)
                self.ledger.report(command.command_id, binding, "accepted").result(1)
                self.ledger.report(command.command_id, binding, "unknown").result(1)
                self.dispatcher.set_gate(True)
                self.dispatcher.update_context(ex_session="sdk-session", goal_id="sdk-goal", goal_revision=1,
                    task_id="sdk-task", allowed_actions=["arm.move.v1"], bound_params={"arm.move.v1": {"meters": 1}},
                    runtime_state="running", catalog_revision=self.actions.catalog.snapshot().revision,
                    config_revision=0, environment_revision=1, ttl_ms=10000)
                context, epoch = self.dispatcher._context, self.dispatcher._epoch
                release.set()
                self.assertTrue(progressed.wait(1))
                self.assertTrue(wait_for(lambda: service.status()["unresolved"]))
                with service._condition:
                    self.assertEqual(service.goals.phase, "idle")
                    self.assertIsNone(service.goals.active)
                    self.assertIsNone(service.goals.pending_replace)
                    self.assertFalse(service._stop_pending)
                    self.assertIs(self.dispatcher._context, context)
                    self.assertEqual(self.dispatcher._epoch, epoch)
                    self.assertTrue(service.status()["gate_open"])
                    stops.assert_not_called()
                self.assertEqual(self.plugins["arm"].cancels, [])
                self.assertEqual(self.ledger.get(command.command_id).result(1).held_resources, ("shared",))
                self.assertTrue(self.actions.status()["blocked"])
                self.assertIsNone(self.ledger.stop_proof(command.command_id, binding).result(1))
        finally:
            release.set()
            if self.ledger.get("sdk-uncertain").result(1) is not None:
                self.ledger.reconcile_stop("sdk-uncertain", OwnerBinding("arm", 1),
                    StopEvidence("sdk-uncertain", True, "fixture", "parked")).result(1)

    def test_disabled_pending_goal_still_blocks_on_uncertain_actions(self):
        service = self.create(decision_mode="disabled", resource="shared", backend=MockBackend(kind="wait"))
        from astrbot_ex.core.actions.models import ActionCommand
        command = ActionCommand.parse({"schema_version": 1, "command_id": "pending-uncertain",
            "ex_session": "sdk-session", "goal_id": "sdk-goal", "goal_revision": 1,
            "decision_id": "sdk-decision", "owner": "arm", "plugin_generation": 1,
            "action_id": "arm.move.v1", "operation": "start", "params": {"meters": 1}, "lease_ms": 1000})
        binding = OwnerBinding("arm", 1)
        entered, release = threading.Event(), threading.Event()
        original = service._poll_rows
        def parked():
            entered.set()
            release.wait(2)
            original()
        try:
            with patch.object(service, "_poll_rows", side_effect=parked), \
                    patch.object(self.actions, "stop_actions", wraps=self.actions.stop_actions) as stops:
                self.assertTrue(entered.wait(1))
                self.ledger.admit(command, ("shared",), binding, task_id="sdk-task").result(1)
                self.ledger.report(command.command_id, binding, "accepted").result(1)
                self.ledger.report(command.command_id, binding, "unknown").result(1)
                self.submit()
                release.set()
                self.assertTrue(wait_for(lambda: service.goals.phase == "blocked"))
                self.assertTrue(wait_for(lambda: stops.call_count > 0))
                self.assertFalse(service.status()["gate_open"])
                self.assertIsNone(service.goals.active)
                self.assertEqual(service.goals.pending_replace.payload()["goal_id"], "goal-1")
                self.assertEqual(self.plugins["arm"].commands, [])
                self.assertEqual(self.ledger.get(command.command_id).result(1).held_resources, ("shared",))
        finally:
            release.set()
            if self.ledger.get(command.command_id).result(1) is not None:
                self.ledger.reconcile_stop(command.command_id, binding,
                    StopEvidence(command.command_id, True, "fixture", "parked")).result(1)

    def test_disabled_unowned_ledger_error_does_not_revoke_sdk_context(self):
        service = self.create(decision_mode="disabled")
        self.dispatcher.set_gate(True)
        self.dispatcher.update_context(ex_session="sdk-session", goal_id="sdk-goal", goal_revision=1,
            task_id="sdk-task", allowed_actions=["arm.move.v1"], bound_params={"arm.move.v1": {"meters": 1}},
            runtime_state="running", catalog_revision=self.actions.catalog.snapshot().revision,
            config_revision=0, environment_revision=1, ttl_ms=10000)
        context, epoch = self.dispatcher._context, self.dispatcher._epoch
        with patch.object(service, "_poll_rows", side_effect=OSError("injected ledger failure")), \
                patch.object(self.actions, "stop_actions", wraps=self.actions.stop_actions) as stops:
            self.assertTrue(wait_for(lambda: service.status()["error"] == "injected ledger failure"))
            with service._condition:
                self.assertEqual(service.goals.phase, "idle")
                self.assertFalse(service._stop_pending)
                self.assertIs(self.dispatcher._context, context)
                self.assertEqual(self.dispatcher._epoch, epoch)
                stops.assert_not_called()

    def test_disabled_explicit_stop_and_mode_change_still_revoke_sdk_context(self):
        service = self.create(decision_mode="disabled")
        for trigger in (lambda: service.request_stop("explicit_sdk_stop"), lambda: service.set_mode("shadow")):
            with self.subTest(trigger=trigger):
                self.dispatcher.set_gate(True)
                self.dispatcher.update_context(ex_session="sdk-session", goal_id="sdk-goal", goal_revision=1,
                    task_id="sdk-task", allowed_actions=["arm.move.v1"], bound_params={"arm.move.v1": {"meters": 1}},
                    runtime_state="running", catalog_revision=self.actions.catalog.snapshot().revision,
                    config_revision=0, environment_revision=1, ttl_ms=10000)
                with patch.object(self.actions, "stop_actions", wraps=self.actions.stop_actions) as stops:
                    trigger()
                    self.assertIsNone(self.dispatcher._context)
                    self.assertFalse(service.status()["gate_open"])
                    self.assertTrue(wait_for(lambda: stops.call_count > 0 and service.goals.phase == "idle"))

    def test_completed_synchronous_stop_is_not_replayed_against_new_sdk_context(self):
        service = self.create(decision_mode="disabled")
        entered, release, progressed = threading.Event(), threading.Event(), threading.Event()
        original_poll, original_progress = service._poll_rows, service._progress
        def parked():
            entered.set()
            release.wait(2)
            original_poll()
        def progress():
            original_progress()
            progressed.set()
        try:
            with patch.object(service, "_poll_rows", side_effect=parked), \
                    patch.object(service, "_progress", side_effect=progress):
                self.assertTrue(entered.wait(1))
                service.request_stop("synchronous_controller_stop")
                stop_epoch = self.dispatcher._epoch
                self.assertTrue(self.actions.stop_actions("synchronous_controller_stop"))
                self.assertGreaterEqual(self.actions._stop_proof_epoch, stop_epoch)
                self.dispatcher.set_gate(True)
                self.dispatcher.update_context(ex_session="sdk-session", goal_id="sdk-goal", goal_revision=1,
                    task_id="sdk-task", allowed_actions=["arm.move.v1"], bound_params={"arm.move.v1": {"meters": 1}},
                    runtime_state="running", catalog_revision=self.actions.catalog.snapshot().revision,
                    config_revision=0, environment_revision=1, ttl_ms=10000)
                context, epoch = self.dispatcher._context, self.dispatcher._epoch
                from astrbot_ex.core.actions.models import ActionCommand
                command = ActionCommand.parse({"schema_version": 1, "command_id": "post-stop-sdk-unknown",
                    "ex_session": "sdk-session", "goal_id": "sdk-goal", "goal_revision": 1,
                    "decision_id": "sdk-decision", "owner": "arm", "plugin_generation": 1,
                    "action_id": "arm.move.v1", "operation": "start", "params": {"meters": 1}, "lease_ms": 1000})
                binding = OwnerBinding("arm", 1)
                self.ledger.admit(command, ("shared",), binding, task_id="sdk-task").result(1)
                self.ledger.report(command.command_id, binding, "accepted").result(1)
                self.ledger.report(command.command_id, binding, "unknown").result(1)
                with patch.object(self.actions, "request_stops", wraps=self.actions.request_stops) as stops:
                    release.set()
                    self.assertTrue(progressed.wait(1))
                    self.assertTrue(wait_for(lambda: service.goals.phase == "idle"))
                    with service._condition:
                        self.assertIs(self.dispatcher._context, context)
                        self.assertEqual(self.dispatcher._epoch, epoch)
                        self.assertFalse(service._stop_pending)
                        stops.assert_not_called()
        finally:
            release.set()
            if self.ledger.get("post-stop-sdk-unknown").result(1) is not None:
                self.ledger.reconcile_stop("post-stop-sdk-unknown", OwnerBinding("arm", 1),
                    StopEvidence("post-stop-sdk-unknown", True, "fixture", "parked")).result(1)

    def test_completed_synchronous_stop_then_poll_failure_remains_passive(self):
        service = self.create(decision_mode="disabled")
        entered, release = threading.Event(), threading.Event()
        def parked_failure():
            entered.set()
            release.wait(2)
            raise OSError("post-stop poll failed")
        try:
            with patch.object(service, "_poll_rows", side_effect=parked_failure):
                self.assertTrue(entered.wait(1))
                service.request_stop("synchronous_controller_stop")
                self.assertTrue(self.actions.stop_actions("synchronous_controller_stop"))
                self.dispatcher.set_gate(True)
                self.dispatcher.update_context(ex_session="sdk-session", goal_id="sdk-goal", goal_revision=1,
                    task_id="sdk-task", allowed_actions=["arm.move.v1"], bound_params={"arm.move.v1": {"meters": 1}},
                    runtime_state="running", catalog_revision=self.actions.catalog.snapshot().revision,
                    config_revision=0, environment_revision=1, ttl_ms=10000)
                context, epoch = self.dispatcher._context, self.dispatcher._epoch
                with patch.object(self.actions, "request_stops", wraps=self.actions.request_stops) as stops:
                    release.set()
                    self.assertTrue(wait_for(lambda: service.status()["error"] == "post-stop poll failed"))
                    with service._condition:
                        self.assertEqual(service.goals.phase, "idle")
                        self.assertFalse(service._stop_pending)
                        self.assertIs(self.dispatcher._context, context)
                        self.assertEqual(self.dispatcher._epoch, epoch)
                        stops.assert_not_called()
        finally:
            release.set()

    def test_disabling_active_goal_still_cancels_before_releasing_authorization(self):
        service = self.create(resource="shared", stop_proof=False)
        self.submit()
        self.assertTrue(self.plugins["arm"].started.wait(1))
        cid = self.plugins["arm"].commands[0].command_id
        service.set_mode("disabled")
        self.assertFalse(service.status()["gate_open"])
        self.assertTrue(wait_for(lambda: self.plugins["arm"].cancels))
        self.assertTrue(wait_for(lambda: service.goals.phase == "blocked"))
        self.assertIsNotNone(service.goals.active)
        self.assertEqual(self.ledger.get(cid).result(1).held_resources, ("shared",))
        self.assertEqual(len(self.plugins["arm"].commands), 1)
        self.dispatcher.reconcile_stop(cid, OwnerBinding("arm", 1),
            StopEvidence(cid, True, "fixture", "parked")).result(1)
        self.assertTrue(service.review().result(2)["new_authorization_required"])
        self.assertIsNone(service.goals.active)
        self.assertFalse(service.status()["gate_open"])

    def test_worker_io_failure_with_active_goal_still_revokes_and_requests_stop(self):
        service = self.create(backend=MockBackend(kind="wait"))
        self.submit()
        self.assertTrue(wait_for(lambda: service.goals.phase == "active"))
        original = service._poll_rows
        failures = [True]
        def fail_once():
            if failures:
                failures.pop()
                raise OSError("active ledger failure")
            return original()
        with patch.object(service, "_poll_rows", side_effect=fail_once), \
                patch.object(self.actions, "stop_actions", wraps=self.actions.stop_actions) as stops:
            self.assertTrue(wait_for(lambda: service.goals.phase == "blocked"))
            self.assertFalse(service.status()["gate_open"])
            self.assertTrue(wait_for(lambda: stops.call_count > 0))
            self.assertIsNotNone(service.goals.active)

    def test_persistent_poll_failure_cancels_real_running_owner_without_proof(self):
        service = self.create(resource="shared", stop_proof=False)
        self.submit()
        self.assertTrue(self.plugins["arm"].started.wait(1))
        cid = self.plugins["arm"].commands[0].command_id
        self.assertTrue(wait_for(lambda: self.ledger.get(cid).result(1).status == "accepted"))
        self.dispatcher.report(cid, OwnerBinding("arm", 1), "running").result(1)
        goal = service.goals.active
        try:
            with patch.object(service, "_poll_rows", side_effect=OSError("persistent read failure")) as polls, \
                    patch.object(self.actions, "stop_actions", wraps=self.actions.stop_actions) as stops:
                self.assertTrue(wait_for(lambda: service.goals.phase == "blocked"))
                self.assertFalse(service.status()["gate_open"])
                self.assertTrue(wait_for(lambda: stops.call_count > 0))
                self.assertTrue(wait_for(lambda: self.plugins["arm"].cancels))
                self.assertTrue(wait_for(lambda: service._stop_attempts == 3 and not service._stop_pending))
                epoch, gate_epoch = self.dispatcher._epoch, service.goals.gate_epoch
                polled = polls.call_count
                self.assertTrue(wait_for(lambda: polls.call_count > polled))
                self.assertEqual(stops.call_count, 1)
                self.assertEqual(self.dispatcher._epoch, epoch)
                self.assertEqual(service.goals.gate_epoch, gate_epoch)
                self.assertIs(service.goals.active, goal)
                self.assertTrue(service._control.is_alive())
                self.assertIn("stop proof pending", service.status()["stop_error"])
                self.assertEqual(self.ledger.get(cid).result(1).held_resources, ("shared",))
                self.assertIsNone(self.ledger.stop_proof(cid, OwnerBinding("arm", 1)).result(1))
        finally:
            self.dispatcher.reconcile_stop(cid, OwnerBinding("arm", 1),
                StopEvidence(cid, True, "fixture", "parked")).result(1)

    def test_persistent_poll_failure_preserves_pending_replacement_and_responds_to_review(self):
        service = self.create(resource="shared", stop_proof=False)
        self.submit()
        self.assertTrue(self.plugins["arm"].started.wait(1))
        cid = self.plugins["arm"].commands[0].command_id
        self.assertTrue(wait_for(lambda: self.ledger.get(cid).result(1).status == "accepted"))
        with patch.object(service, "_poll_rows", side_effect=OSError("persistent replacement read failure")), \
                patch.object(self.actions, "stop_actions", wraps=self.actions.stop_actions) as stops:
            self.assertTrue(wait_for(lambda: service.goals.phase == "blocked"))
            self.submit(2)
            self.assertTrue(wait_for(lambda: stops.call_count > 0 and self.plugins["arm"].cancels))
            self.assertEqual(service.goals.phase, "blocked")
            self.assertEqual(service.goals.active.payload()["goal_id"], "goal-1")
            self.assertEqual(service.goals.pending_replace.payload()["goal_id"], "goal-2")
            self.assertEqual(self.ledger.get(cid).result(1).held_resources, ("shared",))
            self.assertEqual(len(self.plugins["arm"].commands), 1)
            current = self.ledger.get(cid).result(1)
            if current.status not in {ActionStatus.UNKNOWN, ActionStatus.TIMED_OUT, ActionStatus.FAILED}:
                self.dispatcher.report(cid, OwnerBinding("arm", 1), "unknown").result(1)
            self.dispatcher.reconcile_stop(cid, OwnerBinding("arm", 1),
                StopEvidence(cid, True, "fixture", "parked")).result(1)
            self.assertTrue(service.review().result(2)["new_authorization_required"])
            self.assertIsNone(service.goals.active)
            self.assertIsNone(service.goals.pending_replace)
            self.assertFalse(service.status()["gate_open"])
            self.assertEqual(len(self.plugins["arm"].commands), 1)

    def test_persistent_poll_failure_does_not_wait_for_live_provider_to_stop(self):
        backend = MockBackend(kind="start", block=True)
        service = self.create(backend=backend)
        self.submit()
        self.assertTrue(backend.entered.wait(1))
        with patch.object(service, "_poll_rows", side_effect=OSError("persistent provider read failure")), \
                patch.object(self.actions, "stop_actions", wraps=self.actions.stop_actions) as stops:
            self.assertTrue(wait_for(lambda: service.goals.phase == "blocked"))
            before = time.monotonic()
            service.request_stop("provider_live_stop")
            self.assertFalse(service.status()["gate_open"])
            self.assertLess(time.monotonic() - before, 0.1)
            self.assertTrue(wait_for(lambda: stops.call_count > 0))
            self.assertFalse(backend.release.is_set())
            self.assertTrue(service._backend_worker.is_alive())
            self.assertTrue(service._control.is_alive())
            self.assertIsNotNone(service.goals.active)
            self.assertEqual(self.plugins["arm"].commands, [])

    def test_persistent_poll_and_ledger_failure_keeps_resources_and_bounds_stop_attempts(self):
        service = self.create(resource="shared", stop_proof=False)
        self.submit()
        self.assertTrue(self.plugins["arm"].started.wait(1))
        cid = self.plugins["arm"].commands[0].command_id
        self.assertTrue(wait_for(lambda: self.ledger.get(cid).result(1).status == "accepted"))
        self.dispatcher.report(cid, OwnerBinding("arm", 1), "running").result(1)
        goal = service.goals.active
        failed = Future()
        failed.set_exception(OSError("persistent ledger unavailable"))
        try:
            with patch.object(service, "_poll_rows", side_effect=OSError("persistent read failure")) as polls, \
                    patch.object(self.ledger, "list_commands", return_value=failed), \
                    patch.object(self.actions, "stop_actions", wraps=self.actions.stop_actions) as stops:
                self.assertTrue(wait_for(lambda: service.goals.phase == "blocked"))
                self.assertTrue(wait_for(lambda: stops.call_count > 0))
                self.assertTrue(wait_for(lambda: service._stop_attempts == 3 and not service._stop_pending))
                epoch, gate_epoch = self.dispatcher._epoch, service.goals.gate_epoch
                polled = polls.call_count
                self.assertTrue(wait_for(lambda: polls.call_count > polled))
                self.assertEqual(stops.call_count, 1)
                self.assertEqual(self.dispatcher._epoch, epoch)
                self.assertEqual(service.goals.gate_epoch, gate_epoch)
                self.assertIs(service.goals.active, goal)
                self.assertTrue(service._control.is_alive())
                self.assertIn("unavailable", service.status()["stop_error"])
                self.assertIn("persistent ledger unavailable", service.status()["stop_error"])
                self.assertEqual(self.ledger.get(cid).result(1).held_resources, ("shared",))
                self.assertIsNone(self.ledger.stop_proof(cid, OwnerBinding("arm", 1)).result(1))
        finally:
            current = self.ledger.get(cid).result(1)
            if current.status not in {ActionStatus.UNKNOWN, ActionStatus.TIMED_OUT, ActionStatus.FAILED}:
                self.dispatcher.report(cid, OwnerBinding("arm", 1), "unknown").result(1)
            self.dispatcher.reconcile_stop(cid, OwnerBinding("arm", 1),
                StopEvidence(cid, True, "fixture", "parked")).result(1)

    def test_persistent_unowned_poll_failure_never_revokes_or_cancels_sdk_context(self):
        service = self.create(decision_mode="disabled")
        self.dispatcher.set_gate(True)
        self.dispatcher.update_context(ex_session="sdk-session", goal_id="sdk-goal", goal_revision=1,
            task_id="sdk-task", allowed_actions=["arm.move.v1"], bound_params={"arm.move.v1": {"meters": 1}},
            runtime_state="running", catalog_revision=self.actions.catalog.snapshot().revision,
            config_revision=0, environment_revision=1, ttl_ms=10000)
        context, epoch = self.dispatcher._context, self.dispatcher._epoch
        with patch.object(service, "_poll_rows", side_effect=OSError("persistent passive read failure")) as polls, \
                patch.object(self.actions, "revoke", wraps=self.actions.revoke) as revoke, \
                patch.object(self.actions, "stop_actions", wraps=self.actions.stop_actions) as stops, \
                patch.object(self.actions, "cancel", wraps=self.actions.cancel) as cancel:
            self.assertTrue(wait_for(lambda: polls.call_count >= 3))
            self.assertEqual(service.goals.phase, "idle")
            self.assertFalse(service._stop_pending)
            self.assertEqual(self.dispatcher._epoch, epoch)
            self.assertIs(self.dispatcher._context, context)
            self.assertTrue(service.status()["gate_open"])
            revoke.assert_not_called()
            stops.assert_not_called()
            cancel.assert_not_called()
            self.assertEqual(self.plugins["arm"].cancels, [])
            self.assertTrue(service._control.is_alive())

    def test_proof_retries_keep_same_gate_epoch_and_do_not_clear_blocked_goal(self):
        service = self.create(resource="shared", stop_proof=False)
        self.submit()
        self.assertTrue(self.plugins["arm"].started.wait(1))
        cid = self.plugins["arm"].commands[0].command_id
        self.assertTrue(wait_for(lambda: self.ledger.get(cid).result(1).status == "accepted"))
        self.dispatcher.report(cid, OwnerBinding("arm", 1), "running").result(1)
        goal, epochs = service.goals.active, []
        original = self.actions.await_stop_proof
        def prove(*args, **kwargs):
            epochs.append((self.dispatcher._epoch, service.goals.gate_epoch))
            if len(epochs) == 2:
                current = self.ledger.get(cid).result(1)
                if current.status not in {ActionStatus.UNKNOWN, ActionStatus.TIMED_OUT, ActionStatus.FAILED}:
                    self.dispatcher.report(cid, OwnerBinding("arm", 1), "unknown").result(1)
                self.dispatcher.reconcile_stop(cid, OwnerBinding("arm", 1),
                    StopEvidence(cid, True, "fixture", "parked")).result(1)
            return original(*args, **kwargs)
        with patch.object(service, "_poll_rows", side_effect=OSError("persistent proof read failure")), \
                patch.object(self.actions, "await_stop_proof", side_effect=prove):
            self.assertTrue(wait_for(lambda: len(epochs) >= 2 and not service._stop_pending
                                     and service.status()["stop_error"] == ""))
            self.assertEqual(epochs[0], epochs[1])
            self.assertEqual(service.goals.phase, "blocked")
            self.assertIs(service.goals.active, goal)
            self.assertFalse(service.status()["gate_open"])
            self.assertEqual(service.status()["stop_error"], "")
            self.assertEqual(self.ledger.get(cid).result(1).held_resources, ())
            self.assertIsNotNone(self.ledger.stop_proof(cid, OwnerBinding("arm", 1)).result(1))

    def test_stop_request_exception_has_bounded_backoff_without_poll_dependency(self):
        service = self.create(backend=MockBackend(kind="wait"))
        self.submit()
        self.assertTrue(wait_for(lambda: service.goals.phase == "active"))
        goal = service.goals.active
        attempts = []
        def failed(*args, **kwargs):
            attempts.append(time.monotonic())
            raise OSError("persistent stop request failure")
        with patch.object(service, "_poll_rows", side_effect=OSError("persistent read failure")) as polls, \
                patch.object(self.actions, "stop_actions", side_effect=failed) as stops, \
                patch.object(self.actions, "request_stops", side_effect=failed) as requests:
            self.assertTrue(wait_for(lambda: service._stop_attempts == 3 and not service._stop_pending))
            self.assertEqual(stops.call_count, 1)
            self.assertEqual(requests.call_count, 2)
            self.assertEqual(len(attempts), 3)
            self.assertGreaterEqual(attempts[1] - attempts[0], 0.09)
            self.assertGreaterEqual(attempts[2] - attempts[1], 0.19)
            epoch = service.goals.gate_epoch
            count = polls.call_count
            self.assertTrue(wait_for(lambda: polls.call_count > count))
            self.assertEqual(service.goals.gate_epoch, epoch)
            self.assertIs(service.goals.active, goal)
            self.assertEqual(service.goals.phase, "blocked")
            self.assertFalse(service.status()["gate_open"])
            self.assertIn("persistent stop request failure", service.status()["stop_error"])

    def test_observation_pagination_uses_one_total_budget_before_next_submission(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        now, budgets = [0.0], []
        class PageFuture(Future):
            def result(self, timeout=None):
                budgets.append(timeout)
                now[0] += 0.4
                return super().result(timeout)
        page = PageFuture()
        page.set_result(tuple(SimpleNamespace(command_id=str(index)) for index in range(500)))
        ledger = Mock(list_commands=Mock(return_value=page))
        service = DecisionService.__new__(DecisionService)
        service.actions = SimpleNamespace(ledger=ledger)
        service._io_timeout = 1
        with patch("astrbot_ex.core.decision.service.time", SimpleNamespace(monotonic=lambda: now[0])):
            with self.assertRaisesRegex(TimeoutError, "observation read deadline"):
                service._poll_rows()
        self.assertEqual(ledger.list_commands.call_count, 3)
        self.assertEqual(len(budgets), 3)
        for actual, expected in zip(budgets, (1.0, 0.6, 0.2)):
            self.assertAlmostEqual(actual, expected)

    def test_disabled_startup_recovery_remains_blocked_until_explicit_review(self):
        from astrbot_ex.core.actions.models import ActionCommand
        command = ActionCommand.parse({"schema_version": 1, "command_id": "recovered",
            "ex_session": "old-session", "goal_id": "old-goal", "goal_revision": 1,
            "decision_id": "old-decision", "owner": "arm", "plugin_generation": 1,
            "action_id": "arm.move.v1", "operation": "start", "params": {"meters": 1}, "lease_ms": 1000})
        binding = OwnerBinding("arm", 1)
        self.ledger.admit(command, ("shared",), binding, task_id="old-task").result(1)
        self.dispatcher.close()
        self.ledger.close()
        self.ledger = ActionLedger(Path(self.tmp.name) / "actions.sqlite")
        self.dispatcher = ActionDispatcher(self.ledger)
        self.assertTrue(self.dispatcher.blocked)
        self.actions = ActionService(self.ledger, self.dispatcher, make_catalog(), stop_timeout=0.05)
        self.service = service = DecisionService(self.actions)
        self.assertEqual(service.goals.phase, "blocked")
        self.assertEqual(service.goals.reason, "startup_recovery_requires_explicit_review")
        self.assertFalse(service.status()["gate_open"])
        with self.assertRaises(RuntimeError):
            service.review().result(2)
        self.assertEqual(self.ledger.get(command.command_id).result(1).held_resources, ("shared",))
        self.dispatcher.reconcile_recovered_stop(command.command_id, binding,
            StopEvidence(command.command_id, True, "fixture", "parked")).result(1)
        self.assertTrue(service.review().result(2)["new_authorization_required"])
        self.assertEqual(service.goals.phase, "idle")
        self.assertFalse(service.status()["gate_open"])
        self.assertIsNone(service.goals.active)
        self.assertIsNone(service.goals.pending_replace)

    def test_keep_does_not_repeat_start_and_completion_awaits_committed_success(self):
        service = self.create()
        self.submit(completion={"required_success_actions": ["arm.move.v1"]})
        self.assertTrue(self.plugins["arm"].started.wait(1))
        cid = self.plugins["arm"].commands[0].command_id
        self.assertTrue(wait_for(lambda: self.dispatcher.query(cid).result(1).status == "accepted"))
        service.backend.kind = "keep"
        for _ in range(20): service.tick()
        self.assertTrue(wait_for(lambda: any(d["outcome"] == "no_dispatch" for d in service.status()["decisions"])))
        self.assertEqual(len(self.plugins["arm"].commands), 1)
        self.assertEqual(service.goals.phase, "active")
        self.dispatcher.report(cid, OwnerBinding("arm", 1), "succeeded").result(1)
        self.assertTrue(wait_for(lambda: service.goals.phase == "awaiting_llm"))
        self.assertFalse(service.status()["gate_open"])
        self.assertEqual(service.goals.active.payload()["goal_id"], "goal-1")

    def test_G02_100_replacements_blocked_backend_late_result_is_discarded(self):
        backend = MockBackend(kind="start", block=True)
        service = self.create(backend=backend)
        self.submit()
        self.assertTrue(backend.entered.wait(1))
        for n in range(2, 102): self.submit(n)
        self.assertEqual(backend.calls, 1)
        self.assertTrue(wait_for(lambda: service.goals.active and service.goals.active.revision == 101))
        backend.release.set()
        service.tick()
        self.assertTrue(wait_for(lambda: self.plugins["arm"].commands))
        self.assertEqual({c.goal_revision for c in self.plugins["arm"].commands}, {101})
        self.assertTrue(any(d["outcome"] == "discarded" and "changed" in d["reason_code"]
                            for d in service.status()["decisions"]))

    def test_G03_cancel_without_committed_evidence_blocks_new_start(self):
        service = self.create(stop_proof=False)
        self.submit()
        self.assertTrue(self.plugins["arm"].started.wait(1))
        self.submit(2)
        self.assertFalse(service.status()["gate_open"])
        self.assertTrue(wait_for(lambda: service.goals.phase == "blocked"))
        self.assertEqual(len(self.plugins["arm"].commands), 1)
        self.assertEqual(service.goals.pending_replace.payload()["goal_id"], "goal-2")
        self.assertEqual(service.goals.reason, "stop_not_proven")

    def test_G04_timeout_keeps_single_live_request_and_stop_status_are_prompt(self):
        backend = MockBackend(kind="start", block=True)
        service = self.create(backend=backend, backend_timeout=0.03)
        self.submit()
        self.assertTrue(backend.entered.wait(1))
        self.assertTrue(wait_for(lambda: service.status()["backend_timed_out"]))
        for _ in range(1000): service.tick()
        before = time.perf_counter_ns()
        service.request_stop("test_stop")
        state = service.status()
        elapsed = (time.perf_counter_ns() - before) / 1e9
        self.assertLess(elapsed, 0.1)
        self.assertFalse(state["gate_open"])
        self.assertEqual(backend.calls, 1)
        self.assertTrue(state["backend_live"])
        backend.release.set()
        self.assertTrue(wait_for(lambda: not service.status()["backend_live"]))
        self.assertEqual(self.plugins["arm"].commands, [])
        samples = []
        for _ in range(20):
            stamp = time.perf_counter_ns()
            service.request_stop("test_stop")
            self.assertFalse(service.status()["gate_open"])
            samples.append((time.perf_counter_ns() - stamp) / 1e6)
        p95 = sorted(samples)[18]
        self.assertLess(max(samples), 100)
        print(f"G04 event-latch gate+status n=20 p95_ms={p95:.3f} max_ms={max(samples):.3f}; single_live_calls={backend.calls}")

    def test_G06_independent_choices_share_resource_zero_double_start(self):
        service = self.create(owners=("arm", "base"), resource="shared")
        self.submit(owners=("arm", "base"))
        self.assertTrue(wait_for(lambda: any(d["reason_code"] == "resource_busy" for d in service.status()["decisions"])))
        self.assertEqual(sum(len(p.commands) for p in self.plugins.values()), 0)

    def test_G07_config_change_discards_bound_response(self):
        backend = MockBackend(kind="start", block=True)
        service = self.create(backend=backend)
        self.submit()
        self.assertTrue(backend.entered.wait(1))
        service.configuration_changed()
        backend.release.set()
        self.assertTrue(wait_for(lambda: not service.status()["backend_live"] and any(
            d["reason_code"] == "config_revision_changed" for d in service.status()["decisions"])))
        self.assertEqual(self.plugins["arm"].commands, [])
        self.assertTrue(any("config_revision_changed" == d["reason_code"]
                            for d in service.status()["decisions"]))

    def test_G08_offline_lease_expires_and_restart_does_not_authorize(self):
        service = self.create(backend=MockBackend(kind="wait"))
        self.submit(lease_ms=30)
        self.assertTrue(wait_for(lambda: service.goals.reason == "new_authorization_required"))
        self.assertFalse(service.status()["gate_open"])
        self.assertEqual(self.plugins["arm"].commands, [])
        old_session = service.goals.ex_session
        service.close()
        self.service = DecisionService(self.actions)
        self.assertNotEqual(old_session, self.service.goals.ex_session)
        self.assertEqual(self.service.mode, "disabled")
        self.assertIsNone(self.service.goals.active)

    def test_G10_late_owner_rejection_rolls_back_started_peer(self):
        service = self.create(owners=("arm", "base"), reject_owner="base")
        self.submit(owners=("arm", "base"))
        self.assertTrue(wait_for(lambda: service.status()["decisions"] and any(
            d["outcome"] == "partial_execution" for d in service.status()["decisions"])))
        self.assertTrue(wait_for(lambda: self.plugins["arm"].cancels))
        self.assertFalse(service.status()["gate_open"])
        self.assertEqual(len(self.plugins["arm"].commands), 1)
        self.assertNotIn("succeeded", [d["outcome"] for d in service.status()["decisions"]])

    def test_backend_invented_option_cannot_dispatch(self):
        class Invent(MockBackend):
            def decide(self, snapshot):
                result = super().decide(snapshot)
                result.choices[0]["option_id"] = "invented"
                return result
        service = self.create(backend=Invent(kind="start"))
        self.submit()
        self.assertTrue(wait_for(lambda: any("unknown_action" in d["reason_code"]
                                            for d in service.status()["decisions"])))
        self.assertEqual(self.plugins["arm"].commands, [])

    def test_G05_source_restarts_during_backend_request_result_is_discarded(self):
        backend = MockBackend(kind="start", block=True)
        service = self.create(backend=backend, observe=True)
        service.observations.ingest("arm_pose", {"position": 1}, source_epoch="sensor-a", seq=10,
                                    source_timestamp=time.time())
        self.submit()
        self.assertTrue(backend.entered.wait(1))
        service.observations.ingest("arm_pose", {"position": 2}, source_epoch="sensor-b", seq=1,
                                    source_timestamp=time.time())
        backend.release.set()
        self.assertTrue(wait_for(lambda: not service.status()["backend_live"] and any(
            d["reason_code"] == "observation_source_epoch_changed" for d in service.status()["decisions"])))
        self.assertEqual(self.plugins["arm"].commands, [])
        self.assertTrue(any(d["reason_code"] == "observation_source_epoch_changed"
                            for d in service.status()["decisions"]))

    def test_G05_observation_ages_during_backend_call_no_physical_start(self):
        backend = MockBackend(kind="start", block=True)
        service = self.create(backend=backend, observe=True)
        service.observations.ingest("arm_pose", {"position": 1}, source_epoch="sensor-a", seq=1,
                                    source_timestamp=time.time())
        self.submit()
        self.assertTrue(backend.entered.wait(1))
        # Advance only the observation clock; no sleep hides worker responsiveness.
        service.observations._clock = lambda: time.monotonic_ns() + 100_000_000
        backend.release.set()
        self.assertTrue(wait_for(lambda: not service.status()["backend_live"] and any(
            d["reason_code"] == "observation_expired" for d in service.status()["decisions"])))
        self.assertEqual(self.plugins["arm"].commands, [])
        self.assertTrue(any(d["reason_code"] == "observation_expired"
                            for d in service.status()["decisions"]))

    def test_G07_catalog_plugin_generation_change_rejects_late_start(self):
        backend = MockBackend(kind="start", block=True)
        service = self.create(backend=backend)
        self.submit()
        self.assertTrue(backend.entered.wait(1))
        from astrbot_ex.core.decision.catalog import CapabilityInput
        from astrbot_ex.core.actions.models import parse_action_manifest
        entry = service.catalog.snapshot().entries[0]
        service.catalog.refresh([CapabilityInput("arm", 2, parse_action_manifest(entry["manifest"], owner="arm"),
            {}, entry["guide"], True, "2")])
        backend.release.set()
        self.assertTrue(wait_for(lambda: not service.status()["backend_live"] and any(
            d["reason_code"] in {"catalog_revision_changed", "plugin_generations_changed"}
            for d in service.status()["decisions"])))
        self.assertEqual(self.plugins["arm"].commands, [])
        self.assertTrue(any(d["reason_code"] in {"catalog_revision_changed", "plugin_generations_changed"}
                            for d in service.status()["decisions"]))

    def test_G07_environment_generation_change_rejects_late_start(self):
        class Environment:
            generation = 1
            def snapshot(self): return {"generation": self.generation, "phase": "idle"}
        environment = Environment()
        backend = MockBackend(kind="start", block=True)
        service = self.create(backend=backend, environment=environment)
        self.submit()
        self.assertTrue(backend.entered.wait(1))
        environment.generation = 2
        backend.release.set()
        self.assertTrue(wait_for(lambda: not service.status()["backend_live"] and any(
            d["reason_code"] == "environment_generation_changed" for d in service.status()["decisions"])))
        self.assertEqual(self.plugins["arm"].commands, [])
        self.assertTrue(any(d["reason_code"] == "environment_generation_changed"
                            for d in service.status()["decisions"]))

    def test_G04_sqlite_latch_does_not_hold_stop_status_runtime_lock(self):
        service = self.create(backend=MockBackend(kind="wait"), io_timeout=0.1)
        entered = threading.Event()
        pending = Future()
        def parked(**kwargs):
            entered.set()
            return pending
        with patch.object(self.ledger, "list_commands", side_effect=parked):
            self.assertTrue(entered.wait(1))
            begin = time.perf_counter_ns()
            service.request_stop("sqlite_latch")
            self.assertFalse(service.status()["gate_open"])
            self.assertLess((time.perf_counter_ns() - begin) / 1e9, 0.1)
            pending.set_result(())

    def test_explicit_review_clears_block_but_discards_pending_authorization(self):
        service = self.create(stop_proof=False)
        self.submit()
        self.assertTrue(self.plugins["arm"].started.wait(1))
        self.submit(2)
        self.assertTrue(wait_for(lambda: service.goals.phase == "blocked"))
        cid = self.plugins["arm"].commands[0].command_id
        self.dispatcher.reconcile_stop(cid, OwnerBinding("arm", 1),
            StopEvidence(cid, True, "arm", cid)).result(1)
        self.assertTrue(service.review().result(2)["new_authorization_required"])
        self.assertIsNone(service.goals.active)
        self.assertIsNone(service.goals.pending_replace)
        self.assertFalse(service.status()["gate_open"])
        self.assertEqual(len(self.plugins["arm"].commands), 1)

    def test_shadow_replan_has_no_goal_or_dispatch_side_effect(self):
        service = self.create(backend=MockBackend(kind="request_replan"))
        service.set_mode("shadow")
        self.assertTrue(wait_for(lambda: service.goals.phase == "idle"))
        self.submit()
        self.assertTrue(wait_for(lambda: any(d["outcome"] == "shadow" for d in service.status()["decisions"])))
        self.assertEqual(service.goals.phase, "active")
        self.assertEqual(self.plugins["arm"].commands, [])
        self.assertFalse(service.status()["gate_open"])

    def test_python310_shutdown_aggregate_preserves_all_errors_and_cleanup(self):
        from astrbot_ex.core.decision.service import ShutdownErrors
        service = self.create(backend=MockBackend(kind="wait"))
        backend_error, stop_error = ValueError("backend close failed"), OSError("stop proof unavailable")
        original = service.backend.close
        def bad_close():
            original()
            raise backend_error
        with patch.object(service.backend, "close", side_effect=bad_close), \
                patch.object(self.actions, "stop_actions", side_effect=stop_error), \
                patch("builtins.ExceptionGroup", create=True, side_effect=AssertionError("3.11-only builtin used")):
            with self.assertRaises(ShutdownErrors) as error:
                service.close()
        self.assertIs(error.exception.exceptions[0], backend_error)
        self.assertIs(error.exception.exceptions[1], stop_error)
        self.assertEqual(len(error.exception.exceptions), 2)
        self.assertFalse(service._backend_worker.is_alive())
        self.assertFalse(service._control.is_alive())
        self.assertTrue(service.observations.status()["closed"])
        self.assertEqual(service.goals.reason, "close_stop_not_proven")

    def test_review_request_observation_guard_keeps_original_fields_and_age(self):
        class Once(MockBackend):
            def decide(self, snapshot):
                result = super().decide(snapshot)
                self.kind = "wait"
                return result
        now = [time.monotonic_ns()]
        backend = Once(kind="start", block=True)
        service = self.create(backend=backend, observe=True, clock_ns=lambda: now[0])
        service.observations.ingest("arm_pose", {"position": "original"}, source_epoch="a", seq=1,
                                    source_timestamp=time.time())
        self.submit()
        self.assertTrue(backend.entered.wait(1))
        now[0] += 20_000_000
        service.observations.ingest("arm_pose", {"position": "new"}, source_epoch="a", seq=2,
                                    source_timestamp=time.time())
        captured = []
        original = self.dispatcher.update_context
        def record(**kwargs):
            captured.append(kwargs)
            return original(**kwargs)
        with patch.object(self.dispatcher, "update_context", side_effect=record):
            backend.release.set()
            self.assertTrue(self.plugins["arm"].started.wait(1))
        self.assertEqual(captured[0]["observations"]["arm_pose"]["fields"], {"position": "original"})
        self.assertGreaterEqual(captured[0]["observations"]["arm_pose"]["age_ms"], 20)
        self.assertEqual(len(self.plugins["arm"].commands), 1)

    def test_review_cancelled_review_consumed_without_worker_fault(self):
        service = self.create(backend=MockBackend(kind="wait"))
        entered, release = threading.Event(), threading.Event()
        original = service._poll_rows
        def parked():
            entered.set()
            release.wait(2)
            original()
        try:
            with patch.object(service, "_poll_rows", side_effect=parked), \
                    patch.object(self.dispatcher, "review_stops", wraps=self.dispatcher.review_stops) as review_stops:
                self.assertTrue(entered.wait(1))
                future = service.review()
                self.assertTrue(future.cancel())
                release.set()
                self.assertTrue(wait_for(lambda: service._review_future is None))
                self.assertEqual(review_stops.call_count, 0)
            self.assertTrue(service.review().result(2)["ok"])
            self.assertFalse(any(d["outcome"] == "blocked" for d in service.status()["decisions"]))
            self.assertTrue(service._control.is_alive())
        finally:
            release.set()

    def test_review_old_observation_cannot_borrow_latest_frame_freshness(self):
        class Once(MockBackend):
            def decide(self, snapshot):
                result = super().decide(snapshot)
                self.kind = "wait"
                return result
        now = [time.monotonic_ns()]
        backend = Once(kind="start", block=True)
        service = self.create(backend=backend, observe=True, clock_ns=lambda: now[0])
        service.observations.ingest("arm_pose", {"position": 1}, source_epoch="a", seq=1,
                                    source_timestamp=time.time())
        self.submit()
        self.assertTrue(backend.entered.wait(1))
        request_id = service.snapshot()["snapshot_id"]
        now[0] += 150_000_000
        service.observations.ingest("arm_pose", {"position": 2}, source_epoch="a", seq=2,
                                    source_timestamp=time.time())
        self.assertEqual(service.observations.snapshot(["arm_pose"])[0]["health"]["status"], "ok")
        backend.release.set()
        self.assertTrue(wait_for(lambda: any(d["snapshot_id"] == request_id for d in service.status()["decisions"])))
        self.assertEqual(self.plugins["arm"].commands, [])
        self.assertTrue(any(d["snapshot_id"] == request_id and d["reason_code"] == "request_observation_expired"
                            for d in service.status()["decisions"]))

    def _canonical_target_service(self, backend):
        import copy
        import json
        from astrbot_ex.core.actions.models import parse_action_manifest
        from astrbot_ex.core.decision.catalog import CapabilityInput
        service = self.create(backend=backend, observe=True)
        entry = service.catalog.snapshot().entries[0]
        manifest = copy.deepcopy(entry["manifest"])
        action = manifest["actions"][0]
        action["danger"] = "high"
        action["schema"] = {"type": "object", "properties": {"target_ref": {"type": "object"},
            "frame_id": {"type": "string"}, "observation_seq": {"type": "integer"}},
            "required": ["target_ref"], "additionalProperties": False}
        manifest["observation_sources"]["arm_pose"]["max_age_ms"] = 2000
        service.catalog.refresh([CapabilityInput("arm", 1, parse_action_manifest(manifest, owner="arm"),
            {}, entry["guide"], True, "1")])
        self.dispatcher.remove_owner(OwnerBinding("arm", 1))
        self.dispatcher.register_owner(OwnerBinding("arm", 1), self.actors["arm"], manifest)
        self.actions.update_versions()
        self.assertTrue(wait_for(lambda: service._last_catalog_revision == service.catalog.snapshot().revision))
        payload = json.loads((Path(__file__).parent / "fixtures/decision/p-normal.json").read_text())["payload"]
        data = payload["observation"]["data"] | {"position": 1}
        service.observations.ingest("arm_pose", data, source_epoch="epoch-1", seq=42, source_timestamp=time.time())
        observation = service.observations.snapshot(["arm_pose"])[0]
        params = {"target_ref": payload["target_ref"] | {"observation_id": observation["observation_id"]},
                  "frame_id": data["frame_id"], "observation_seq": 42}
        return service, data, params

    def test_review_target_ref_rechecked_before_actor_queued_start(self):
        backend = MockBackend(kind="start", block=True)
        service, data, params = self._canonical_target_service(backend)
        entered, release = threading.Event(), threading.Event()
        def park():
            entered.set()
            release.wait(2)
        self.plugins["arm"].park = park
        self.actors["arm"].cast("park")
        self.assertTrue(entered.wait(1))
        try:
            self.submit(parameters={"arm.move.v1": params})
            self.assertTrue(backend.entered.wait(1))
            self.assertTrue(any(c["kind"] == "start" for c in service.snapshot()["owners"][0]["candidates"]))
            backend.release.set()
            self.assertTrue(wait_for(lambda: any(d["outcome"] == "admitted" for d in service.status()["decisions"])))
            data["objects"][0]["track_session"] = "reused-track"
            service.observations.ingest("arm_pose", data, source_epoch="epoch-1", seq=43, source_timestamp=time.time())
            self.assertFalse(service.status()["gate_open"])
            release.set()
            self.assertTrue(wait_for(lambda: service.goals.phase in {"idle", "blocked"}))
            self.assertEqual(self.plugins["arm"].commands, [])
        finally:
            release.set()

    def test_review_target_ref_apply_rejects_mismatch_without_notify(self):
        backend = MockBackend(kind="start", block=True)
        service, data, params = self._canonical_target_service(backend)
        self.submit(parameters={"arm.move.v1": params})
        self.assertTrue(backend.entered.wait(1))
        request_id = service.snapshot()["snapshot_id"]
        with patch.object(service.observations, "_on_update", return_value=None):
            service.observations.ingest("arm_pose", data, source_epoch="epoch-1", seq=43, source_timestamp=time.time())
        backend.release.set()
        self.assertTrue(wait_for(lambda: any(d["snapshot_id"] == request_id for d in service.status()["decisions"])))
        self.assertEqual(self.plugins["arm"].commands, [])
        self.assertTrue(any(d["snapshot_id"] == request_id and d["outcome"] == "discarded"
                            for d in service.status()["decisions"]))

    def test_review_cancel_before_worker_claim_has_no_review_side_effect(self):
        service = self.create(backend=MockBackend(kind="wait"))
        entered, release = threading.Event(), threading.Event()
        original = service._poll_rows
        def parked():
            entered.set()
            release.wait(2)
            original()
        try:
            with patch.object(service, "_poll_rows", side_effect=parked), \
                    patch.object(self.dispatcher, "review_stops", wraps=self.dispatcher.review_stops) as review_stops:
                self.assertTrue(entered.wait(1))
                canceled = service.review()
                self.assertTrue(canceled.cancel())
                release.set()
                barrier = service.review()
                self.assertTrue(barrier.result(2)["ok"])
                self.assertEqual(review_stops.call_count, 1)
                self.assertTrue(canceled.cancelled())
                self.assertNotEqual(service.goals.phase, "blocked")
                self.assertFalse(any(d["outcome"] == "blocked" for d in service.status()["decisions"]))
        finally:
            release.set()

    def test_review_cancel_during_execution_is_false_and_completion_truthful(self):
        service = self.create(backend=MockBackend(kind="wait"))
        entered, release = threading.Event(), threading.Event()
        original = self.actions.stop_actions
        def parked(reason, **kwargs):
            if reason == "explicit_review":
                entered.set()
                release.wait(2)
            return original(reason, **kwargs)
        try:
            with patch.object(self.actions, "stop_actions", side_effect=parked):
                future = service.review()
                self.assertTrue(entered.wait(1))
                self.assertFalse(future.cancel())
                release.set()
                self.assertTrue(future.result(2)["ok"])
                self.assertNotEqual(service.goals.phase, "blocked")
                self.assertTrue(service.review().result(2)["ok"])
        finally:
            release.set()

    def test_backend_failure_backoff_and_close_reclaims_blocked_workers(self):
        backend = MockBackend(fail=True)
        service = self.create(backend=backend)
        self.submit()
        self.assertTrue(wait_for(lambda: service._failures == 1))
        for _ in range(100): service.tick()
        self.assertEqual(backend.calls, 1)
        service.close()
        self.assertFalse(service._backend_worker.is_alive())
        self.assertFalse(service._control.is_alive())
