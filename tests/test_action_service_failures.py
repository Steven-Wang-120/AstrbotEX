from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

from types import SimpleNamespace

from astrbot_ex.core.actions.ledger import ActionLedger, LedgerFault, OwnerBinding, StopEvidence
from astrbot_ex.core.actions.models import ActionCommand, ActionStatus
from astrbot_ex.core.actions.service import ActionService
from astrbot_ex.core.decision.catalog import CapabilityCatalog


class FakeDispatcher:
    def __init__(self):
        self._lock = threading.RLock()
        self._gate = False
        self.blocked = False
        self.faults = ()
        self.canceled = []

    def set_gate(self, enabled):
        with self._lock:
            self._gate = enabled

    def update_versions(self, **versions):
        self.set_gate(False)

    def cancel(self, command_id, binding, reason):
        self.canceled.append((command_id, binding, reason))
        result = Future()
        result.set_result(None)
        return result

    def close(self):
        pass


class ActionServiceFailuresTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = ActionLedger(Path(self.temp.name) / "actions.sqlite3")
        self.dispatcher = FakeDispatcher()
        self.service = ActionService(self.ledger, self.dispatcher, CapabilityCatalog(), stop_timeout=0.05)
        self.binding = OwnerBinding("test-owner", 1)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def running_command(self, resources):
        command = ActionCommand.parse({
            "schema_version": 1, "command_id": "test-command",
            "ex_session": "test-session", "goal_id": "test-goal", "goal_revision": 1,
            "decision_id": "test-decision", "owner": self.binding.owner,
            "plugin_generation": self.binding.generation,
            "action_id": "test-owner.move.v1", "operation": "start", "params": {},
            "lease_ms": 1000,
        })
        self.ledger.admit(command, resources, self.binding, task_id="test-task").result(timeout=1)
        self.ledger.report(command.command_id, self.binding, ActionStatus.ACCEPTED).result(timeout=1)
        return self.ledger.report(command.command_id, self.binding, ActionStatus.RUNNING).result(timeout=1)

    def test_deadline_after_pending_does_not_submit_another_read(self):
        row = self.running_command(("test-resource",))
        self.ledger.report(row.command_id, self.binding, ActionStatus.UNKNOWN).result(1)
        self.service.stop_timeout = 1
        now = [0.0]
        clock = SimpleNamespace(monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, 1.0))
        original_get = self.ledger.get
        latched = Future()
        calls = []
        def get(command_id):
            calls.append((command_id, now[0]))
            return original_get(command_id) if len(calls) == 1 else latched
        with patch("astrbot_ex.core.actions.service.time", clock), \
                patch.object(self.ledger, "get", side_effect=get), \
                patch.object(self.ledger, "stop_proof", wraps=self.ledger.stop_proof) as proof:
            self.assertFalse(self.service.await_stop_proof(binding=self.binding))
            self.assertEqual(self.service._last_error, "stop proof pending: test-command")
            self.assertEqual(calls, [(row.command_id, 0.0)])
            self.assertEqual(proof.call_count, 1)
        self.assertEqual(self.ledger.get(row.command_id).result(1).held_resources, ("test-resource",))
        self.assertIsNone(self.ledger.stop_proof(row.command_id, self.binding).result(1))
        self.ledger.reconcile_stop(row.command_id, self.binding,
            StopEvidence(row.command_id, True, "fixture", "committed-after-deadline")).result(1)
        self.assertTrue(self.service.await_stop_proof(binding=self.binding))

    def test_deadline_between_get_and_proof_does_not_submit_proof_read(self):
        row = self.running_command(("test-resource",))
        self.ledger.report(row.command_id, self.binding, ActionStatus.UNKNOWN).result(1)
        self.service.stop_timeout = 1
        now = [0.0]
        original_get = self.ledger.get
        def get(command_id):
            result = original_get(command_id)
            result.result(1)
            now[0] = 1.0
            return result
        clock = SimpleNamespace(monotonic=lambda: now[0])
        with patch("astrbot_ex.core.actions.service.time", clock), \
                patch.object(self.ledger, "get", side_effect=get), \
                patch.object(self.ledger, "stop_proof", return_value=Future()) as proof:
            self.assertFalse(self.service.await_stop_proof(binding=self.binding))
            self.assertEqual(self.service._last_error, "stop proof pending: test-command")
            proof.assert_not_called()
        self.assertEqual(self.ledger.get(row.command_id).result(1).held_resources, ("test-resource",))

    def test_deadline_between_rows_does_not_submit_next_read(self):
        row = self.running_command(("test-resource",))
        self.service.stop_timeout = 1
        now = [0.0]
        clock = SimpleNamespace(monotonic=lambda: now[0])
        def get(command_id):
            result = Future()
            result.set_result(row)
            now[0] = 1.0
            return result
        rows = Future()
        rows.set_result((row, SimpleNamespace(command_id="second-command", owner=row.owner, generation=row.generation)))
        with patch("astrbot_ex.core.actions.service.time", clock), \
                patch.object(self.ledger, "list_commands", return_value=rows), \
                patch.object(self.ledger, "get", side_effect=get) as gets:
            self.assertFalse(self.service.await_stop_proof(binding=self.binding))
            self.assertIn("stop proof pending:", self.service._last_error)
            self.assertIn("test-command", self.service._last_error)
            self.assertIn("second-command", self.service._last_error)
            gets.assert_called_once_with(row.command_id)

    def test_expired_budget_does_not_accept_even_success_or_submit_reads(self):
        self.running_command(("test-resource",))
        self.service.stop_timeout = 1
        now = [0.0]
        original_list = self.ledger.list_commands
        def listed(**kwargs):
            result = original_list(**kwargs)
            result.result(1)
            now[0] = 1.0
            return result
        success = Future()
        success.set_result(SimpleNamespace(status=ActionStatus.SUCCEEDED))
        with patch("astrbot_ex.core.actions.service.time", SimpleNamespace(monotonic=lambda: now[0])), \
                patch.object(self.ledger, "list_commands", side_effect=listed), \
                patch.object(self.ledger, "get", return_value=success) as gets:
            self.assertFalse(self.service.await_stop_proof(binding=self.binding))
            self.assertIn("stop proof pending:", self.service._last_error)
            gets.assert_not_called()
        self.assertEqual(self.ledger.get("test-command").result(1).held_resources, ("test-resource",))

    def test_submission_time_is_deducted_from_each_future_wait_budget(self):
        row = self.running_command(("test-resource",))
        row = self.ledger.report(row.command_id, self.binding, ActionStatus.UNKNOWN).result(1)
        self.service.stop_timeout = 1
        now, waits = [0.0], []
        class TimedFuture(Future):
            def result(self, timeout=None):
                waits.append(timeout)
                return super().result(timeout)
        def submitted(value):
            now[0] += 0.25
            result = TimedFuture()
            result.set_result(value)
            return result
        clock = SimpleNamespace(monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, 1.0))
        with patch("astrbot_ex.core.actions.service.time", clock), \
                patch.object(self.ledger, "list_commands", side_effect=lambda **kwargs: submitted((row,))), \
                patch.object(self.ledger, "get", side_effect=lambda command_id: submitted(row)), \
                patch.object(self.ledger, "stop_proof", side_effect=lambda *args: submitted(None)):
            self.assertFalse(self.service.await_stop_proof(binding=self.binding))
            self.assertEqual(waits, [0.75, 0.5, 0.25])
            self.assertEqual(self.service._last_error, "stop proof pending: test-command")

    def test_proof_arriving_at_deadline_is_not_success_until_next_call(self):
        row = self.running_command(("test-resource",))
        self.ledger.report(row.command_id, self.binding, ActionStatus.UNKNOWN).result(1)
        evidence = StopEvidence(row.command_id, True, "fixture", "parked")
        self.ledger.reconcile_stop(row.command_id, self.binding, evidence).result(1)
        self.service.stop_timeout = 1
        now = [0.0]
        original = self.ledger.stop_proof
        def proof(*args):
            result = original(*args)
            result.result(1)
            now[0] = 1.0
            return result
        with patch("astrbot_ex.core.actions.service.time", SimpleNamespace(monotonic=lambda: now[0])), \
                patch.object(self.ledger, "stop_proof", side_effect=proof):
            self.assertFalse(self.service.await_stop_proof(binding=self.binding))
            self.assertEqual(self.service._last_error, "stop proof pending: test-command")
        self.assertEqual(self.ledger.stop_proof(row.command_id, self.binding).result(1), evidence)
        self.assertTrue(self.service.await_stop_proof(binding=self.binding))

    def test_pagination_stops_at_deadline_without_another_submission(self):
        self.service.stop_timeout = 1
        now = [0.0]
        def listed(**kwargs):
            result = Future()
            result.set_result(tuple(SimpleNamespace(command_id=str(index)) for index in range(500)))
            now[0] = 1.0
            return result
        with patch("astrbot_ex.core.actions.service.time", SimpleNamespace(monotonic=lambda: now[0])), \
                patch.object(self.ledger, "list_commands", side_effect=listed) as lists, \
                patch.object(self.ledger, "get") as gets:
            self.assertFalse(self.service.await_stop_proof())
            self.assertEqual(self.service._last_error, "stop proof pending: ledger scan incomplete")
            lists.assert_called_once_with(after_id="", limit=500)
            gets.assert_not_called()

    def test_unfinished_read_at_total_deadline_preserves_known_pending(self):
        row = self.running_command(("test-resource",))
        self.service.stop_timeout = 1
        now = [0.0]
        ready = Future()
        ready.set_result(row)
        class DeadlineFuture(Future):
            def result(self, timeout=None):
                self.wait_budget = timeout
                now[0] = 1.0
                raise TimeoutError()
        unfinished = DeadlineFuture()
        with patch("astrbot_ex.core.actions.service.time", SimpleNamespace(
                monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, 0.5))), \
                patch.object(self.ledger, "get", side_effect=[ready, unfinished]) as gets:
            self.assertFalse(self.service.await_stop_proof())
            self.assertEqual(gets.call_count, 2)
            self.assertEqual(unfinished.wait_budget, 0.5)
            self.assertEqual(self.service._last_error, "stop proof pending: test-command")
            self.assertFalse(unfinished.done())
        self.assertEqual(self.ledger.get(row.command_id).result(1).held_resources, ("test-resource",))

    def test_io_timeout_completed_at_deadline_is_not_reclassified_as_pending(self):
        row = self.running_command(("test-resource",))
        self.service.stop_timeout = 1
        now = [0.0]
        class FailedFuture(Future):
            def result(self, timeout=None):
                now[0] = 1.0
                return super().result(timeout)
        failed = FailedFuture()
        failed.set_exception(TimeoutError("actual ledger timeout"))
        with patch("astrbot_ex.core.actions.service.time", SimpleNamespace(monotonic=lambda: now[0])), \
                patch.object(self.ledger, "get", return_value=failed):
            self.assertFalse(self.service.await_stop_proof())
            self.assertEqual(self.service._last_error, "stop proof unavailable: TimeoutError: actual ledger timeout")
        self.assertEqual(self.ledger.get(row.command_id).result(1).held_resources, ("test-resource",))

    def test_io_timeout_after_known_pending_remains_unavailable(self):
        row = self.running_command(("test-resource",))
        now = [0.0]
        self.service.stop_timeout = 1
        failed = Future()
        failed.set_exception(TimeoutError("injected I/O timeout"))
        ready = Future()
        ready.set_result(row)
        with patch("astrbot_ex.core.actions.service.time", SimpleNamespace(
                monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, 0.5))), \
                patch.object(self.ledger, "get", side_effect=[ready, failed]) as gets:
            self.assertFalse(self.service.await_stop_proof(binding=self.binding))
            self.assertEqual(gets.call_count, 2)
            self.assertEqual(self.service._last_error, "stop proof unavailable: TimeoutError: injected I/O timeout")
        self.assertEqual(self.ledger.get(row.command_id).result(1).held_resources, ("test-resource",))

    def test_record_disappearing_after_known_pending_remains_unavailable(self):
        row = self.running_command(("test-resource",))
        now = [0.0]
        self.service.stop_timeout = 1
        ready, missing = Future(), Future()
        ready.set_result(row)
        missing.set_result(None)
        with patch("astrbot_ex.core.actions.service.time", SimpleNamespace(
                monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, 0.5))), \
                patch.object(self.ledger, "get", side_effect=[ready, missing]) as gets:
            self.assertFalse(self.service.await_stop_proof(binding=self.binding))
            self.assertEqual(gets.call_count, 2)
            self.assertEqual(self.service._last_error, "stop proof unavailable: RuntimeError: command record missing: test-command")
        self.assertEqual(self.ledger.get(row.command_id).result(1).held_resources, ("test-resource",))

    def test_stop_epoch_requires_global_success_and_cannot_skip_a_newer_stop(self):
        self.dispatcher._epoch = 1
        row = self.running_command(("test-resource",))
        self.assertFalse(self.service.await_stop_proof())
        self.assertEqual(self.service._stop_proof_epoch, -1)
        self.ledger.report(row.command_id, self.binding, ActionStatus.UNKNOWN).result(1)
        self.ledger.reconcile_stop(row.command_id, self.binding,
            StopEvidence(row.command_id, True, "fixture", "parked")).result(1)
        self.assertTrue(self.service.await_stop_proof(binding=self.binding))
        self.assertEqual(self.service._stop_proof_epoch, -1)
        self.assertTrue(self.service.await_stop_proof())
        self.assertEqual(self.service._stop_proof_epoch, 1)
        with patch.object(self.service, "request_stops", wraps=self.service.request_stops) as stops:
            self.assertTrue(self.service.stop_actions("completed old stop", after_epoch=1))
            stops.assert_not_called()
            self.dispatcher._epoch = 2
            self.assertTrue(self.service.stop_actions("new stop", after_epoch=2))
            stops.assert_called_once_with("new stop", binding=None)

    def test_gate_change_during_proof_cannot_stamp_completed_stop_epoch(self):
        self.dispatcher._epoch = 1
        original = self.ledger.list_commands
        def listed(**kwargs):
            result = original(**kwargs)
            result.result(1)
            self.dispatcher._epoch = 2
            return result
        with patch.object(self.ledger, "list_commands", side_effect=listed):
            self.assertTrue(self.service.await_stop_proof())
        self.assertEqual(self.service._stop_proof_epoch, -1)

    def test_status_returns_blocked_diagnostic_when_list_future_fails(self):
        failed = Future()
        failed.set_exception(LedgerFault("mock ledger read failure"))
        with patch.object(self.ledger, "list_commands", return_value=failed):
            status = self.service.status()

        self.assertTrue(status["blocked"])
        self.assertEqual(status["error"],
                         "action status unavailable: LedgerFault: mock ledger read failure")
        self.assertEqual(status["unresolved"], [])
        self.assertEqual(status["control_mode"], "legacy")
        self.assertFalse(status["gate_open"])
        self.assertEqual(status["faults"], [])
        self.assertEqual(status["catalog_revision"], self.service.catalog.snapshot().revision)
        self.assertEqual(status["environment_revision"], 1)
        self.assertEqual(status["config_revision"], 0)

    def test_missing_running_record_is_not_stop_proof(self):
        self.assert_missing_record_is_unproven(())

    def test_missing_running_record_retains_resource_uncertainty(self):
        self.assert_missing_record_is_unproven(("test-resource",))

    def assert_missing_record_is_unproven(self, resources):
        row = self.running_command(resources)
        missing = Future()
        missing.set_result(None)
        with patch.object(self.ledger, "get", return_value=missing) as get:
            self.assertFalse(self.service.await_stop_proof("test stop", binding=self.binding))
            get.assert_called_with(row.command_id)
            status = self.service.status()

        self.assertTrue(status["blocked"])
        self.assertIn("stop proof unavailable:", status["error"])
        self.assertIn("missing", status["error"])
        self.assertIn(row.command_id, status["error"])
        self.assertEqual(status["unresolved"], [{
            "command_id": row.command_id, "owner": row.owner, "generation": row.generation,
            "status": ActionStatus.RUNNING, "held_resources": list(resources),
        }])
        current = self.ledger.get(row.command_id).result(timeout=1)
        self.assertEqual(current.status, ActionStatus.RUNNING)
        self.assertEqual(current.held_resources, resources)
        self.assertIsNone(self.ledger.stop_proof(row.command_id, self.binding).result(timeout=1))


if __name__ == "__main__":
    unittest.main()
