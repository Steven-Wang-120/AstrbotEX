"""Real EX journal/controller failure boundaries; no sibling/Host/provider IO."""
from __future__ import annotations

import copy
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from astrbot_ex.core.actions.ledger import OwnerBinding
from astrbot_ex.core.actions.models import ActionCommand
from astrbot_ex.core.contracts import DecisionState
from astrbot_ex.core.decision.backends import MockBackend
from astrbot_ex.core.plugin_actor import PluginActor
from astrbot_ex.core.decision.feedback_journal import FeedbackJournal
from astrbot_ex.core.decision.goal_manager import GoalManager
from tests.test_decision_service import ActionOwner, wait_for
from tests.wiring_fixture import WiringFixture
from tests.test_goal_manager import goal_payload, make_catalog


class JournalAdmissionTests(unittest.TestCase):
    def test_trigger_failure_rolls_back_goal_and_admission_then_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "journal.sqlite3"
            manager = GoalManager(make_catalog())
            payload = goal_payload(manager)
            journal = FeedbackJournal(path, manager.ex_session)
            try:
                with journal._lock, journal._db:
                    journal._db.execute("CREATE TRIGGER reject_admission BEFORE INSERT ON admissions "
                                        "BEGIN SELECT RAISE(ABORT, 'admission injected'); END")
                with self.assertRaisesRegex(sqlite3.IntegrityError, "admission injected"):
                    journal.prepare_admission(payload, 1)
                self.assertFalse(journal.registered(payload["goal_id"], 1))
                with closing(sqlite3.connect(path)) as observer:
                    self.assertEqual(observer.execute("SELECT count(*) FROM goals").fetchone()[0], 0)
                    self.assertEqual(observer.execute("SELECT count(*) FROM admissions").fetchone()[0], 0)
                with journal._lock, journal._db:
                    journal._db.execute("DROP TRIGGER reject_admission")
                journal.prepare_admission(payload, 1)
                journal.prepare_admission(copy.deepcopy(payload), 1)
                journal.register_goal(copy.deepcopy(payload), 1)
                with closing(sqlite3.connect(path)) as observer:
                    self.assertEqual(observer.execute("SELECT count(*) FROM goals").fetchone()[0], 1)
                    self.assertEqual(observer.execute("SELECT count(*) FROM admissions").fetchone()[0], 1)
                with self.assertRaisesRegex(RuntimeError, "journal_admission_conflict"):
                    journal.prepare_admission({**payload, "goal_text_en": "Other goal."}, 1)
                other = goal_payload(manager, 2)
                journal.register_goal(other, 2)
                with closing(sqlite3.connect(path)) as observer:
                    self.assertEqual(observer.execute("SELECT count(*) FROM goals").fetchone()[0], 2)
            finally:
                journal.close()


class RealJournalControllerTests(WiringFixture):
    def setUp(self):
        super().setUp()
        # Real construction/worker ran. Join it before deterministic single-round
        # driving; never fake Thread.start or construct Controller via __new__.
        self.c._stop.set()
        self.c._worker.join(2)
        self.assertFalse(self.c._worker.is_alive())
        self.c._stop.clear()

    def _sdk_fault_isolation(self, mode, *, sql_fault):
        actions, dispatcher = self.server.action_service, self.server.action_dispatcher
        self.assertEqual(actions.control_mode, "legacy")
        self.s.backend = MockBackend(kind="wait")
        if mode == "shadow":
            self.s.set_mode("shadow")
        self.assertTrue(wait_for(lambda: self.s.goals.phase == "idle"))
        self.bind()
        manifest = make_catalog(resource="shared").snapshot().entries[0]["manifest"]
        owner = ActionOwner("arm", dispatcher)
        actor = PluginActor(owner)
        actor.start()
        self.actors.append(actor)
        self.owners["arm"] = owner
        dispatcher.register_owner(OwnerBinding("arm", 1), actor, manifest)
        actions.update_versions(runtime_state="running")
        self.assertTrue(wait_for(lambda: self.s._last_framework_versions == (
            actions._runtime_state, actions._config_revision, actions._environment_revision, actions.control_mode)))
        dispatcher.set_gate(True)
        dispatcher.update_context(ex_session="sdk-session", goal_id="sdk-goal", goal_revision=1,
            task_id="sdk-task", allowed_actions=["arm.move.v1"],
            bound_params={"arm.move.v1": {"meters": 1}}, runtime_state="running",
            catalog_revision=actions._catalog_revision, config_revision=actions._config_revision,
            environment_revision=actions._environment_revision, ttl_ms=10000)
        command = ActionCommand.parse({"schema_version": 1, "command_id": "sdk-journal-" + mode,
            "ex_session": "sdk-session", "goal_id": "sdk-goal", "goal_revision": 1,
            "decision_id": "sdk-decision", "owner": "arm", "plugin_generation": 1,
            "action_id": "arm.move.v1", "operation": "start", "params": {"meters": 1}, "lease_ms": 1000})
        dispatcher.start(command).result(1)
        self.assertTrue(owner.started.wait(1))
        self.assertTrue(wait_for(lambda: self.server.action_ledger.get(command.command_id).result(1).status == "accepted"))
        dispatcher.report(command.command_id, OwnerBinding("arm", 1), "running").result(1)
        before = self.server.action_ledger.get(command.command_id).result(1)
        epoch, context = dispatcher._epoch, dispatcher._context
        trigger = "CREATE TRIGGER sdk_journal_fault BEFORE UPDATE ON sessions " \
                  "BEGIN SELECT RAISE(ABORT, 'SDK journal SQL fault'); END"
        if sql_fault:
            with self.c.journal._lock, self.c.journal._db:
                self.c.journal._db.execute(trigger)
        original_progress = self.s._progress
        progressed = threading.Event()
        def progress():
            original_progress()
            progressed.set()
        def fail_collect(*args, **kwargs):
            self.assert_io_unlocked()
            raise RuntimeError("journal SDK auxiliary fault")
        try:
            with patch.object(self.s, "request_stop", wraps=self.s.request_stop) as stops, \
                    patch.object(self.s, "_progress", side_effect=progress):
                if sql_fault:
                    self.c._drain_feedback()
                else:
                    with patch.object(self.c.journal, "collect", side_effect=fail_collect):
                        self.c._drain_feedback()
                self.assertTrue(self.c._fault)
                self.assertEqual(self.c._bindings, {})
                self.assertFalse(self.submit()["ok"])
                self.assertFalse(self.request("interaction.task.turn", self.turn()) ["ok"])
                self.s.tick()
                self.assertTrue(progressed.wait(1))
                after = self.server.action_ledger.get(command.command_id).result(1)
                print("SDK journal compatibility", mode, {"starts": len(owner.commands),
                    "cancels": list(owner.cancels), "epoch_before": epoch, "epoch_after": dispatcher._epoch,
                    "gate": dispatcher._gate, "held_resources": list(after.held_resources),
                    "stop_calls": stops.call_count})
                self.assertEqual(len(owner.commands), 1)
                self.assertEqual(owner.cancels, [])
                self.assertEqual(after, before)
                self.assertEqual(after.held_resources, ("shared",))
                self.assertTrue(dispatcher._gate)
                self.assertEqual(dispatcher._epoch, epoch)
                self.assertIs(dispatcher._context, context)
                stops.assert_not_called()
        finally:
            if sql_fault:
                with self.c.journal._lock, self.c.journal._db:
                    self.c.journal._db.execute("DROP TRIGGER sdk_journal_fault")
            # Stop the real SDK owner explicitly only after all isolation assertions.
            self.assertTrue(actions.stop_actions("SDK fixture cleanup"))

    def test_disabled_sdk_real_start_collect_fault_latches_without_revoking_sdk(self):
        self._sdk_fault_isolation("disabled", sql_fault=False)

    def test_shadow_sdk_real_start_sql_fault_latches_without_revoking_sdk(self):
        self._sdk_fault_isolation("shadow", sql_fault=True)

    def test_disabled_decision_owner_fault_still_requests_physical_stop(self):
        cid = self.active_goal()
        with self.s._condition:
            self.s.mode = "disabled"  # mode flag alone must not erase trusted decision ownership
        with patch.object(self.c.journal, "collect", side_effect=RuntimeError("disabled owner journal fault")), \
                patch.object(self.s, "request_stop", wraps=self.s.request_stop) as stops:
            self.c._drain_feedback()
            self.assertTrue(self.c._fault)
            self.assertEqual(self.c._bindings, {})
            self.assertFalse(self.server.action_dispatcher._gate)
            stops.assert_called_once_with("feedback_journal_unavailable")
        self.assertTrue(wait_for(lambda: cid in self.owners["arm"].cancels))

    def active_goal(self):
        self.execute()
        self.bind()
        self.assertTrue(self.submit()["ok"])
        self.assertTrue(self.owners["arm"].started.wait(2), self.diagnostics())
        cid = self.owners["arm"].commands[-1].command_id
        self.assertTrue(wait_for(lambda: self.server.action_ledger.get(cid).result(1).status == "accepted"))
        self.assertIsNotNone(self.s.goals.active)
        self.bind()
        self.c.journal.collect(self.server.action_ledger)
        self.assertGreaterEqual(len(self.c.journal.pending()), 2)
        return cid

    def assert_io_unlocked(self):
        for lock in (self.c._lock, self.s.goals._lock, self.server.interaction_core._lock, self.s._lock):
            self.assertFalse(lock._is_owned(), "journal/network IO held a safety lock")

    def exact_reply(self, connection, channel, method, fact, **kwargs):
        self.assert_io_unlocked()
        return {"ok": True, "ex_session": fact["ex_session"], "acked_event_seq": fact["event_seq"]}, None

    def injected_fault(self, method, error):
        cid = self.active_goal()
        def fail(*args, **kwargs):
            self.assert_io_unlocked()
            raise error
        with patch.object(self.c.journal, method, side_effect=fail), \
                patch.object(self.m, "request_connection", side_effect=self.exact_reply), \
                patch.object(self.s, "request_stop", wraps=self.s.request_stop) as stops:
            self.c._drain_feedback()
            self.assertTrue(self.c._fault)
            self.assertEqual(self.c._bindings, {})
            self.assertFalse(self.server.action_dispatcher._gate)
            stops.assert_called_once_with("feedback_journal_unavailable")
            self.c._drain_feedback()
            stops.assert_called_once()
        self.assertTrue(wait_for(lambda: cid in self.owners["arm"].cancels))

    def test_collect_runtime_capacity_fault_stops_actual_active_goal(self):
        self.injected_fault("collect", RuntimeError("journal_fact_capacity"))

    def test_pending_runtime_fault_stops_actual_active_goal(self):
        self.injected_fault("pending", RuntimeError("journal_pending_capacity"))

    def test_ack_runtime_fault_stops_actual_active_goal(self):
        self.injected_fault("ack", RuntimeError("journal_ack_failure"))

    def test_collect_future_timeout_stops_actual_active_goal(self):
        self.injected_fault("collect", FutureTimeout("ledger read timed out"))

    def test_collect_ledger_failure_stops_actual_active_goal(self):
        cid = self.active_goal()
        with patch.object(self.server.action_ledger, "events", side_effect=RuntimeError("ledger fault")), \
                patch.object(self.s, "request_stop", wraps=self.s.request_stop) as stops:
            self.c._drain_feedback()
            self.assertTrue(self.c._fault)
            stops.assert_called_once_with("feedback_journal_unavailable")
        self.assertTrue(wait_for(lambda: cid in self.owners["arm"].cancels))

    def test_collect_real_sql_failure_stops_actual_active_goal(self):
        cid = self.active_goal()
        with self.c.journal._lock, self.c.journal._db:
            self.c.journal._db.execute("CREATE TRIGGER reject_cursor BEFORE UPDATE ON sessions "
                                      "BEGIN SELECT RAISE(ABORT, 'cursor injected'); END")
        with patch.object(self.s, "request_stop", wraps=self.s.request_stop) as stops:
            self.c._drain_feedback()
            self.assertTrue(self.c._fault)
            self.assertFalse(self.server.action_dispatcher._gate)
            self.assertEqual(self.c._bindings, {})
            stops.assert_called_once_with("feedback_journal_unavailable")
        with self.c.journal._lock, self.c.journal._db:
            self.c.journal._db.execute("DROP TRIGGER reject_cursor")
        self.assertTrue(wait_for(lambda: cid in self.owners["arm"].cancels))

    def test_real_admission_capacity_fault_stops_actual_active_goal(self):
        cid = self.active_goal()
        self.c.journal.goal_limit = 1
        with patch.object(self.s, "request_stop", wraps=self.s.request_stop) as stops:
            self.assertFalse(self.submit(2)["ok"])
            self.assertTrue(self.c._fault)
            self.assertFalse(self.server.action_dispatcher._gate)
            self.assertEqual(self.c._bindings, {})
            stops.assert_called_once_with("feedback_journal_unavailable")
        self.assertFalse(self.c.journal.registered("goal-2", 2))
        self.assertTrue(wait_for(lambda: cid in self.owners["arm"].cancels))

    def test_real_admission_sql_failure_is_atomic_and_stops_actual_active_goal(self):
        cid = self.active_goal()
        journal = self.c.journal
        with journal._lock, journal._db:
            journal._db.execute("CREATE TRIGGER reject_admission BEFORE INSERT ON admissions "
                                "BEGIN SELECT RAISE(ABORT, 'admission injected'); END")
        with patch.object(self.s, "request_stop", wraps=self.s.request_stop) as stops:
            self.assertFalse(self.submit(2)["ok"])
            self.assertTrue(self.c._fault)
            self.assertEqual(self.c._bindings, {})
            self.assertFalse(self.server.action_dispatcher._gate)
            stops.assert_called_once_with("feedback_journal_unavailable")
        with journal._lock, journal._db:
            self.assertEqual(journal._db.execute("SELECT count(*) FROM admissions").fetchone()[0], 1)
            self.assertEqual(journal._db.execute("SELECT count(*) FROM goals").fetchone()[0], 1)
            journal._db.execute("DROP TRIGGER reject_admission")
        self.assertTrue(wait_for(lambda: cid in self.owners["arm"].cancels))

    def test_network_route_and_request_failures_retain_fact_without_stopping(self):
        self.active_goal()
        journal = self.c.journal
        before = journal.pending()
        original_collect, original_pending, original_ack = journal.collect, journal.pending, journal.ack
        def unlocked(operation):
            def run(*args, **kwargs):
                self.assert_io_unlocked()
                return operation(*args, **kwargs)
            return run
        with patch.object(journal, "collect", side_effect=unlocked(original_collect)), \
                patch.object(journal, "pending", side_effect=unlocked(original_pending)), \
                patch.object(journal, "ack", side_effect=unlocked(original_ack)), \
                patch.object(self.s, "request_stop", wraps=self.s.request_stop) as stops:
            for error in (RuntimeError("offline route"), TimeoutError("network timeout")):
                with self.subTest(error=type(error).__name__), \
                        patch.object(self.m, "decision_connection_id", side_effect=error):
                    self.c._drain_feedback()
                    self.c._drain_feedback()
                with patch.object(self.m, "request_connection", side_effect=error):
                    self.c._drain_feedback()
                    self.c._drain_feedback()
            self.assertFalse(self.c._fault)
            self.assertTrue(self.c._bindings)
            self.assertEqual(journal.pending(), before)
            stops.assert_not_called()
            first = before[0]["event_seq"]
            def reply(connection, channel, method, fact, **kwargs):
                self.assert_io_unlocked()
                # Non-exact sequence (bool included) must not ACK another fact.
                seq = first if fact["event_seq"] == first else True
                return {"ok": True, "ex_session": journal.session, "acked_event_seq": seq}, None
            with patch.object(self.m, "request_connection", side_effect=reply):
                self.c._drain_feedback()
            self.assertEqual(journal.pending(), before[1:])
            with patch.object(self.m, "request_connection", side_effect=self.exact_reply):
                self.c._drain_feedback()
            self.assertEqual(journal.pending(), [])
            stops.assert_not_called()

    def test_natural_retention_tombstones_and_page_horizon(self):
        cid = self.active_goal()
        journal = self.c.journal
        journal.retention = 2
        self.server.action_dispatcher.report(cid, OwnerBinding("arm", 1), "running").result(1)
        with patch.object(self.m, "request_connection", side_effect=RuntimeError("offline")):
            self.c._drain_feedback()
        facts = journal.pending()
        self.assertEqual(len(facts), 2)
        head = journal.snapshot()["event_seq"]
        self.assertGreaterEqual(head, 3)
        self.assertEqual([fact["event_seq"] for fact in facts], [head - 1, head])
        self.assertTrue(journal.events(0)["resync_required"])
        self.assertTrue(journal.ack(facts[-1]))
        self.assertFalse(journal.ack({**facts[0], "status": "failed"}))
        self.assertEqual(journal.pending(), facts[:1])
        journal.page_size = 1
        page = journal.events(head - 2)
        self.assertFalse(page["resync_required"])
        self.assertEqual(len(page["events"]), 1)
        self.assertEqual(page["latest_event_seq"], head - 1)
        self.assertEqual(journal.events(head - 1)["latest_event_seq"], head)
        with journal._lock, journal._db:
            journal._db.execute("UPDATE sessions SET ledger_cursor=0 WHERE session=?", (journal.session,))
        journal.collect(self.server.action_ledger)
        self.assertEqual(journal.snapshot()["event_seq"], head)
        self.assertEqual(journal.pending(), facts[:1])
        self.assertTrue(journal.events(0)["resync_required"])
        with journal._lock:
            self.assertEqual(journal._db.execute("SELECT count(*) FROM facts").fetchone()[0], 2)
            self.assertEqual(journal._db.execute("SELECT count(*) FROM source_tombstones").fetchone()[0], head)

    def test_state_idle_is_not_stop_proof_and_gate_epoch_is_rechecked(self):
        self.assertIsNone(self.s.goals.active)
        execution = self.c.state()["execution"]
        self.assertFalse(execution["stop_proven"])
        self.assertLess(execution["stop_proof_epoch"], execution["dispatcher_epoch"])
        self.execute()
        self.assertTrue(wait_for(lambda: self.c.state()["execution"]["stop_proven"]))
        dispatcher = self.server.action_dispatcher
        with self.s._condition, dispatcher._lock:
            dispatcher.set_gate(False)
            execution = self.c.state()["execution"]
            self.assertFalse(execution["stop_proven"])
            self.assertLess(execution["stop_proof_epoch"], execution["dispatcher_epoch"])
        self.assertTrue(self.server.action_service.await_stop_proof("test real empty proof"))
        with patch.object(self.s, "request_stop", wraps=self.s.request_stop) as stop, \
                patch.object(self.server.action_service, "await_stop_proof") as prove, \
                patch.object(self.server.action_ledger, "events") as ledger:
            state = DecisionState.parse(self.c.state()).to_dict()
            self.assertTrue(state["execution"]["stop_proven"])
            self.assertFalse(state["execution"]["gate_open"])
            self.assertGreaterEqual(state["execution"]["stop_proof_epoch"], state["execution"]["dispatcher_epoch"])
            stop.assert_not_called()
            prove.assert_not_called()
            ledger.assert_not_called()
        self.s.goals.block("manual review required")
        self.assertTrue(self.server.action_service.await_stop_proof("test blocked proof"))
        execution = self.c.state()["execution"]
        self.assertTrue(execution["blocked"])
        self.assertFalse(execution["stop_proven"])

    def test_state_active_unresolved_and_fault_cannot_claim_safe(self):
        self.active_goal()
        self.assertTrue(wait_for(lambda: self.c.state()["execution"]["unresolved"]))
        execution = self.c.state()["execution"]
        self.assertFalse(execution["stop_proven"])
        self.c._journal_fault()
        execution = self.c.state()["execution"]
        self.assertTrue(execution["blocked"])
        self.assertFalse(execution["stop_proven"])
