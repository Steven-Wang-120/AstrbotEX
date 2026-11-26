"""Bounded stop proofs survive the real contract, ledger and Goal chain."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from astrbot_ex.core import contracts
from astrbot_ex.core.actions import models
from astrbot_ex.core.actions.ledger import ActionLedger, OwnerBinding, StopEvidence
from tests import test_action_ledger as ledger_tests
from tests import test_action_dispatcher as dispatcher_tests
from tests import test_proved_failed_terminal as failed_tests
from tests.test_decision_service import wait_for
from tests.wiring_fixture import WiringFixture


def scope():
    return {"load": "unloaded", "window_ms": 2000, "range_raw": 5}


def proof(command_id="one", *, load="unloaded"):
    return StopEvidence(command_id, True, "stop_window", "window-001",
                        scope={**scope(), "load": load})


class StopScopeContractTests(unittest.TestCase):
    def test_ex_parsers_accept_scope_and_legacy_proof(self):
        for parser in (models, contracts):
            with self.subTest(parser=parser.__name__):
                parser.validate_stop_evidence({"stop_evidence": proof().to_dict()})
                parser.validate_stop_evidence({"stop_evidence": proof(load="loaded").to_dict()})
                parser.validate_stop_evidence({"stop_evidence": {
                    "stopped": True, "source": "legacy_plugin", "reference": "ref"}})
                with self.assertRaises(ValueError):
                    parser.validate_stop_evidence({"stop_evidence": {
                        "stopped": True, "source": "stop_window", "reference": "ref"}})

    def test_scope_requires_all_bounds_with_strict_types(self):
        invalid = [None, [], {}, {**scope(), "extra": 1},
                   {**scope(), "load": "unknown"}, {**scope(), "load": []}]
        invalid += [{key: value for key, value in scope().items() if key != missing}
                    for missing in scope()]
        invalid += [{**scope(), "window_ms": value} for value in (True, False, 0, -1, 2.0, "2")]
        invalid += [{**scope(), "range_raw": value} for value in (True, False, -1, 0.0, "0")]
        for parser in (models, contracts):
            for value in invalid:
                with self.subTest(parser=parser.__name__, scope=value), self.assertRaises(ValueError):
                    parser.validate_stop_evidence({"stop_evidence": {
                        "stopped": True, "source": "stop_window", "reference": "ref", "scope": value}})
            parser.validate_stop_evidence({"stop_evidence": {
                "stopped": True, "source": "stop_window", "reference": "ref",
                "scope": {"load": "loaded", "window_ms": 1, "range_raw": 0}}})

    def test_scope_does_not_weaken_positive_stop_requirement(self):
        for parser in (models, contracts):
            for stopped in (False, 1, "true", None):
                with self.subTest(parser=parser.__name__, stopped=stopped), self.assertRaises(ValueError):
                    parser.validate_stop_evidence({"stop_evidence": {**proof().to_dict(), "stopped": stopped}})

    def test_proof_owns_immutable_scope_snapshot(self):
        source = scope()
        evidence = StopEvidence("one", True, "stop_window", "ref", source)
        source["range_raw"] = 99
        evidence.scope["range_raw"] = 98
        evidence.to_dict()["scope"]["range_raw"] = 97
        self.assertEqual(evidence.scope, scope())
        self.assertEqual(evidence.to_dict()["scope"], scope())


class StopScopeLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.sqlite"
        self.ledger = ActionLedger(self.path)
        self.addCleanup(lambda: self.ledger.close())

    def admit(self, command_id="one"):
        self.ledger.admit(ledger_tests.command(command_id), ("arm",),
                          ledger_tests.BINDING, task_id="trusted-task").result(5)

    def restart(self):
        self.ledger.close()
        self.ledger = ActionLedger(self.path)

    def test_cancel_scope_is_atomic_in_event_proof_and_restart(self):
        self.admit()
        evidence = proof()
        result = self.ledger.report("one", ledger_tests.BINDING, "canceled",
                                    stop_evidence=evidence).result(5)
        self.assertEqual(result.held_resources, ())
        self.assertEqual(result.details["stop_evidence"], evidence.to_dict())
        event = self.ledger.events().result(5)[-1]
        self.assertEqual(event.to_action_event().to_dict()["details"]["stop_evidence"], evidence.to_dict())
        self.restart()
        self.assertEqual(self.ledger.stop_proof("one", ledger_tests.BINDING).result(5), evidence)
        self.assertEqual(self.ledger.get("one").result(5), result)

    def test_missing_window_scope_never_releases_resources(self):
        self.admit()
        invalid = StopEvidence("one", True, "stop_window", "no-scope")
        for status in ("canceled", "failed"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.ledger.report("one", ledger_tests.BINDING, status, stop_evidence=invalid)
        self.assertEqual(self.ledger.get("one").result(5).held_resources, ("arm",))
        self.assertIsNone(self.ledger.stop_proof("one", ledger_tests.BINDING).result(5))

    def test_unknown_requires_explicit_valid_proof_and_keeps_old_terminal(self):
        self.admit()
        before = self.ledger.report("one", ledger_tests.BINDING, "unknown").result(5)
        self.restart()
        self.assertEqual(self.ledger.get("one").result(5).held_resources, ("arm",))
        with self.assertRaises(ValueError):
            self.ledger.reconcile_stop("one", ledger_tests.BINDING,
                                      StopEvidence("one", True, "stop_window", "missing"))
        evidence = proof()
        after = self.ledger.reconcile_stop("one", ledger_tests.BINDING, evidence).result(5)
        self.assertEqual(after.status, "unknown")
        self.assertEqual(after.event_seq, before.event_seq)
        self.assertEqual(after.details_json, before.details_json)
        self.assertEqual(after.held_resources, ())
        self.restart()
        self.assertEqual(self.ledger.stop_proof("one", ledger_tests.BINDING).result(5), evidence)
        self.assertEqual(self.ledger.get("one").result(5).status, "unknown")

    def test_reconcile_rejects_changed_scope_even_with_same_reference(self):
        self.admit()
        self.ledger.report("one", ledger_tests.BINDING, "unknown").result(5)
        self.ledger.reconcile_stop("one", ledger_tests.BINDING, proof()).result(5)
        for change in ({"load": "loaded"}, {"range_raw": 6}, {"window_ms": 1999}):
            changed = StopEvidence("one", True, "stop_window", "window-001", {**scope(), **change})
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.ledger.reconcile_stop("one", ledger_tests.BINDING, changed).result(5)
        self.assertEqual(self.ledger.stop_proof("one", ledger_tests.BINDING).result(5), proof())

    def test_legacy_database_migrates_without_inventing_scope(self):
        self.admit()
        evidence = StopEvidence("one", True, "legacy_plugin", "legacy-ref")
        self.ledger.report("one", ledger_tests.BINDING, "canceled", stop_evidence=evidence).result(5)
        self.ledger.close()
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE old_proof (command_id TEXT PRIMARY KEY REFERENCES commands(command_id), "
                       "source TEXT NOT NULL, reference TEXT NOT NULL)")
            db.execute("INSERT INTO old_proof SELECT command_id,source,reference FROM stop_evidence")
            db.execute("DROP TABLE stop_evidence")
            db.execute("ALTER TABLE old_proof RENAME TO stop_evidence")
        self.ledger = ActionLedger(self.path)
        self.assertEqual(self.ledger.stop_proof("one", ledger_tests.BINDING).result(5), evidence)
        self.assertNotIn("scope", self.ledger.get("one").result(5).details["stop_evidence"])
        self.admit("two")
        self.ledger.report("two", ledger_tests.BINDING, "canceled", stop_evidence=proof("two")).result(5)
        self.restart()
        self.assertEqual(self.ledger.stop_proof("two", ledger_tests.BINDING).result(5).scope, scope())

    def test_persisted_window_proof_without_scope_is_not_trusted(self):
        self.admit()
        self.ledger.report("one", ledger_tests.BINDING, "unknown").result(5)
        self.ledger._submit(lambda db: db.execute(
            "INSERT INTO stop_evidence(command_id,source,reference) VALUES ('one','stop_window','old')")).result(5)
        with self.assertRaises(ValueError):
            self.ledger.stop_proof("one", ledger_tests.BINDING).result(5)
        self.assertEqual(self.ledger.get("one").result(5).held_resources, ("arm",))


class StopScopeDispatcherTests(unittest.TestCase):
    setUp = dispatcher_tests.DispatcherTests.setUp
    close = dispatcher_tests.DispatcherTests.close
    owner = dispatcher_tests.DispatcherTests.owner
    context = dispatcher_tests.DispatcherTests.context
    enabled = dispatcher_tests.DispatcherTests.enabled
    wait_status = dispatcher_tests.DispatcherTests.wait_status

    def test_scoped_failed_proof_is_compared_without_dropping_scope(self):
        plugin = self.owner(resource="joint")
        self.enabled("arm")
        self.dispatcher.start(dispatcher_tests.command("arm", "one")).result(5)
        self.wait_status("one", "accepted")
        with plugin._physical_lock:
            plugin._physical_running = False
        result = self.dispatcher.report("one", OwnerBinding("arm", 3), "failed",
                                        stop_evidence=proof()).result(5)
        self.assertEqual(result.held_resources, ())
        self.assertEqual(result.details["stop_evidence"]["scope"], scope())
        self.assertFalse(self.dispatcher.blocked)
        self.assertFalse(self.dispatcher._gate)


class StopScopeGoalTests(WiringFixture):
    active = failed_tests.FailedGoalTests.active

    def test_goal_failure_export_preserves_observation_scope(self):
        cid = self.active()
        evidence = proof(cid)
        self.server.action_dispatcher.report(cid, OwnerBinding("arm", 1), "failed",
                                              stop_evidence=evidence).result(5)
        self.assertTrue(wait_for(lambda: self.c.journal.snapshot()["feedback"] is not None), self.diagnostics())
        fact = self.c.journal.snapshot()["feedback"]
        self.assertEqual(fact["status"], "failed")
        for key in ("terminal_evidence", "failure_evidence"):
            self.assertEqual(fact["details"][key]["commands"][0]["stop_evidence"], evidence.to_dict())
        self.assertFalse(self.server.action_dispatcher._gate)
        self.assertFalse(self.server.action_dispatcher.blocked)


if __name__ == "__main__":
    unittest.main()
