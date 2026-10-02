"""Application-side I/O faults must revoke authority before the next worker step."""
from concurrent.futures import TimeoutError as FutureTimeout
import threading
import unittest
from unittest.mock import patch

from astrbot_ex.core.actions.dispatcher import DispatcherBusy
from astrbot_ex.core.actions.ledger import LedgerBusy, LedgerClosed, LedgerFault, OwnerBinding, StopEvidence
from tests import test_decision_service as fixtures


class ApplyIOTests(fixtures.DecisionServiceTests):
    # Reuse the owner/ledger lifecycle without collecting the inherited suite.
    def _application_failure(self, error, *, ledger_read=True):
        service = self.create(resource="shared", stop_proof=False)
        self.submit()
        self.assertTrue(self.plugins["arm"].started.wait(1))
        cid = self.plugins["arm"].commands[0].command_id
        self.assertTrue(fixtures.wait_for(lambda: self.ledger.get(cid).result(1).status == "accepted"))
        self.dispatcher.report(cid, OwnerBinding("arm", 1), "running").result(1)
        service.backend.kind = "keep"
        original_apply, original_record = service._apply, service._record
        published, release = threading.Event(), threading.Event()
        injected, fault_state = [False], []

        def apply(snapshot, decision):
            if injected[0]:
                return original_apply(snapshot, decision)
            injected[0] = True
            if not ledger_read:
                raise error
            with patch.object(service, "_poll_rows", side_effect=error):
                return original_apply(snapshot, decision)

        def record(snapshot, outcome, reason="", **details):
            original_record(snapshot, outcome, reason, **details)
            if reason == str(error):
                with service._condition, self.dispatcher._lock:
                    fault_state.append((service.goals.phase, self.dispatcher._gate,
                                        self.dispatcher._context, service._stop_pending))
                published.set()
                if not release.wait(2):
                    raise TimeoutError("application failure publication latch")

        try:
            with patch.object(service, "_apply", side_effect=apply), \
                    patch.object(service, "_record", side_effect=record):
                service.tick()
                self.assertTrue(published.wait(1))
                # The control worker is held after publishing the failure. A
                # later lease timeout cannot make these assertions pass.
                self.assertEqual(fault_state, [("blocked", False, None, True)])
                release.set()
                self.assertTrue(fixtures.wait_for(lambda: self.plugins["arm"].cancels))
                self.assertEqual(len(self.plugins["arm"].commands), 1)
                self.assertEqual(self.ledger.get(cid).result(1).held_resources, ("shared",))
                self.assertIsNone(self.ledger.stop_proof(cid, OwnerBinding("arm", 1)).result(1))
        finally:
            release.set()
            self.actions.stop_actions("fixture shutdown")
            self.dispatcher.reconcile_stop(cid, OwnerBinding("arm", 1),
                StopEvidence(cid, True, "fixture", "parked")).result(1)

    def test_application_poll_oserror_revokes_running_owner_immediately(self):
        self._application_failure(OSError("application observation read failed"))

    def test_application_poll_capacity_fault_revokes_running_owner(self):
        self._application_failure(RuntimeError("ledger_scan_capacity"))

    def test_application_dispatcher_busy_revokes_running_owner(self):
        self._application_failure(DispatcherBusy("application execution I/O failed"), ledger_read=False)

    def test_application_execution_oserror_revokes_running_owner(self):
        self._application_failure(OSError("application execution I/O failed"), ledger_read=False)

    def test_application_execution_timeout_revokes_running_owner(self):
        self._application_failure(FutureTimeout("application execution I/O failed"), ledger_read=False)

    def test_application_ledger_busy_revokes_running_owner(self):
        self._application_failure(LedgerBusy("application execution I/O failed"), ledger_read=False)

    def test_application_ledger_closed_revokes_running_owner(self):
        self._application_failure(LedgerClosed("application execution I/O failed"), ledger_read=False)

    def test_application_ledger_fault_revokes_running_owner(self):
        self._application_failure(LedgerFault("application execution I/O failed"), ledger_read=False)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ApplyIOTests(name) for name in ApplyIOTests.__dict__
                              if name.startswith("test_"))


if __name__ == "__main__":
    unittest.main()
