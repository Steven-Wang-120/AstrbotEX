"""Owned-process tests use fake health and short children, never model weights."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from astrbot_ex.core.decision.owned_laya import Deployment, OwnedLayaError, OwnedLayaService


class FakeProcess:
    _next_pid = 80000

    def __init__(self, *, ignore_term=False):
        type(self)._next_pid += 1
        self.pid = self._next_pid
        self.returncode = None
        self.ignore_term = ignore_term
        self.terminated = self.killed = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        if not self.ignore_term:
            self.returncode = -15

    def kill(self):
        self.killed += 1
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake-owned", timeout)
        return self.returncode


class FakeBackend:
    def __init__(self, config=None):
        self.config = config
        self.busy = self.restart = self.closed = False
        self.cancelled = threading.Event()
        self.last_record = None

    def probe(self):
        return {"ok": True, "health": {"status": "ok", "fixture": True}}

    def status(self):
        return {"busy": self.busy, "restart_required": self.restart, "error_code": "deadline_exceeded" if self.restart else None}

    def cancel(self):
        self.cancelled.set()

    def close(self):
        self.closed = True
        self.cancelled.set()


class OwnedLayaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.deployment = Deployment(Path(sys.executable), root / "cache", root / "logs", port=port,
                                     device="cpu", state_path=root / "execution/laya/service-state.json",
                                     terminate_timeout_s=.2, kill_timeout_s=.2)
        self.processes, self.services = [], []

    def tearDown(self):
        for service in reversed(self.services):
            if not service.status()["ownership_unknown"]:
                try:
                    service.stop()
                except OwnedLayaError:
                    pass
        self.temporary.cleanup()

    def manager(self, *, process_factory=None, warmup=None):
        def create(*args, **kwargs):
            child = FakeProcess()
            self.processes.append(child)
            return child
        service = OwnedLayaService(self.deployment, process_factory=process_factory or create,
                                   probe_factory=FakeBackend, warmup=warmup or (lambda backend: {"fixture": True}))
        self.services.append(service)
        return service

    def assert_code(self, code, callback):
        with self.assertRaises(OwnedLayaError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_ready_warmup_stop_and_fixed_environment(self):
        captured = {}
        def create(command, **kwargs):
            captured.update(command=command, **kwargs)
            child = FakeProcess()
            self.processes.append(child)
            return child
        service = self.manager(process_factory=create)
        config = service.start()
        self.assertEqual(config.port, self.deployment.port)
        self.assertEqual(service.status()["state"], "ready")
        self.assertIn("warmup", service.history[-1])
        self.assertEqual(captured["command"], [sys.executable, "-m", "laya.serve"])
        self.assertEqual(captured["env"]["LAYA_HOST"], "127.0.0.1")
        self.assertEqual(captured["env"]["LAYA_MAX_CONCURRENT"], "1")
        self.assertNotIn("LAYA_API_KEY", captured["env"])
        self.assertEqual(service.stop()["exit_confirmed"], True)
        self.assertEqual(service.status()["state"], "stopped")

    def test_history_is_copied_and_state_mode_restricted(self):
        service = self.manager()
        service.start(warmup=False)
        copied = service.history
        copied[0]["pid"] = -1
        self.assertGreater(service.history[0]["pid"], 0)
        self.assertEqual(self.deployment.state_path.stat().st_mode & 0o777, 0o600)

    def test_owned_handle_access_has_no_pid_adoption_and_checks_generation(self):
        service = self.manager()
        self.assert_code("service_not_owned", service.owned_process_handle)
        service.start()
        self.assertIs(service.owned_process_handle(expected_generation=service.generation), self.processes[-1])
        self.assert_code("stale_service_generation", lambda: service.owned_process_handle(expected_generation="old"))

    def test_generation_quarantine_blocks_other_client_and_requires_recovery(self):
        service = self.manager()
        service.start()
        generation = service.generation
        backend = FakeBackend()
        with service.decision_guard(generation, backend):
            backend.restart = True
        self.assertEqual(service.status()["state"], "restart_required")
        self.assert_code("restart_required", lambda: service.guard(generation))
        service.stop()
        self.assert_code("restart_required", service.start)
        service.start(recovery=True)
        self.assertNotEqual(generation, service.generation)
        self.assert_code("stale_service_generation", lambda: service.guard(generation))
        service.guard(service.generation)

    def test_old_closed_worker_retains_permit_until_it_exits(self):
        service = self.manager()
        service.start()
        old = FakeBackend()
        with service.decision_guard(service.generation, old):
            old.busy = True
        old.close()
        self.assertFalse(service.requests_idle())
        self.assert_code("busy", lambda: service.guard(service.generation))
        service.stop()
        self.assert_code("old_requests_pending", lambda: service.start(recovery=True))
        old.busy = False
        self.assertTrue(service.requests_idle())
        service.start(recovery=True)

    def test_unknown_persisted_ownership_cannot_be_claimed_or_killed(self):
        first = self.manager()
        first.start()
        other = self.manager()
        self.assertTrue(other.status()["ownership_unknown"])
        self.assertFalse(other.status()["owned"])
        self.assert_code("ownership_unknown", other.stop)
        self.assert_code("ownership_unknown", lambda: other.start(recovery=True))
        self.assertEqual(self.processes[0].terminated, 0)

    def test_confirmed_exit_and_quarantine_survive_new_manager(self):
        first = self.manager()
        first.start()
        generation = first.generation
        first.quarantine(generation, "deadline_exceeded")
        first.stop()
        other = self.manager()
        self.assertTrue(other.status()["restart_required"])
        self.assertFalse(other.status()["ownership_unknown"])
        self.assert_code("restart_required", other.start)
        other.start(recovery=True)
        self.assertNotEqual(generation, other.generation)

    def test_old_generation_fault_and_stop_cannot_change_new_service(self):
        service = self.manager()
        service.start()
        old = service.generation
        service.stop()
        service.start()
        service.quarantine(old, "late_old_result")
        self.assertEqual(service.status()["state"], "ready")
        self.assert_code("stale_service_generation", lambda: service.stop(expected_generation=old))
        self.assertEqual(service.status()["state"], "ready")
        self.assertIsNone(self.processes[-1].poll())

    def test_port_occupied_is_not_terminated(self):
        service = self.manager()
        with socket.socket() as occupant:
            occupant.bind(("127.0.0.1", self.deployment.port))
            occupant.listen()
            self.assert_code("service_port_occupied", service.start)
        self.assertEqual(self.processes, [])

    def test_term_then_kill_only_the_owned_handle(self):
        child = FakeProcess(ignore_term=True)
        service = self.manager(process_factory=lambda *args, **kwargs: child)
        service.start()
        stopped = service.stop()
        self.assertEqual((child.terminated, child.killed), (1, 1))
        self.assertEqual(stopped["exit_code"], -9)

    def test_interrupt_warmup_invalidates_start_and_confirms_child_exit(self):
        entered = threading.Event()
        errors = []
        def warmup(backend):
            entered.set()
            self.assertTrue(backend.cancelled.wait(2))
            return {"late": True}
        service = self.manager(warmup=warmup)
        def start():
            try:
                service.start()
            except OwnedLayaError as exc:
                errors.append(exc.code)
        worker = threading.Thread(target=start)
        worker.start()
        self.assertTrue(entered.wait(2))
        service.interrupt()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ["superseded"])
        self.assertEqual(service.status()["state"], "stopped")
        self.assertIsNotNone(self.processes[0].poll())

    def test_superseded_before_spawn_has_no_side_effect(self):
        service = self.manager()
        cancellation = threading.Event()
        cancellation.set()
        self.assert_code("superseded", lambda: service.start(cancel=cancellation))
        self.assert_code("superseded", lambda: service.start(is_current=lambda: False))
        self.assertEqual(self.processes, [])

    def test_warmup_failure_cleans_own_child_and_keeps_fault(self):
        def warmup(backend):
            backend.restart = True
            raise OwnedLayaError("deadline_exceeded")
        service = self.manager(warmup=warmup)
        self.assert_code("deadline_exceeded", service.start)
        self.assertEqual(service.status()["state"], "restart_required")
        self.assertIsNotNone(self.processes[0].poll())
        self.assertIsNotNone(service.history[-1]["exit_confirmed_monotonic_ns"])

    def test_short_owned_child_has_confirmed_exit(self):
        def child(command, **kwargs):
            return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        service = self.manager(process_factory=child)
        service.start(warmup=False)
        self.assertTrue(service.status()["owned"])
        self.assertTrue(service.stop()["exit_confirmed"])

    def test_failed_warmup_worker_must_drain_before_restart(self):
        captured = []
        def warmup(backend):
            captured.append(backend)
            backend.busy = backend.restart = True
            raise OwnedLayaError("deadline_exceeded")
        service = self.manager(warmup=warmup)
        self.assert_code("deadline_exceeded", service.start)
        self.assertTrue(captured[0].closed)
        self.assertFalse(service.requests_idle())
        self.assert_code("old_requests_pending", lambda: service.start(recovery=True))
        captured[0].busy = False
        self.assertTrue(service.requests_idle())

    def test_stop_before_warmup_skips_the_inference_callback(self):
        called = []
        child = FakeProcess()
        service = None
        def backend_factory(config):
            if config.deadline_ms == self.deployment.warmup_deadline_ms:
                service.interrupt("test_stop_before_inference")
            return FakeBackend(config)
        service = OwnedLayaService(self.deployment, process_factory=lambda *args, **kwargs: child,
                                   probe_factory=backend_factory, warmup=lambda backend: called.append(True))
        self.services.append(service)
        self.assert_code("superseded", service.start)
        self.assertEqual(called, [])
        self.assertIsNotNone(child.poll())


if __name__ == "__main__":
    unittest.main()
