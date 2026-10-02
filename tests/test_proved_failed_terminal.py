"""Proof-qualified FAILED tests on real Ledger/Dispatcher/EX wiring only."""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from astrbot_ex.core.actions.ledger import ActionLedger, LedgerFault, OwnerBinding, StopEvidence
from astrbot_ex.core.actions.models import ContractError
from tests.test_action_ledger import command as ledger_command, BINDING
from tests import test_action_dispatcher as dispatcher_tests
from tests.test_action_dispatcher import command as dispatcher_command
from tests.test_decision_service import wait_for
from tests.wiring_fixture import WiringFixture


class FailedLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.sqlite3"
        self.ledger = ActionLedger(self.path)

    def tearDown(self):
        if self.ledger is not None:
            self.ledger.close()
        self.tmp.cleanup()

    def admitted(self):
        self.ledger.admit(ledger_command("failed"), ("joint",), BINDING, task_id="task").result(1)
        return self.ledger.report("failed", BINDING, "accepted").result(1)

    def test_failed_positive_proof_event_resource_atomic_and_restart(self):
        self.admitted()
        proof = StopEvidence("failed", True, "arm", "atomic-stop")
        result = self.ledger.report("failed", BINDING, "failed", stop_evidence=proof).result(1)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.held_resources, ())
        self.assertEqual(self.ledger.stop_proof("failed", BINDING).result(1), proof)
        events = self.ledger.events().result(1)
        self.assertEqual(events[-1].status, "failed")
        self.assertEqual(events[-1].details["stop_evidence"],
            {"stopped": True, "source": "arm", "reference": "atomic-stop"})
        self.assertEqual(self.ledger.report("failed", BINDING, "failed", stop_evidence=proof).result(1), result)
        self.assertEqual(self.ledger.events().result(1), events)
        self.ledger.close()
        self.ledger = ActionLedger(self.path)
        self.assertEqual(self.ledger.get("failed").result(1), result)
        self.assertEqual(self.ledger.events().result(1), events)
        self.assertEqual(self.ledger.stop_proof("failed", BINDING).result(1), proof)

    def test_failed_sql_trigger_rolls_back_proof_event_and_resource_release(self):
        before = self.admitted()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER reject_failed BEFORE UPDATE ON commands WHEN NEW.status='failed' "
                       "BEGIN SELECT RAISE(ABORT, 'failed injected'); END")
        with self.assertRaises(LedgerFault):
            self.ledger.report("failed", BINDING, "failed",
                stop_evidence=StopEvidence("failed", True, "arm", "atomic-stop")).result(1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status,event_seq FROM commands WHERE command_id='failed'").fetchone(),
                             (before.status, before.event_seq))
            self.assertEqual(db.execute("SELECT count(*) FROM stop_evidence").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM resources").fetchone()[0], 1)
        with self.assertRaises(LedgerFault):
            self.ledger.close()
        self.ledger = None

    def test_invalid_proof_foreign_owner_and_late_report_do_not_release(self):
        before = self.admitted()
        for proof in (StopEvidence("other", True, "arm", "ref"),
                      StopEvidence("failed", False, "arm", "ref"),
                      StopEvidence("failed", True, "arm", "")):
            with self.subTest(proof=proof), self.assertRaises(ValueError):
                self.ledger.report("failed", BINDING, "failed", stop_evidence=proof)
        proof = StopEvidence("failed", True, "arm", "ref")
        for binding in (OwnerBinding("foreign", 3), OwnerBinding("arm", 4)):
            with self.subTest(binding=binding), self.assertRaises(ContractError):
                self.ledger.report("failed", binding, "failed", stop_evidence=proof).result(1)
        self.assertEqual(self.ledger.get("failed").result(1), before)
        failed = self.ledger.report("failed", BINDING, "failed").result(1)
        self.assertEqual(failed.held_resources, ("joint",))
        self.assertEqual(self.ledger.report("failed", BINDING, "failed", stop_evidence=proof).result(1), failed)
        self.assertIsNone(self.ledger.stop_proof("failed", BINDING).result(1))
        reconciled = self.ledger.reconcile_stop("failed", BINDING, proof).result(1)
        self.assertEqual(reconciled.status, "failed")
        self.assertEqual(reconciled.held_resources, ())


class FailedDispatcherTests(unittest.TestCase):
    # Reuse actual fixture helpers, not the base test methods/suite a second time.
    setUp = dispatcher_tests.DispatcherTests.setUp
    close = dispatcher_tests.DispatcherTests.close
    owner = dispatcher_tests.DispatcherTests.owner
    context = dispatcher_tests.DispatcherTests.context
    enabled = dispatcher_tests.DispatcherTests.enabled
    wait_status = dispatcher_tests.DispatcherTests.wait_status

    def test_proved_failed_revokes_queued_start_without_uncertain_block(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        self.dispatcher.start(dispatcher_command("arm", "failed")).result(1)
        self.wait_status("failed", "accepted")
        # Offline controller really parks its simulated device before its proof.
        with plugin._physical_lock:
            plugin._physical_running = False
        entered, release = threading.Event(), threading.Event()
        def barrier():
            entered.set()
            release.wait(2)
        busy = self.dispatcher._enqueue(barrier, priority=True)
        self.assertTrue(entered.wait(1))
        try:
            report = self.dispatcher.report("failed", OwnerBinding("arm", 3), "failed",
                stop_evidence=StopEvidence("failed", True, "arm", "parked"))
            queued = self.dispatcher.start(dispatcher_command("arm", "queued"))
            release.set()
            busy.result(1)
            result = report.result(1)
            try:
                queued.result(1)
            except (ContractError, RuntimeError):
                pass  # Either admission or Actor guard can reject revoked work.
            self.dispatcher._enqueue(lambda: None).result(1)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.held_resources, ())
            self.assertFalse(self.dispatcher._gate)
            self.assertFalse(self.dispatcher.blocked)
            self.assertIsNone(self.dispatcher._context)
            self.assertEqual([cmd.command_id for cmd in plugin.commands], ["failed"])
        finally:
            release.set()

    def test_caller_evidence_ignored_by_ledger_cannot_avoid_block(self):
        plugin = self.owner()
        self.enabled("arm")
        self.dispatcher.start(dispatcher_command("arm", "failed")).result(1)
        self.wait_status("failed", "accepted")
        original = self.ledger.report
        def without_proof(*args, **kwargs):
            self.assertFalse(self.dispatcher._lock._is_owned())
            kwargs.pop("stop_evidence", None)
            return original(*args, **kwargs)
        with patch.object(self.ledger, "report", side_effect=without_proof):
            result = self.dispatcher.report("failed", OwnerBinding("arm", 3), "failed",
                stop_evidence=StopEvidence("failed", True, "arm", "caller-only")).result(1)
        self.assertEqual(result.held_resources, ())  # no-resource action still needs proof
        self.assertTrue(self.dispatcher.blocked)
        self.assertFalse(self.dispatcher._gate)
        self.assertIsNone(self.ledger.stop_proof("failed", OwnerBinding("arm", 3)).result(1))

    def test_already_blocked_proved_failed_does_not_clear_review(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        self.dispatcher.start(dispatcher_command("arm", "failed")).result(1)
        self.wait_status("failed", "accepted")
        self.dispatcher._block("explicit_existing_review")
        with plugin._physical_lock:
            plugin._physical_running = False
        result = self.dispatcher.report("failed", OwnerBinding("arm", 3), "failed",
            stop_evidence=StopEvidence("failed", True, "arm", "parked")).result(1)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.held_resources, ())
        self.assertTrue(self.dispatcher.blocked)
        self.assertFalse(self.dispatcher._gate)
        self.assertIn("explicit_existing_review", self.dispatcher.faults)

    def test_controlled_revoke_denies_queued_start_generic_gate_still_blocks(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        self.dispatcher.start(dispatcher_command("arm", "running")).result(1)
        self.wait_status("running", "accepted")
        entered, release = threading.Event(), threading.Event()
        def hold():
            entered.set()
            release.wait(2)
        busy = self.dispatcher._enqueue(hold, priority=True)
        self.assertTrue(entered.wait(1))
        try:
            queued = self.dispatcher.start(dispatcher_command("arm", "queued"))
            epoch = self.dispatcher._epoch
            self.dispatcher.revoke_decision_gate()
            self.assertGreater(self.dispatcher._epoch, epoch)
            self.assertFalse(self.dispatcher._gate)
            self.assertIsNone(self.dispatcher._context)
            self.assertFalse(self.dispatcher.blocked)
            release.set()
            busy.result(1)
            with self.assertRaises((ContractError, RuntimeError)):
                queued.result(1)
            self.assertEqual([cmd.command_id for cmd in plugin.commands], ["running"])
            self.dispatcher.set_gate(False)
            self.assertTrue(self.dispatcher.blocked)
            self.assertIn("gate_revoked", self.dispatcher.faults)
            self.dispatcher.revoke_decision_gate()
            self.assertTrue(self.dispatcher.blocked)
        finally:
            release.set()

    def test_plain_failed_late_proof_replay_does_not_unblock(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        self.dispatcher.start(dispatcher_command("arm", "failed")).result(1)
        self.wait_status("failed", "accepted")
        failed = self.dispatcher.report("failed", OwnerBinding("arm", 3), "failed").result(1)
        self.assertTrue(self.dispatcher.blocked)
        self.assertFalse(self.dispatcher._gate)
        self.assertEqual(failed.held_resources, ("joint",))
        with plugin._physical_lock:
            plugin._physical_running = False
        proof = StopEvidence("failed", True, "arm", "late-parked")
        self.dispatcher.report("failed", OwnerBinding("arm", 3), "failed", stop_evidence=proof).result(1)
        self.assertIsNone(self.ledger.stop_proof("failed", OwnerBinding("arm", 3)).result(1))
        self.dispatcher.reconcile_stop("failed", OwnerBinding("arm", 3), proof).result(1)
        self.assertTrue(self.dispatcher.blocked)
        self.assertFalse(self.dispatcher._gate)


class FailedGoalTests(WiringFixture):
    def active(self, *, owners=("arm",), stop_proof=True):
        self.execute(owners, stop_proof=stop_proof)
        self.bind()
        self.assertTrue(self.submit(owners=owners)["ok"])
        self.assertTrue(self.owners["arm"].started.wait(2), self.diagnostics())
        cid = self.owners["arm"].commands[-1].command_id
        self.assertTrue(wait_for(lambda: self.server.action_ledger.get(cid).result(1).status == "accepted"))
        self.bind()
        return cid

    def fail(self, cid, *, proof=True):
        return self.server.action_dispatcher.report(cid, OwnerBinding("arm", 1), "failed",
            stop_evidence=StopEvidence(cid, True, "arm", "atomic-parked") if proof else None).result(1)

    def tearDown(self):
        workers = [self.c._worker, self.server.task_public_delivery._worker, self.s._control, self.s._backend_worker]
        super().tearDown()
        self.assertEqual([worker.name for worker in workers if worker.is_alive()], [])
        self.assertTrue(all(not actor.alive for actor in self.actors))

    def unlocked(self):
        for lock in (self.c._lock, self.s._lock, self.s.goals._lock, self.server.interaction_core._lock):
            self.assertFalse(lock._is_owned(), "failed terminal IO held safety lock")

    def test_real_failed_one_goal_summary_idempotent_and_never_completed_public(self):
        cid = self.active()
        self.fail(cid)
        self.assertTrue(wait_for(lambda: self.c.journal.snapshot()["feedback"] is not None))
        fact = self.c.journal.snapshot()["feedback"]
        self.assertEqual(fact["status"], "failed")
        self.assertEqual(fact["goal_id"], "goal-1")
        self.assertEqual(fact["goal_revision"], 1)
        evidence = fact["details"]["failure_evidence"]
        self.assertTrue(evidence["verified"])
        self.assertEqual(evidence["goal_id"], fact["goal_id"])
        self.assertEqual(evidence["commands"][0]["command_id"], cid)
        self.assertEqual(evidence["commands"][0]["stop_evidence"],
            {"stopped": True, "source": "arm", "reference": "atomic-parked"})
        self.assertIsNone(self.s.goals.active)
        self.assertIsNone(self.s.goals.pending_replace)
        self.assertEqual(self.s.goals.phase, "idle")
        self.assertFalse(self.server.action_dispatcher._gate)
        self.assertFalse(self.server.action_dispatcher.blocked)
        self.assertEqual(self.c._verified_completions, {})
        head = self.c.journal.snapshot()["event_seq"]
        self.s._poll_rows()
        self.s.tick()
        self.c._drain_feedback()
        self.assertEqual(self.c.journal.snapshot()["event_seq"], head)
        self.assertEqual(len(self.c.journal.snapshot()["goal_summaries"]), 1)
        turn = self.bind()
        self.assertFalse(self.request("interaction.reply", self.public(turn, claim="completed",
            evidence_seq=fact["event_seq"]))["ok"])
        self.assertEqual(len(self.owners["arm"].commands), 1)

    def test_failed_history_then_explicit_new_goal_cancel_is_target_scoped(self):
        # A few real old-session success events must not inflate either summary.
        for index in range(3):
            command = ledger_command("history-" + str(index))
            self.server.action_ledger.admit(command, (), BINDING, task_id="old-task").result(1)
            self.server.action_ledger.report(command.command_id, BINDING, "accepted").result(1)
            self.server.action_ledger.report(command.command_id, BINDING, "succeeded").result(1)
        first = self.active()
        self.fail(first)
        self.assertTrue(wait_for(lambda: self.c.journal.snapshot()["feedback"] is not None))
        failed = self.c.journal.snapshot()["feedback"]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual([item["command_id"] for item in failed["details"]["terminal_evidence"]["commands"]], [first])
        self.assertFalse(self.server.action_dispatcher.blocked)
        self.bind(2, turn_id="explicit-next")
        self.assertTrue(self.submit(2)["ok"])
        self.assertTrue(wait_for(lambda: len(self.owners["arm"].commands) == 2))
        second = self.owners["arm"].commands[-1].command_id
        self.assertTrue(wait_for(lambda: self.server.action_ledger.get(second).result(1).status == "accepted"))
        self.bind(2, turn_id="explicit-next")
        self.assertTrue(self.request("decision.goal.cancel", {"schema_version": 1,
            "request_id": "cancel-second", "ex_session": self.s.goals.ex_session,
            "goal_id": "goal-2", "goal_revision": 2})["accepted"])
        self.assertTrue(wait_for(lambda: len(self.c.journal.snapshot()["goal_summaries"]) == 2))
        facts = {fact["goal_id"]: fact for fact in self.c.journal.snapshot()["goal_summaries"]}
        self.assertEqual(facts["goal-1"], failed)
        canceled = facts["goal-2"]
        self.assertEqual(canceled["status"], "canceled")
        evidence = canceled["details"]["terminal_evidence"]
        self.assertEqual([item["command_id"] for item in evidence["commands"]], [second])
        self.assertEqual(evidence["commands"][0]["ex_session"], self.s.goals.ex_session)
        self.assertEqual(evidence["commands"][0]["goal_id"], "goal-2")
        self.assertEqual(evidence["commands"][0]["goal_revision"], 2)
        self.assertGreaterEqual(evidence["stop_proof_epoch"], evidence["dispatcher_epoch"])
        self.assertEqual(self.server.action_ledger.get(first).result(1).status, "failed")
        self.assertEqual(self.server.action_ledger.get(second).result(1).status, "canceled")
        self.assertEqual([command.command_id for command in self.owners["arm"].commands], [first, second])
        self.assertEqual(self.owners["arm"].cancels, [first, second])
        self.assertIsNone(self.s.goals.active)
        self.assertIsNone(self.s.goals.pending_replace)
        self.assertFalse(self.server.action_dispatcher._gate)
        self.assertFalse(self.server.action_dispatcher.blocked)
        self.assertTrue(self.c.state()["execution"]["stop_proven"])
        self.assertEqual(self.c._verified_completions, {})

    def test_controlled_cancel_existing_fault_never_auto_reviews_or_publishes(self):
        cid = self.active()
        self.server.action_dispatcher._block("existing_requires_review")
        with patch.object(self.server.action_dispatcher, "review_stops",
                          wraps=self.server.action_dispatcher.review_stops) as review:
            self.assertTrue(self.request("decision.goal.cancel", {"schema_version": 1,
                "request_id": "blocked-cancel", "ex_session": self.s.goals.ex_session,
                "goal_id": "goal-1", "goal_revision": 1})["accepted"])
            self.assertTrue(wait_for(lambda: self.s.goals.phase == "blocked"))
            self.assertTrue(wait_for(lambda: cid in self.owners["arm"].cancels))
            review.assert_not_called()
        self.assertTrue(self.server.action_dispatcher.blocked)
        self.assertIsNotNone(self.s.goals.active)
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])
        self.assertFalse(self.c.state()["execution"]["stop_proven"])

    def test_terminal_cas_refuses_block_introduced_after_durable_draft(self):
        cid = self.active()
        entered, release = threading.Event(), threading.Event()
        original = self.c.journal.prepare_completion
        def prepare(*args, **kwargs):
            self.unlocked()
            token = original(*args, **kwargs)
            entered.set()
            release.wait(2)
            return token
        try:
            with patch.object(self.c.journal, "prepare_completion", side_effect=prepare):
                self.fail(cid)
                self.assertTrue(entered.wait(1))
                self.server.action_dispatcher._block("review_between_draft_and_CAS")
                release.set()
                self.server.action_dispatcher._enqueue(lambda: None).result(1)
        finally:
            release.set()
        self.assertTrue(wait_for(lambda: not self.s._stop_pending))
        self.assertIsNotNone(self.s.goals.active)
        self.assertTrue(self.server.action_dispatcher.blocked)
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])

    def test_failed_peer_without_proof_prevents_goal_release(self):
        cid = self.active(owners=("arm", "camera"), stop_proof=False)
        self.assertTrue(self.owners["camera"].started.wait(1))
        other = self.owners["camera"].commands[-1].command_id
        self.assertTrue(wait_for(lambda: self.server.action_ledger.get(other).result(1).status == "accepted"))
        self.server.action_service.stop_timeout = 0.1
        self.fail(cid)
        self.assertTrue(wait_for(lambda: self.s.goals.phase == "blocked"))
        self.assertIsNotNone(self.s.goals.active)
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])
        self.assertFalse(self.c.state()["execution"]["stop_proven"])
        self.assertTrue(wait_for(lambda: self.server.action_ledger.get(other).result(1).status in
                                 {"unknown", "timed_out"}))
        self.server.action_dispatcher.reconcile_stop(other, OwnerBinding("camera", 1),
            StopEvidence(other, True, "camera", "late-peer-cleanup")).result(1)
        self.assertEqual(self.s.goals.phase, "blocked")

    def test_failed_no_proof_and_late_reconcile_remain_blocked_without_summary(self):
        cid = self.active(stop_proof=False)
        self.server.action_service.stop_timeout = 0.1
        self.fail(cid, proof=False)
        self.assertTrue(wait_for(lambda: self.s.goals.phase == "blocked"))
        self.assertIsNotNone(self.s.goals.active)
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])
        self.server.action_dispatcher.reconcile_stop(cid, OwnerBinding("arm", 1),
            StopEvidence(cid, True, "arm", "late-proof")).result(1)
        self.s._poll_rows()
        self.s.tick()
        self.assertEqual(self.s.goals.phase, "blocked")
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])

    def test_failed_draft_superseded_new_goal_not_cleared(self):
        cid = self.active()
        entered, release = threading.Event(), threading.Event()
        original = self.c.journal.prepare_completion
        def prepare(*args, **kwargs):
            self.unlocked()
            token = original(*args, **kwargs)
            entered.set()
            release.wait(2)
            return token
        try:
            with patch.object(self.c.journal, "prepare_completion", side_effect=prepare):
                self.fail(cid)
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.submit(2)["ok"])
                self.assertEqual(self.s.goals.pending_replace.payload()["goal_id"], "goal-2")
                release.set()
                self.assertTrue(wait_for(lambda: self.s.goals.active is not None
                    and self.s.goals.active.payload()["goal_id"] == "goal-2"))
        finally:
            release.set()
        self.assertFalse(any(fact["goal_id"] == "goal-1" for fact in self.c.journal.snapshot()["goal_summaries"]))

    def test_failed_draft_stop_epoch_supersede_no_stale_terminal(self):
        cid = self.active()
        entered, release = threading.Event(), threading.Event()
        original = self.c.journal.prepare_completion
        def prepare(*args, **kwargs):
            self.unlocked()
            token = original(*args, **kwargs)
            entered.set()
            release.wait(2)
            return token
        try:
            with patch.object(self.c.journal, "prepare_completion", side_effect=prepare):
                self.fail(cid)
                self.assertTrue(entered.wait(1))
                self.s.request_stop("superseding explicit stop")
                release.set()
                self.assertTrue(wait_for(lambda: self.s.goals.phase == "idle"))
        finally:
            release.set()
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])

    def _journal_failure(self, method):
        cid = self.active()
        journal = self.c.journal
        original = getattr(journal, method)
        def failure(*args, **kwargs):
            self.unlocked()
            # Actual journal transaction fails before fact becomes public.
            with journal._lock, journal._db:
                journal._db.execute("CREATE TRIGGER reject_terminal BEFORE INSERT ON drafts "
                    "BEGIN SELECT RAISE(ABORT, 'failed SQL injected'); END" if method == "prepare_completion" else
                    "CREATE TRIGGER reject_terminal BEFORE UPDATE OF summary ON goals "
                    "BEGIN SELECT RAISE(ABORT, 'failed SQL injected'); END")
            return original(*args, **kwargs)
        with patch.object(journal, method, side_effect=failure):
            self.fail(cid)
            self.assertTrue(wait_for(lambda: self.c._fault))
        self.assertEqual(self.c._bindings, {})
        self.assertFalse(self.server.action_dispatcher._gate)
        self.assertEqual(journal.snapshot()["goal_summaries"], [])
        self.assertFalse(any(fact["status"] == "failed" for fact in journal.pending()))
        with journal._lock, journal._db:
            journal._db.execute("DROP TRIGGER reject_terminal")

    def test_failed_draft_sql_fault_latches_stop(self):
        self._journal_failure("prepare_completion")

    def test_failed_publish_sql_fault_rolls_back_fact_latches_stop(self):
        self._journal_failure("publish_completion")
