from __future__ import annotations

import inspect
import sys
import threading
import unittest
from unittest.mock import patch

from tests import test_action_dispatcher as fixtures


class DispatcherWatchdogTests(unittest.TestCase):
    def setUp(self):
        # Keep the shared TestCase inside the module, not in this module's loader.
        self.fixture = fixtures.DispatcherTests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)

    def test_stale_timeout_retry_cannot_reblock_reviewed_gate(self):
        captured, release, resumed = threading.Event(), threading.Event(), threading.Event()
        original_watch = fixtures.ActionDispatcher._watch
        source, first_line = inspect.getsourcelines(original_watch)
        retry_line = first_line + next(i for i, line in enumerate(source)
                                      if line.strip() == "for cid, reason in retry:")
        loop_line = first_line + next(i for i, line in enumerate(source)
                                     if line.strip() == "while not self._closed:")
        errors = []

        def trace(frame, event, arg):
            if frame.f_code is original_watch.__code__ and event == "line":
                if (frame.f_lineno == retry_line and not captured.is_set()
                        and any(cid == "stale-expired" for cid, _ in frame.f_locals["retry"])):
                    # Observation has finished and released _lock in both versions.
                    captured.set()
                    if not release.wait(5):
                        raise TimeoutError("captured watchdog retry latch")
                elif frame.f_lineno == loop_line and captured.is_set() and release.is_set():
                    resumed.set()
            return trace

        def watch(dispatcher, interval):
            sys.settrace(trace)
            try:
                original_watch(dispatcher, interval)
            except BaseException as exc:
                errors.append(exc)
                resumed.set()
            finally:
                sys.settrace(None)

        with patch.object(fixtures.ActionDispatcher, "_watch", watch):
            self.fixture.setUp()
        self.addCleanup(release.set)
        f = self.fixture
        plugin = f.owner(resource="joint", max_duration=30_000)
        f.enabled("arm")
        binding = fixtures.OwnerBinding("arm", 3)
        try:
            f.dispatcher.start(fixtures.command("arm", "stale-expired", lease=80)).result(3)
            f.wait_status("stale-expired", fixtures.ActionStatus.ACCEPTED)
            self.assertTrue(captured.wait(3))
            self.assertTrue(plugin.watchdog_parked.wait(2))
            # Real worker report detects the expired lease and commits timeout.
            report = f.dispatcher.report("stale-expired", binding, fixtures.ActionStatus.SUCCEEDED)
            with self.assertRaises(fixtures.ContractError):
                report.result(3)
            timed_out = f.dispatcher.query("stale-expired").result(3)
            self.assertEqual((timed_out.status, timed_out.held_resources),
                             (fixtures.ActionStatus.TIMED_OUT, ("joint",)))
            with self.assertRaisesRegex(RuntimeError, "stop proof"):
                f.dispatcher.review_stops().result(3)
            proof = fixtures.StopEvidence("stale-expired", True, "mock_controller",
                                          "software_watchdog_parked")
            f.dispatcher.reconcile_stop("stale-expired", binding, proof).result(3)
            self.assertEqual(f.ledger.stop_proof("stale-expired", binding).result(3), proof)
            f.dispatcher.review_stops().result(3)
            self.assertFalse(f.dispatcher.blocked)
            f.enabled("arm", ttl=30_000)
            epoch = f.dispatcher._epoch
            faults = f.dispatcher.faults
            self.assertNotIn("stale-expired", f.dispatcher._timeout_pending)
            release.set()
            self.assertTrue(resumed.wait(3))
            self.assertEqual(errors, [])
            self.assertFalse(f.dispatcher.blocked, "captured old timeout reblocked reviewed gate")
            self.assertTrue(f.dispatcher._gate)
            self.assertEqual(f.dispatcher._epoch, epoch)
            self.assertEqual(f.dispatcher.faults, faults)
            f.dispatcher.start(fixtures.command("arm", "fresh-after-review", lease=30_000)).result(3)
            f.wait_status("fresh-after-review", fixtures.ActionStatus.ACCEPTED)
            f.dispatcher.report("fresh-after-review", binding, fixtures.ActionStatus.SUCCEEDED).result(3)
        finally:
            release.set()

    def test_actual_expiry_closes_gate_before_worker_can_persist(self):
        self.fixture.setUp()
        f = self.fixture
        plugin = f.owner(resource="joint", max_duration=30_000)
        f.enabled("arm")
        entered, release, blocked = threading.Event(), threading.Event(), threading.Event()
        original_block = f.dispatcher._block

        def block(reason):
            original_block(reason)
            if threading.current_thread() is f.dispatcher._watchdog:
                blocked.set()

        def hold_worker():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("expiry persistence latch")

        f.dispatcher._block = block
        self.addCleanup(release.set)
        try:
            f.dispatcher.start(fixtures.command("arm", "real-expired", lease=180)).result(3)
            f.wait_status("real-expired", fixtures.ActionStatus.ACCEPTED)
            held = f.dispatcher._enqueue(hold_worker, priority=True)
            self.assertTrue(entered.wait(2))
            self.assertTrue(blocked.wait(3))
            self.assertTrue(f.dispatcher.blocked)
            self.assertFalse(f.dispatcher._gate)
            self.assertEqual(f.dispatcher.query("real-expired").result(3).status,
                             fixtures.ActionStatus.ACCEPTED)
            release.set()
            held.result(3)
            f.wait_status("real-expired", fixtures.ActionStatus.TIMED_OUT)
            self.assertTrue(plugin.watchdog_parked.wait(2))
            with self.assertRaisesRegex(RuntimeError, "stop proof"):
                f.dispatcher.review_stops().result(3)
        finally:
            release.set()
            f.dispatcher._block = original_block

    def test_unexpired_action_does_not_close_gate(self):
        self.fixture.setUp()
        f = self.fixture
        f.owner(resource="joint", max_duration=30_000)
        f.enabled("arm", ttl=30_000)
        f.dispatcher.start(fixtures.command("arm", "unexpired", lease=30_000)).result(3)
        f.wait_status("unexpired", fixtures.ActionStatus.ACCEPTED)
        self.assertFalse(f.dispatcher.blocked)
        self.assertTrue(f.dispatcher._gate)
        self.assertNotIn("unexpired", f.dispatcher._timeout_pending)
        f.dispatcher.report("unexpired", fixtures.OwnerBinding("arm", 3),
                            fixtures.ActionStatus.SUCCEEDED).result(3)
        self.assertFalse(f.dispatcher.blocked)
