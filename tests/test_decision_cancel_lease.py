"""Real EX cancellation/lease/restart stop-proof tests, no Host/model/hardware."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_ex.core.api_server import build_server
from astrbot_ex.core.actions.ledger import ActionLedger, OwnerBinding, StopEvidence
from astrbot_ex.core.actions.models import ActionCommand
from astrbot_ex.core.contracts import Feedback
from tests.test_decision_service import wait_for
from tests.wiring_fixture import WiringFixture


class CancelLeaseTests(WiringFixture):
    def tearDown(self):
        workers = [self.c._worker, self.server.task_public_delivery._worker,
                   self.s._control, self.s._backend_worker]
        super().tearDown()
        for worker in workers:
            self.assertFalse(worker.is_alive(), "terminal worker leak: " + worker.name)
        self.assertTrue(all(not actor.alive for actor in self.actors))

    def active(self, *, stop_proof=True, lease_ms=10000):
        self.execute(stop_proof=stop_proof)
        self.bind()
        self.assertTrue(self.submit(lease_ms=lease_ms)["ok"])
        self.assertTrue(self.owners["arm"].started.wait(2), self.diagnostics())
        cid = self.owners["arm"].commands[-1].command_id
        self.assertTrue(wait_for(lambda: self.server.action_ledger.get(cid).result(1).status == "accepted"))
        self.bind()
        return cid

    def cancel(self, goal_id="goal-1", revision=1, request_id="cancel-once", **extra):
        return {"schema_version": 1, "request_id": request_id, "ex_session": self.s.goals.ex_session,
                "goal_id": goal_id, "goal_revision": revision, **extra}

    def terminal(self, goal_id="goal-1"):
        self.assertTrue(wait_for(lambda: any(fact["goal_id"] == goal_id
            for fact in self.c.journal.snapshot()["goal_summaries"])))
        return next(fact for fact in self.c.journal.snapshot()["goal_summaries"] if fact["goal_id"] == goal_id)

    def unlocked(self):
        for lock in (self.c._lock, self.s._lock, self.s.goals._lock, self.server.interaction_core._lock):
            self.assertFalse(lock._is_owned(), "terminal IO held safety lock")

    def test_active_cancel_one_terminal_with_original_structured_proof_and_replay(self):
        cid = self.active()
        raw = self.cancel()
        first = self.request("decision.goal.cancel", raw)
        self.assertTrue(first["accepted"])
        fact = Feedback.parse(self.terminal()).to_dict()
        self.assertEqual(fact["status"], "canceled")
        self.assertTrue(fact["details"]["stop_evidence"]["stopped"])
        evidence = fact["details"]["terminal_evidence"]
        self.assertTrue(evidence["verified"])
        self.assertEqual(evidence["goal_id"], "goal-1")
        self.assertGreaterEqual(evidence["stop_proof_epoch"], evidence["dispatcher_epoch"])
        command = next(item for item in evidence["commands"] if item["command_id"] == cid)
        self.assertEqual(command["stop_evidence"], {"stopped": True, "source": "arm", "reference": cid})
        self.assertIsNone(self.s.goals.active)
        self.assertEqual(self.s.goals.phase, "idle")
        self.assertFalse(self.server.action_dispatcher._gate)
        head = self.c.journal.snapshot()["event_seq"]
        epoch = self.s.goals.gate_epoch
        with patch.object(self.s, "cancel_goal", wraps=self.s.cancel_goal) as cancel:
            self.assertEqual(self.request("decision.goal.cancel", raw), first)
            cancel.assert_not_called()
        with self.s._condition:
            self.s._stop_pending = False
            self.assertEqual(self.s.cancel_goal(raw), first)
            self.assertFalse(self.s._stop_pending)
        self.s._poll_rows()
        self.c._drain_feedback()
        self.s.tick()
        self.assertEqual(self.c.journal.snapshot()["event_seq"], head)
        self.assertEqual(self.s.goals.gate_epoch, epoch)
        self.assertEqual(self.owners["arm"].cancels.count(cid), 1)
        self.assertEqual(len(self.c.journal.snapshot()["goal_summaries"]), 1)
        self.assertFalse(self.request("decision.goal.cancel", {**raw, "reason_code": "collision"})["ok"])
        self.assertFalse(self.request("decision.goal.cancel", raw, conn="foreign")["ok"])
        self.bind(2, turn_id="new")
        self.assertFalse(self.request("decision.goal.cancel", raw)["ok"])
        self.assertTrue(self.submit(2)["ok"])
        self.assertFalse(self.request("decision.goal.cancel", raw)["ok"])
        self.assertTrue(wait_for(lambda: self.s.goals.active is not None))
        self.assertEqual(self.s.goals.active.payload()["goal_id"], "goal-2")

    def test_pending_goal_cancel_before_start_preserves_target_and_zero_starts(self):
        self.execute()
        self.bind()
        with self.s._condition:
            self.assertTrue(self.submit()["ok"])
            self.assertIsNotNone(self.s.goals.pending_replace)
            self.assertTrue(self.request("decision.goal.cancel", self.cancel())["accepted"])
        fact = self.terminal()
        self.assertEqual(fact["status"], "canceled")
        self.assertEqual(fact["details"]["terminal_evidence"]["commands"], [])
        self.assertEqual(self.owners["arm"].commands, [])
        self.assertIsNone(self.s.goals.pending_replace)
        self.assertIsNone(self.s.goals.active)

    def test_lease_expiry_has_full_stop_proof_no_success_or_renew_resurrection(self):
        cid = self.active(lease_ms=400)
        # Advance only the GoalManager's trusted lease clock; let the framework
        # revoke before the independent real-time action watchdog deadline.
        with self.s._condition:
            expires = self.s.goals.active.expires_ns
        with patch.object(self.s.goals, "_clock", return_value=expires + 1):
            self.s.tick()
            fact = self.terminal()
        self.assertEqual(fact["status"], "timed_out")
        self.assertEqual(fact["reason_code"], "goal_lease_expired")
        self.assertTrue(fact["details"]["terminal_evidence"]["verified"])
        self.assertTrue(fact["details"]["stop_evidence"]["stopped"])
        self.assertIn(cid, self.owners["arm"].cancels)
        self.assertIsNone(self.s.goals.active)
        self.assertFalse(self.server.action_dispatcher._gate)
        self.assertEqual(self.c._verified_completions, {})
        self.assertFalse(self.request("decision.goal.renew", {"schema_version": 1, "request_id": "renew",
            "ex_session": self.s.goals.ex_session, "goal_id": "goal-1", "goal_revision": 1,
            "lease_ms": 10000})["ok"])
        self.assertEqual(len(self.owners["arm"].commands), 1)

    def test_missing_stop_and_late_proof_stay_blocked_without_terminal(self):
        cid = self.active(stop_proof=False)
        self.server.action_service.stop_timeout = 0.1
        self.assertTrue(self.request("decision.goal.cancel", self.cancel())["accepted"])
        self.assertTrue(wait_for(lambda: self.s.goals.phase == "blocked"))
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])
        self.server.action_dispatcher.reconcile_stop(cid, OwnerBinding("arm", 1),
            StopEvidence(cid, True, "arm", "late-proof")).result(1)
        self.s._poll_rows()
        self.s.tick()
        threading.Event().wait(0.1)
        self.assertEqual(self.s.goals.phase, "blocked")
        self.assertIsNotNone(self.s.goals.active)
        self.assertFalse(self.c.state()["execution"]["stop_proven"])
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])

    def test_terminal_draft_superseded_by_new_goal_cannot_clear_pending(self):
        self.active()
        prepared, release = threading.Event(), threading.Event()
        original = self.c.journal.prepare_completion
        def draft(*args, **kwargs):
            self.unlocked()
            token = original(*args, **kwargs)
            prepared.set()
            release.wait(2)
            return token
        try:
            with patch.object(self.c.journal, "prepare_completion", side_effect=draft):
                self.assertTrue(self.request("decision.goal.cancel", self.cancel())["accepted"])
                self.assertTrue(prepared.wait(1))
                self.bind(2, turn_id="replacement")
                self.assertTrue(self.submit(2)["ok"])
                self.assertEqual(self.s.goals.pending_replace.payload()["goal_id"], "goal-2")
                release.set()
                self.assertTrue(wait_for(lambda: self.s.goals.active is not None
                    and self.s.goals.active.payload()["goal_id"] == "goal-2"))
        finally:
            release.set()
        self.assertFalse(any(fact["goal_id"] == "goal-1" for fact in self.c.journal.snapshot()["goal_summaries"]))

    def test_terminal_draft_epoch_stop_supersede_preserves_authority(self):
        self.active()
        prepared, release = threading.Event(), threading.Event()
        original = self.c.journal.prepare_completion
        def draft(*args, **kwargs):
            self.unlocked()
            token = original(*args, **kwargs)
            prepared.set()
            release.wait(2)
            return token
        try:
            with patch.object(self.c.journal, "prepare_completion", side_effect=draft):
                self.request("decision.goal.cancel", self.cancel())
                self.assertTrue(prepared.wait(1))
                old_epoch = self.s.goals.gate_epoch
                self.s.request_stop("new stop")
                self.assertGreater(self.s.goals.gate_epoch, old_epoch)
                release.set()
                self.assertTrue(wait_for(lambda: self.s.goals.phase == "idle"))
        finally:
            release.set()
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])

    def test_terminal_draft_io_fault_latches_and_stops(self):
        self.active()
        def fail(*args, **kwargs):
            self.unlocked()
            raise OSError("terminal draft SQL fault")
        with patch.object(self.c.journal, "prepare_completion", side_effect=fail):
            self.request("decision.goal.cancel", self.cancel())
            self.assertTrue(wait_for(lambda: self.c._fault))
        self.assertEqual(self.c._bindings, {})
        self.assertFalse(self.server.action_dispatcher._gate)
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])

    def test_lease_missing_stop_proof_never_emits_timed_out_goal(self):
        cid = self.active(stop_proof=False)
        self.server.action_service.stop_timeout = 0.1
        with self.s._condition:
            expires = self.s.goals.active.expires_ns
        with patch.object(self.s.goals, "_clock", return_value=expires + 1):
            self.s.tick()
            self.assertTrue(wait_for(lambda: self.s.goals.phase == "blocked"))
        self.assertIsNotNone(self.s.goals.active)
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])
        self.assertFalse(self.c.state()["execution"]["stop_proven"])
        self.server.action_dispatcher.reconcile_stop(cid, OwnerBinding("arm", 1),
            StopEvidence(cid, True, "arm", "lease-test-cleanup")).result(1)
        self.s.tick()
        self.assertEqual(self.s.goals.phase, "blocked")
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])

    def test_original_task_owner_replay_rejected_after_new_task_binding(self):
        self.active()
        raw = self.cancel()
        self.request("decision.goal.cancel", raw)
        self.terminal()
        self.s._poll_rows()
        self.assertTrue(self.server.action_service.await_stop_proof("handoff real proof"))
        self.assertTrue(self.server.action_dispatcher.review_stops().result(1) is not None)
        self.bind(1, task_id="other-task", turn_id="other-turn")
        self.assertFalse(self.request("decision.goal.cancel", raw)["ok"])

    def test_terminal_publish_io_fault_latches_and_stops(self):
        self.active()
        def fail(token):
            self.unlocked()
            raise OSError("terminal publish SQL fault")
        with patch.object(self.c.journal, "publish_completion", side_effect=fail):
            self.request("decision.goal.cancel", self.cancel())
            self.assertTrue(wait_for(lambda: self.c._fault))
        self.assertEqual(self.c._bindings, {})
        self.assertFalse(self.server.action_dispatcher._gate)
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])


class RestartPhysicalStateTests(unittest.TestCase):
    def test_old_session_canceled_record_restart_can_be_physically_proven(self):
        self._restart("canceled")

    def test_old_session_unknown_without_proof_restart_is_not_safe(self):
        self._restart("unknown")

    def _restart(self, status):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "execution/actions.sqlite3"
            path.parent.mkdir(parents=True)
            ledger = ActionLedger(path)
            command = ActionCommand.parse({"schema_version": 1, "command_id": "old-command",
                "ex_session": "old-session", "goal_id": "old-goal", "goal_revision": 1,
                "decision_id": "old-decision", "owner": "arm", "plugin_generation": 1,
                "action_id": "arm.move.v1", "operation": "start", "params": {"meters": 1}, "lease_ms": 1000})
            binding = OwnerBinding("arm", 1)
            try:
                ledger.admit(command, ("shared",), binding, task_id="old-task").result(1)
                ledger.report(command.command_id, binding, "accepted").result(1)
                if status == "canceled":
                    ledger.report(command.command_id, binding, status,
                        stop_evidence=StopEvidence(command.command_id, True, "arm", "old-stop")).result(1)
                else:
                    ledger.report(command.command_id, binding, status).result(1)
            finally:
                ledger.close()
            with patch.dict(os.environ, {"ASTRBOTEX_DATA_DIR": root,
                    "ASTRBOTEX_STT_ENABLED": "", "ASTRBOTEX_TTS_ENABLED": ""}):
                server = build_server("127.0.0.1", 0, 20)
            service, controller = server.decision_service, server.decision_controller
            try:
                self.assertNotEqual(service.goals.ex_session, "old-session")
                self.assertTrue(wait_for(lambda: any(row.command_id == "old-command" for row in service._rows)))
                if status == "canceled":
                    self.assertTrue(server.action_service.await_stop_proof("restart actual proof"))
                    state = controller.state()["execution"]
                    self.assertEqual(state["unresolved"], [])
                    self.assertTrue(state["stop_proven"])
                else:
                    state = controller.state()["execution"]
                    self.assertFalse(state["stop_proven"])
                    self.assertTrue(state["blocked"])
                    self.assertEqual(state["unresolved"][0]["command_id"], "old-command")
                    server.action_dispatcher.reconcile_recovered_stop(command.command_id, binding,
                        StopEvidence(command.command_id, True, "arm", "cleanup-proof")).result(1)
            finally:
                server.server_close()
            self.assertFalse(controller._worker.is_alive())
            self.assertFalse(service._control.is_alive())
            self.assertFalse(service._backend_worker.is_alive())
