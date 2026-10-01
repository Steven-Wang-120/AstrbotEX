from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests import action_ledger_crash_helper as crash_helper

from astrbot_ex.core.actions.ledger import (
    ActionLedger, LedgerBusy, LedgerClosed, LedgerFault, OwnerBinding, StopEvidence,
)
from astrbot_ex.core.actions.models import (
    ActionCommand, ActionStatus, ContractError, ErrorCode, MAX_VALIDATION_BYTES,
    measure_json_budget,
)


BINDING = OwnerBinding("arm", 3)


def command(command_id: str, *, params: dict | None = None) -> ActionCommand:
    return ActionCommand(
        command_id=command_id, ex_session="session", goal_id="goal",
        goal_revision=1, decision_id="decision", owner="arm",
        plugin_generation=3, action_id="arm.move.v2", operation="start",
        params={"meters": 1} if params is None else params, lease_ms=1000,
    )


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.sqlite"
        self.ledger = ActionLedger(self.path)
        self.addCleanup(self._close)

    def admit(self, command: ActionCommand, resources: list[str], binding: OwnerBinding):
        return self.ledger.admit(command, resources, binding, task_id="trusted-task")

    def _close(self) -> None:
        if self.ledger is not None:
            self.ledger.close()
            self.ledger = None

    def test_duplicate_ten_retries_and_changed_payload_or_reservation_conflict(self) -> None:
        first = self.admit(command("one"), ["arm"], BINDING).result()
        self.assertTrue(first.admitted_new)
        for _ in range(10):
            retry = self.admit(command("one"), ["arm"], BINDING).result()
            self.assertFalse(retry.admitted_new)
            self.assertEqual(retry.snapshot.event_seq, first.snapshot.event_seq)
        for changed, resources in ((command("one", params={"meters": 2}), ["arm"]),
                                   (command("one"), ["wrist"])):
            with self.assertRaises(ContractError) as caught:
                self.admit(changed, resources, BINDING).result()
            self.assertEqual(caught.exception.code, ErrorCode.DUPLICATE_REQUEST_ID_CONFLICT)
        self.assertEqual(len(self.ledger.events().result()), 1)

    def test_concurrent_exclusion_persists_across_restart_and_reconciliation(self) -> None:
        successes: list[str] = []
        conflicts: list[str] = []
        barrier = threading.Barrier(12)
        def contender(i: int) -> None:
            barrier.wait()
            try:
                if self.admit(command(str(i)), ["arm"], BINDING).result().admitted_new:
                    successes.append(str(i))
            except ContractError:
                conflicts.append(str(i))
        workers = [threading.Thread(target=contender, args=(i,)) for i in range(12)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            self.assertFalse(worker.is_alive())
        self.assertEqual((len(successes), len(conflicts)), (1, 11))
        winner = successes[0]
        self._close()
        self.ledger = ActionLedger(self.path)
        recovered = self.ledger.get(winner).result()
        self.assertEqual((recovered.status, recovered.reason_code, recovered.held_resources),
                         (ActionStatus.UNKNOWN, "resume_review", ("arm",)))
        self.assertFalse(self.admit(command(winner), ["arm"], BINDING).result().admitted_new)
        with self.assertRaises(ContractError):
            self.admit(command("other"), ["arm"], BINDING).result()
        proof = StopEvidence(winner, True, "plugin_stop", "stop-123")
        self.ledger.reconcile_stop(winner, BINDING, proof).result()
        self.assertEqual(self.ledger.get(winner).result().status, ActionStatus.UNKNOWN)
        self.assertTrue(self.admit(command("other"), ["arm"], BINDING).result().admitted_new)

    def test_committed_only_events_ack_and_immutable_snapshots(self) -> None:
        initial = self.admit(command("one"), ["arm"], BINDING).result().snapshot
        accepted = self.ledger.report("one", BINDING, ActionStatus.ACCEPTED).result()
        running = self.ledger.report("one", BINDING, ActionStatus.RUNNING).result()
        terminal = self.ledger.report("one", BINDING, ActionStatus.SUCCEEDED,
                                      details={"result": [1]}).result()
        self.assertEqual(initial.status, ActionStatus.ADMITTED)
        self.assertEqual(accepted.status, ActionStatus.ACCEPTED)
        self.assertEqual(running.status, ActionStatus.RUNNING)
        self.assertEqual(terminal.held_resources, ())
        copy = terminal.details
        copy["result"].append(2)
        self.assertEqual(self.ledger.get("one").result().details, {"result": [1]})
        events = self.ledger.events(limit=2).result()
        self.assertEqual(len(events), 2)
        all_events = events + self.ledger.events(after_seq=events[-1].event_seq).result()
        self.assertEqual([e.status for e in all_events], ["admitted", "accepted", "running", "succeeded"])
        self.assertTrue(self.ledger.ack(all_events[-1].event_seq).result())
        self.assertFalse(self.ledger.ack(99999).result())
        self.assertEqual(len(self.ledger.events(unacknowledged_only=True).result()), 3)
        self._close()
        self.ledger = ActionLedger(self.path)
        self.assertEqual([e.status for e in self.ledger.events().result()],
                         ["admitted", "accepted", "running", "succeeded"])
        self.assertEqual(self.ledger.events().result()[-1].acknowledged, True)

    def test_binding_and_out_of_order_or_duplicate_terminal(self) -> None:
        self.admit(command("one"), ["arm"], BINDING).result()
        with self.assertRaises(ContractError):
            self.admit(command("other"), [], OwnerBinding("arm", 4))
        for wrong in (OwnerBinding("other", 3), OwnerBinding("arm", 4)):
            with self.assertRaises(ContractError):
                self.ledger.report("one", wrong, ActionStatus.ACCEPTED).result()
        with self.assertRaises(ContractError):
            self.ledger.report("one", BINDING, ActionStatus.RUNNING).result()
        self.ledger.report("one", BINDING, ActionStatus.ACCEPTED).result()
        terminal = self.ledger.report("one", BINDING, ActionStatus.TIMED_OUT).result()
        self.assertEqual(terminal.held_resources, ("arm",))
        repeat = self.ledger.report("one", BINDING, ActionStatus.TIMED_OUT,
                                    details={"malicious": True}).result()
        self.assertEqual(repeat, terminal)
        with self.assertRaises(ContractError):
            self.ledger.report("one", BINDING, ActionStatus.SUCCEEDED).result()
        self.assertEqual(len(self.ledger.events().result()), 3)

    def test_cancel_requires_positive_stop_evidence(self) -> None:
        self.admit(command("one"), ["arm"], BINDING).result()
        with self.assertRaises(ValueError):
            self.ledger.report("one", BINDING, ActionStatus.CANCELED)
        with self.assertRaises(ValueError):
            self.ledger.report("one", BINDING, ActionStatus.CANCELED,
                               stop_evidence=StopEvidence("one", False, "plugin", "ref"))
        snapshot = self.ledger.report("one", BINDING, ActionStatus.CANCELED,
                                      stop_evidence=StopEvidence("one", True, "plugin", "ref")).result()
        self.assertEqual(snapshot.held_resources, ())
        self.assertEqual(self.ledger.get("one").result().status, "canceled")

    def test_fault_and_queue_saturation_fail_closed(self) -> None:
        self._close()
        self.ledger = ActionLedger(self.path, queue_size=1)
        entered = threading.Event()
        release = threading.Event()
        def hold(conn: sqlite3.Connection) -> None:
            entered.set()
            release.wait(5)
        running = self.ledger._submit(hold)
        self.assertTrue(entered.wait(5))
        queued = self.admit(command("one"), [], BINDING)
        with self.assertRaises(LedgerBusy):
            self.admit(command("two"), [], BINDING)
        with self.assertRaises(LedgerBusy):
            self.ledger.close()
        release.set()
        running.result(timeout=5)
        queued.result(timeout=5)
        with self.assertRaises(LedgerBusy):
            self.admit(command("after-overflow"), [], BINDING)
        self.ledger.close()
        with self.assertRaises(LedgerClosed):
            self.admit(command("three"), [], BINDING)
        self.ledger = None

        self.ledger = ActionLedger(self.path)
        fault = self.ledger._submit(lambda conn: conn.execute("INSERT INTO missing_table VALUES (1)"))
        with self.assertRaises(LedgerFault):
            fault.result(timeout=5)
        with self.assertRaises(LedgerFault):
            self.admit(command("four"), [], BINDING)
        with self.assertRaises(LedgerFault):
            self._close()
        self.ledger = None

    def test_sqlite_fault_closes_gate_before_future_callback(self) -> None:
        self.ledger._submit(lambda conn: conn.execute("PRAGMA query_only=ON")).result()
        entered, release = threading.Event(), threading.Event()
        def hold(conn: sqlite3.Connection) -> None:
            entered.set()
            release.wait(5)
        running = self.ledger._submit(hold)
        self.assertTrue(entered.wait(5))
        failing = self.ledger._submit(lambda conn: conn.execute("CREATE TABLE forbidden (id INTEGER)"))
        observed = []
        callback_done = threading.Event()
        def on_failure(future) -> None:
            try:
                health = self.ledger.health
                try:
                    self.admit(command("after-fault"), [], BINDING)
                except Exception as exc:
                    observed.append((health, type(exc)))
                else:
                    observed.append((health, None))
            finally:
                callback_done.set()
        failing.add_done_callback(on_failure)
        release.set()
        running.result(timeout=5)
        with self.assertRaises(LedgerFault):
            failing.result(timeout=5)
        self.assertTrue(callback_done.wait(5))
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0][0].faulted)
        self.assertTrue(observed[0][0].admission_blocked)
        self.assertEqual(observed[0][1], LedgerFault)
        with self.assertRaises(LedgerFault):
            self._close()
        self.ledger = None
        self.ledger = ActionLedger(self.path)
        self.assertIsNone(self.ledger.get("after-fault").result())

    def test_full_queue_close_blocks_admission_and_can_retry(self) -> None:
        self._close()
        self.ledger = ActionLedger(self.path, queue_size=1)
        self.admit(command("one"), [], BINDING).result()
        entered, release = threading.Event(), threading.Event()
        def hold(conn: sqlite3.Connection) -> None:
            entered.set()
            release.wait(5)
        running = self.ledger._submit(hold)
        self.assertTrue(entered.wait(5))
        queued = self.ledger.report("one", BINDING, ActionStatus.ACCEPTED)
        try:
            with self.assertRaises(LedgerBusy):
                self.ledger.close(timeout=.01)
            health = self.ledger.health
            self.assertTrue(health.admission_blocked)
            self.assertFalse(health.closed)
            self.assertFalse(health.faulted)
            with self.assertRaises(LedgerBusy):
                self.admit(command("blocked-by-close"), [], BINDING)
        finally:
            release.set()
        running.result(timeout=5)
        self.assertEqual(queued.result(timeout=5).status, ActionStatus.ACCEPTED)
        self.assertEqual(self.ledger.get("one").result(timeout=5).status, ActionStatus.ACCEPTED)
        self.ledger.close()
        self.assertTrue(self.ledger.health.closed)
        with self.assertRaises(LedgerClosed):
            self.ledger.get("one")
        self.ledger = None

    def test_events_filter_and_close_timeout_strict_types(self) -> None:
        for invalid in (None, 0, 1, "true", [], {}):
            with self.subTest(unacknowledged_only=invalid), self.assertRaises(ValueError):
                self.ledger.events(unacknowledged_only=invalid)
        self.assertEqual(self.ledger.events(unacknowledged_only=False).result(), ())
        self.assertEqual(self.ledger.events(unacknowledged_only=True).result(), ())
        for invalid in (None, True, False, "1", 0, -1, float("nan"),
                        float("inf"), -float("inf")):
            with self.subTest(timeout=invalid), self.assertRaises(ValueError):
                self.ledger.close(timeout=invalid)
            self.assertFalse(self.ledger.health.closed)
        self.assertIsNone(self.ledger.get("still-open").result())

    def test_pagination_and_one_thousand_seeded_status_sequences(self) -> None:
        rng = random.Random(20260928)
        for i in range(1000):
            cid = f"run-{i:04d}"
            resource = f"resource-{i:04d}"
            initial = self.admit(command(cid), [resource], BINDING).result().snapshot
            state = initial.status
            for _ in range(rng.randint(1, 5)):
                candidate = rng.choice(tuple((ActionStatus.ACCEPTED, ActionStatus.RUNNING,
                                              ActionStatus.SUCCEEDED, ActionStatus.FAILED,
                                              ActionStatus.TIMED_OUT, ActionStatus.UNKNOWN)))
                before = self.ledger.get(cid).result()
                try:
                    state = self.ledger.report(cid, BINDING, candidate).result().status
                except ContractError:
                    self.assertEqual(self.ledger.get(cid).result(), before)
                    continue
                if before.status in ("succeeded", "failed", "timed_out", "unknown"):
                    self.assertEqual(self.ledger.get(cid).result(), before)
                self.assertEqual(state, candidate)
            current = self.ledger.get(cid).result()
            if current.status in ("failed", "timed_out", "unknown"):
                self.assertEqual(current.held_resources, (resource,))
            if current.status == "succeeded":
                self.assertEqual(current.held_resources, ())
        page = self.ledger.list_commands(limit=17).result()
        self.assertEqual(len(page), 17)
        self.assertGreater(self.ledger.list_commands(after_id=page[-1].command_id, limit=17).result()[0].command_id,
                           page[-1].command_id)

    def test_real_sqlite_write_failure_blocks_admission(self) -> None:
        # SQLite's query_only switch gives a deterministic storage write error.
        self.ledger._submit(lambda conn: conn.execute("PRAGMA query_only=ON")).result()
        with self.assertRaises(LedgerFault):
            self.admit(command("write-fault"), ["arm"], BINDING).result(timeout=5)
        with self.assertRaises(LedgerFault):
            self.admit(command("another"), [], BINDING)
        with self.assertRaises(LedgerFault):
            self._close()
        self.ledger = None
        self.ledger = ActionLedger(self.path)
        self.assertIsNone(self.ledger.get("write-fault").result())
        self.assertEqual(self.ledger.events().result(), ())

    def test_freezes_command_before_writer_runs(self) -> None:
        entered, release = threading.Event(), threading.Event()
        def hold(conn):
            entered.set()
            release.wait(5)
        waiting = self.ledger._submit(hold)
        self.assertTrue(entered.wait(5))
        item = command("original", params={"meters": [1]})
        pending = self.admit(item, ["arm"], BINDING)
        item.command_id = "changed"
        item.params["meters"].append(2)
        release.set()
        waiting.result(timeout=5)
        self.assertEqual(pending.result(timeout=5).snapshot.command_id, "original")
        self.assertIsNone(self.ledger.get("changed").result())
        self.assertEqual(json.loads(self.ledger.get("original").result().canonical_command)
                         ["command"]["params"], {"meters": [1]})

    def test_cancel_queued_and_running_transactions(self) -> None:
        entered, release = threading.Event(), threading.Event()
        def hold(conn):
            entered.set()
            release.wait(5)
        running = self.ledger._submit(hold)
        self.assertTrue(entered.wait(5))
        queued = self.admit(command("skipped"), ["arm"], BINDING)
        self.assertTrue(queued.cancel())
        release.set()
        running.result(timeout=5)
        self.assertIsNone(self.ledger.get("skipped").result(timeout=5))
        entered.clear()
        release.clear()
        def transaction(conn):
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO commands(command_id,canonical,owner,generation,status,event_seq,task_id) "
                         "VALUES ('active','{}','arm',3,'admitted',0,'trusted-task')")
            entered.set()
            release.wait(5)
            conn.commit()
            return "committed"
        active = self.ledger._submit(transaction)
        self.assertTrue(entered.wait(5))
        self.assertFalse(active.cancel())
        release.set()
        self.assertEqual(active.result(timeout=5), "committed")
        self.assertEqual(self.ledger.get("active").result().status, ActionStatus.ADMITTED)

    def test_query_overflow_latches_cached_gate(self) -> None:
        self._close()
        self.ledger = ActionLedger(self.path, queue_size=1)
        entered, release = threading.Event(), threading.Event()
        def hold(conn):
            entered.set()
            release.wait(5)
        running = self.ledger._submit(hold)
        self.assertTrue(entered.wait(5))
        queued = self.ledger.events()
        with self.assertRaises(LedgerBusy):
            self.ledger.get("query")
        self.assertTrue(self.ledger.health.admission_blocked)
        release.set()
        running.result(timeout=5)
        queued.result(timeout=5)
        with self.assertRaises(LedgerBusy):
            self.admit(command("blocked"), [], BINDING)

    def test_identity_stop_proof_and_strict_inputs(self) -> None:
        for bad in (None, "", "x" * 257, True):
            with self.assertRaises(ValueError):
                self.ledger.admit(command("bad"), [], BINDING, task_id=bad)
        for bad_binding in (OwnerBinding("arm", True), OwnerBinding("", 3), OwnerBinding("arm", -1)):
            with self.assertRaises(ValueError):
                self.admit(command("bad"), [], bad_binding)
        self.admit(command("one"), ["arm"], BINDING).result()
        for bad in (StopEvidence("one", True, "x" * 257, "ref"),
                    StopEvidence("other", True, "source", "ref"),
                    StopEvidence("one", True, "source", "")):
            with self.assertRaises(ValueError):
                self.ledger.report("one", BINDING, ActionStatus.CANCELED, stop_evidence=bad)
        proof = StopEvidence("one", True, "plugin", "stop-1")
        with self.assertRaises(ValueError):
            self.ledger.report("one", BINDING, ActionStatus.CANCELED,
                               details={"stop_evidence": {}}, stop_evidence=proof)
        self.ledger.report("one", BINDING, ActionStatus.CANCELED, stop_evidence=proof).result()
        events = self.ledger.events().result()
        self.assertEqual(events[-1].details["stop_evidence"],
                         {"stopped": True, "source": "plugin", "reference": "stop-1"})
        self.assertEqual(events[-1].to_action_event().task_id, "trusted-task")
        self.assertEqual(len({event.event_id for event in events}), len(events))
        self._close()
        self.ledger = ActionLedger(self.path)
        restored = self.ledger.events().result()
        self.assertEqual(restored, events)
        self.assertEqual(restored[-1].to_action_event().to_dict()["details"], events[-1].details)
        with self.assertRaises(ContractError):
            self.ledger.admit(command("one"), ["arm"], BINDING, task_id="another-task").result()

    def test_report_overflow_latches_gate(self) -> None:
        self._close()
        self.ledger = ActionLedger(self.path, queue_size=1)
        self.admit(command("one"), [], BINDING).result()
        entered, release = threading.Event(), threading.Event()
        def hold(conn):
            entered.set()
            release.wait(5)
        running = self.ledger._submit(hold)
        self.assertTrue(entered.wait(5))
        queued = self.ledger.get("one")
        with self.assertRaises(LedgerBusy):
            self.ledger.report("one", BINDING, ActionStatus.ACCEPTED)
        self.assertTrue(self.ledger.health.admission_blocked)
        release.set()
        running.result(timeout=5)
        queued.result(timeout=5)
        self.assertEqual(self.ledger.get("one").result().status, ActionStatus.ADMITTED)

    def test_legacy_row_never_fabricates_task_identity(self) -> None:
        self.admit(command("one"), [], BINDING).result()
        self._close()
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("UPDATE commands SET task_id='' WHERE command_id='one'")
            conn.commit()
        finally:
            conn.close()
        self.ledger = ActionLedger(self.path)
        event = self.ledger.events().result()[0]
        self.assertFalse(event.complete)
        with self.assertRaises(ValueError):
            event.to_action_event()
        with self.assertRaises(ContractError):
            self.admit(command("one"), [], BINDING).result()

    def test_durable_event_envelope_budget_and_atomic_rollback(self) -> None:
        item = command("c")
        item.ex_session = "s"
        item.goal_id = "g"
        item.decision_id = "d"
        item.owner = "arm"
        self.ledger.admit(item, ["arm"], BINDING, task_id="task").result()
        initial = self.ledger.get("c").result()
        initial_events = self.ledger.events().result()
        candidate = {
            "event_id": "0" * 32, "event_seq": initial.event_seq + 1,
            "ex_session": "s", "task_id": "task", "goal_id": "g",
            "goal_revision": 1, "command_id": "c", "owner": "arm",
            "status": ActionStatus.ACCEPTED, "reason_code": "", "details": {"blob": ""},
        }
        # JSON budget includes the object structure, generated identity and seq.
        baseline = len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode())
        size = MAX_VALIDATION_BYTES - baseline
        self.assertIsNone(measure_json_budget({**candidate, "details": {"blob": "x" * size}}))
        valid = self.ledger.report("c", BINDING, ActionStatus.ACCEPTED,
                                   details={"blob": "x" * size}).result()
        self.assertEqual(valid.held_resources, ("arm",))
        event = self.ledger.events(after_seq=initial.event_seq).result()[0]
        self.assertEqual(event.to_action_event().to_dict()["details"], {"blob": "x" * size})
        self.assertLessEqual(len(json.dumps(event.to_action_event().to_dict(),
                                           ensure_ascii=False, separators=(",", ":")).encode()),
                             MAX_VALIDATION_BYTES)
        self.assertEqual(len(initial_events), 1)
        before = self.ledger.get("c").result()
        previous_events = self.ledger.events().result()
        # Details alone fit; the complete event envelope cannot.
        oversized = {"blob": "x" * (MAX_VALIDATION_BYTES - 64)}
        self.assertIsNone(measure_json_budget(oversized))
        with self.assertRaises(ContractError) as caught:
            self.ledger.report("c", BINDING, ActionStatus.RUNNING, details=oversized).result()
        self.assertEqual(caught.exception.code, ErrorCode.VALUE_BUDGET_EXCEEDED)
        self.assertEqual(self.ledger.get("c").result(), before)
        self.assertEqual(self.ledger.events().result(), previous_events)
        self.assertFalse(self.ledger.health.faulted)
        self._close()
        self.ledger = ActionLedger(self.path)
        restored = self.ledger.events(after_seq=initial.event_seq).result()[0]
        self.assertEqual(restored.event_id, event.event_id)
        self.assertEqual(restored.to_action_event().details, {"blob": "x" * size})

    def test_stop_proof_envelope_overflow_rolls_back_release(self) -> None:
        self.admit(command("c"), ["arm"], BINDING).result()
        before = self.ledger.get("c").result()
        previous_events = self.ledger.events().result()
        details = {"blob": "x" * (MAX_VALIDATION_BYTES - 256)}
        self.assertIsNone(measure_json_budget(details))
        plain = {"event_id": "0" * 32, "event_seq": before.event_seq + 1,
                 "ex_session": "session", "task_id": "trusted-task", "goal_id": "goal",
                 "goal_revision": 1, "command_id": "c", "owner": "arm",
                 "status": ActionStatus.CANCELED, "reason_code": "", "details": details}
        self.assertIsNone(measure_json_budget(plain))
        proof = StopEvidence("c", True, "plugin", "proof")
        self.assertEqual(measure_json_budget({**plain, "details": {
            **details, "stop_evidence": {"stopped": True, "source": "plugin", "reference": "proof"}}}),
                         ErrorCode.VALUE_BUDGET_EXCEEDED)
        with self.assertRaises(ContractError) as caught:
            self.ledger.report("c", BINDING, ActionStatus.CANCELED,
                               details=details, stop_evidence=proof).result()
        self.assertEqual(caught.exception.code, ErrorCode.VALUE_BUDGET_EXCEEDED)
        self.assertEqual(self.ledger.get("c").result(), before)
        self.assertEqual(self.ledger.events().result(), previous_events)
        self.assertEqual(self.ledger._submit(lambda conn: conn.execute(
            "SELECT COUNT(*) FROM stop_evidence WHERE command_id='c'"
        ).fetchone()[0]).result(), 0)
        self.assertTrue(before.held_resources)
        self.assertFalse(self.ledger.health.faulted)

    def test_crash_marker_portable_binary_flag(self) -> None:
        # Simulate a POSIX os module without O_BINARY while exercising helper code.
        posix_os = SimpleNamespace(O_CREAT=os.O_CREAT, O_WRONLY=os.O_WRONLY,
                                   O_APPEND=os.O_APPEND)
        with patch.object(crash_helper, "os", posix_os):
            self.assertEqual(crash_helper.marker_open_flags(),
                             os.O_CREAT | os.O_WRONLY | os.O_APPEND)
        self.assertEqual(crash_helper.marker_open_flags(),
                         os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0))

    def test_real_process_crash_boundaries(self) -> None:
        self._close()
        helper = Path(__file__).with_name("action_ledger_crash_helper.py")
        for boundary in ("before_admission", "during_admission_transaction", "after_commit_before_delivery", "running_before_terminal_commit"):
            db = Path(self.temp.name) / f"{boundary}.sqlite"
            root = Path(__file__).resolve().parents[1]
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(root) + os.pathsep + environment.get("PYTHONPATH", "")
            result = subprocess.run([sys.executable, str(helper), str(db), boundary],
                                    cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with ActionLedger(db) as reopened:
                snapshot = reopened.get("crash-command").result()
                if boundary in ("before_admission", "during_admission_transaction"):
                    self.assertIsNone(snapshot)
                    self.assertEqual(reopened.events().result(), ())
                    self.assertTrue(reopened.admit(command("crash-command"), ["arm"], BINDING,
                                                   task_id="trusted-task").result().admitted_new)
                else:
                    self.assertEqual((snapshot.status, snapshot.reason_code, snapshot.held_resources),
                                     (ActionStatus.UNKNOWN, "resume_review", ("arm",)))
                    replay = reopened.admit(command("crash-command"), ["arm"], BINDING,
                                            task_id="trusted-task").result()
                    self.assertFalse(replay.admitted_new)
                    self.assertEqual(reopened.events().result()[-1].status, ActionStatus.UNKNOWN)
                    if boundary == "after_commit_before_delivery":
                        self.assertFalse(Path(str(db) + ".starts").exists())
                    if boundary == "running_before_terminal_commit":
                        self.assertEqual((Path(str(db) + ".starts").read_bytes().count(b"start\n")), 1)
                        self.assertFalse(reopened.admit(command("crash-command"), ["arm"], BINDING,
                                                        task_id="trusted-task").result().admitted_new)
                        self.assertEqual(Path(str(db) + ".starts").read_bytes().count(b"start\n"), 1)
        self.ledger = ActionLedger(self.path)


if __name__ == "__main__":
    unittest.main()
