"""Owned-process tests use fake health and short children, never model weights."""
from __future__ import annotations

import errno
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

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

    def health_probe(self):
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
        (root / "cache").mkdir()
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
        if sys.platform != "win32":
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

    def test_foreign_listeners_still_block_launch(self):
        for host in ("127.0.0.1", "0.0.0.0"):
            with self.subTest(host=host), socket.socket() as occupant:
                # Windows reusable wildcard listeners permit specific-address binds.
                option = socket.SO_EXCLUSIVEADDRUSE if os.name == "nt" and host == "0.0.0.0" else socket.SO_REUSEADDR
                occupant.setsockopt(socket.SOL_SOCKET, option, 1)
                occupant.bind((host, 0))
                occupant.listen()
                occupant.settimeout(2)
                service = OwnedLayaService(replace(self.deployment, port=occupant.getsockname()[1]),
                                           process_factory=lambda *a, **k: self.fail("spawn"))
                self.services.append(service)
                self.assert_code("service_port_occupied", service.start)
                self.assertIsNone(service.generation)
                self.assertFalse(service.status()["owned"])
                # A failed launch must leave the foreign listener usable.
                with socket.create_connection(("127.0.0.1", service.deployment.port), timeout=2):
                    accepted, _ = occupant.accept()
                    accepted.close()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux TCP TIME_WAIT and /proc proof")
    def test_clean_owned_exit_with_tcp_time_wait_allows_same_port_restart(self):
        children, controls = [], []
        child_source = """
import socket
import sys
with socket.socket() as listener, socket.socket() as control:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", int(sys.argv[1])))
    listener.listen()
    listener.settimeout(2)
    control.connect(("127.0.0.1", int(sys.argv[2])))
    control.sendall(b"ready")
    connection, _ = listener.accept()
    with connection:
        connection.settimeout(2)
        connection.sendall(b"owned")
        connection.shutdown(socket.SHUT_WR)
        while connection.recv(1024):
            pass
    listener.close()
    control.settimeout(2)
    assert control.recv(1) == b"x"
"""
        def create(command, **kwargs):
            with socket.socket() as ready:
                ready.bind(("127.0.0.1", 0))
                ready.listen()
                ready.settimeout(2)
                child = subprocess.Popen([sys.executable, "-c", child_source,
                                          str(self.deployment.port), str(ready.getsockname()[1])], **kwargs)
                children.append(child)
                control, _ = ready.accept()
                controls.append(control)
                control.settimeout(2)
                self.assertEqual(control.recv(5), b"ready")
                return child
        def warmup(backend):
            with socket.create_connection(("127.0.0.1", self.deployment.port), timeout=2) as client:
                self.assertEqual(client.recv(5), b"owned")
                self.assertEqual(client.recv(1), b"")  # Server sends FIN before the client closes.
            return {"fixture": True}
        service = self.manager(process_factory=create, warmup=warmup)
        try:
            service.start()
            generation = service.generation
            controls[-1].sendall(b"x")
            self.assertEqual(children[-1].wait(timeout=2), 0)
            self.assertTrue(service.stop()["exit_confirmed"])
            self.assertTrue(service.history[-1]["exit_confirmed_monotonic_ns"])
            port_hex = f"{self.deployment.port:04X}"
            states = [row.split()[3] for row in Path("/proc/net/tcp").read_text().splitlines()[1:]
                      if row.split()[1] == "0100007F:" + port_hex]
            self.assertIn("06", states, states)  # TCP_TIME_WAIT is not a listening socket.
            self.assertNotIn("0A", states, states)
            with socket.socket() as raw:
                with self.assertRaises(OSError) as caught:
                    raw.bind(("127.0.0.1", self.deployment.port))
                self.assertEqual(caught.exception.errno, errno.EADDRINUSE)
            with socket.socket() as reusable:
                reusable.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                reusable.bind(("127.0.0.1", self.deployment.port))
            service.start()
            self.assertNotEqual(service.generation, generation)
            self.assertEqual(service.status()["state"], "ready")
            controls[-1].sendall(b"x")
            self.assertEqual(children[-1].wait(timeout=2), 0)
            self.assertTrue(service.stop()["exit_confirmed"])
        finally:
            for control in controls:
                control.close()
            for child in children:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=2)

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

    def test_owned_preflight_errors_have_no_child_or_generation(self):
        for changes, code in (({"python": Path(self.temporary.name) / "missing.exe"}, "python_executable_missing"),
                              ({"python": Path("relative.exe")}, "python_executable_missing"),
                              ({"cache": Path(self.temporary.name) / "missing-cache"}, "model_cache_missing")):
            with self.subTest(changes=changes):
                service = OwnedLayaService(replace(self.deployment, **changes), process_factory=lambda *a, **k: self.fail("spawn"))
                self.services.append(service)
                self.assert_code(code, service.start)
                self.assertIsNone(service.generation)
                self.assertFalse(service.status()["owned"])
                self.assertEqual(service.status()["state"], "failed")
                self.assertIsNone(service._intent)

    def test_state_write_failure_before_spawn_keeps_old_metadata_and_closes_log(self):
        service = self.manager()
        service.start(warmup=False)
        service.stop()
        old = self.deployment.state_path.read_bytes()
        count = len(self.processes)
        with patch("os.replace", side_effect=OSError("fixture metadata failure")):
            self.assert_code("service_state_write_failed", lambda: service.start(warmup=False))
        self.assertEqual(len(self.processes), count)
        self.assertEqual(self.deployment.state_path.read_bytes(), old)
        self.assertIsNone(service._log)
        self.assertIsNone(service._intent)
        self.assertEqual(list(self.deployment.state_path.parent.glob(".decision-*")), [])

    def test_failure_after_spawn_stops_only_captured_child(self):
        service = self.manager()
        original = service._persist_locked
        calls = []
        def fail_once():
            calls.append(True)
            if len(calls) == 2:
                raise OwnedLayaError("service_state_write_failed")
            return original()
        with patch.object(service, "_persist_locked", side_effect=fail_once):
            self.assert_code("service_state_write_failed", service.start)
        self.assertEqual(self.processes[0].terminated, 1)
        self.assertIsNotNone(self.processes[0].poll())
        self.assertIsNone(service._log)
        self.assertTrue(service.history[-1]["exit_confirmed_monotonic_ns"])

    def test_bearer_key_shared_with_startup_client_not_ambient_or_admin(self):
        captured, clients = {}, []
        deployment = replace(self.deployment, auth_mode="bearer")
        secret = "laya-owned-fixture-only"
        def create(command, **kwargs):
            captured.update(command=command, **kwargs)
            return FakeProcess()
        class ReadinessBackend(FakeBackend):
            def health_probe(self):
                return {"ok": True, "health": {"status": "ok"}}
            def probe(self):
                raise AssertionError("startup must use health only")
        def client(config, *, secret_provider):
            clients.append((config.auth_mode, secret_provider()))
            return ReadinessBackend(config)
        service = OwnedLayaService(deployment, process_factory=create, probe_factory=client,
                                   warmup=lambda backend: {"fixture": True}, secret_provider=lambda: secret)
        self.services.append(service)
        with patch.dict(os.environ, {"LAYA_API_KEY": "ambient-must-not-inherit", "LAYA_API_KEY_FILE": "ambient-file",
                                     "TYPESAFE_API_KEY": "jev-ambient-must-not-inherit"}):
            service.start()
        self.assertEqual(captured["env"]["LAYA_API_KEY"], secret)
        self.assertNotIn("LAYA_API_KEY_FILE", captured["env"])
        self.assertNotIn("TYPESAFE_API_KEY", captured["env"])
        self.assertIs(captured["shell"], False)
        self.assertEqual(clients, [("bearer", secret), ("bearer", secret)])
        self.assertNotIn(secret, json.dumps(service.status()))
        result = service.stop()
        self.assertEqual(result["stop_scope"], "owned_popen_handle")
        self.assertFalse(result["descendant_exit_confirmed"])
        self.assertNotIn(secret, self.deployment.state_path.read_text())

    def test_missing_bearer_key_does_not_spawn_or_inherit_ambient(self):
        service = OwnedLayaService(replace(self.deployment, auth_mode="bearer"),
                                   process_factory=lambda *a, **k: self.fail("spawn"), probe_factory=FakeBackend)
        self.services.append(service)
        with patch.dict(os.environ, {"LAYA_API_KEY": "ambient-must-not-inherit"}):
            self.assert_code("missing_or_invalid_secret", service.start)
        self.assertFalse(service.status()["owned"])
        self.assertIsNone(service.generation)

    def test_owned_default_warmup_is_fixed_wait_only_without_goal_actions(self):
        snapshots = []
        class WarmupBackend(FakeBackend):
            def decide(self, snapshot):
                snapshots.append(snapshot.to_dict())
                class Decision:
                    def to_dict(self):
                        return {"fixture": True}
                return Decision()
        service = OwnedLayaService(self.deployment, process_factory=lambda *a, **k: FakeProcess(),
                                   probe_factory=WarmupBackend)
        self.services.append(service)
        service.start()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["goal"]["allowed_actions"], [])
        self.assertEqual(snapshots[0]["observations"], [])
        self.assertEqual([c["kind"] for c in snapshots[0]["owners"][0]["candidates"]], ["wait"])

    def test_directory_rollback_uncertainty_before_spawn_survives_owned_restart(self):
        from tests.test_provider_config import directory_fault
        service = self.manager()
        with directory_fault(self.deployment.state_path.parent, rollback="sync"):
            self.assert_code("storage_write_uncertain", lambda: service.start(warmup=False))
        self.assertEqual(self.processes, [])
        self.assertIsNone(service._log)
        self.assertEqual(service.status()["state"], "restart_required")
        other = self.manager()
        self.assert_code("storage_write_uncertain", lambda: other.start(recovery=True))
        self.assert_code("storage_write_uncertain", lambda: service.start(recovery=True))

    def test_storage_recovery_marker_blocks_owned_start_even_with_recovery(self):
        first = self.manager()
        first.start(warmup=False)
        first.stop()
        path = self.deployment.state_path
        marker = path.parent / (".decision-" + path.name + "-rollback-fixture")
        marker.write_bytes(path.read_bytes())
        other = self.manager()
        self.assertEqual(other.status()["state"], "restart_required")
        self.assertEqual(other.status()["error_code"], "storage_write_uncertain")
        self.assert_code("storage_write_uncertain", other.start)
        self.assert_code("storage_write_uncertain", lambda: other.start(recovery=True))
        self.assert_code("storage_write_uncertain", lambda: other.guard(other.generation))
        self.assert_code("storage_write_uncertain", other._persist_locked)
        self.assertEqual(len(self.processes), 1)

    def test_storage_recovery_marker_without_metadata_also_blocks_owned_start(self):
        path = self.deployment.state_path
        path.parent.mkdir(parents=True)
        (path.parent / (".decision-" + path.name + "-rollback-fixture")).write_bytes(b"")
        service = self.manager()
        self.assert_code("storage_write_uncertain", lambda: service.start(recovery=True))
        self.assertEqual(self.processes, [])

    def test_startup_failure_with_unconfirmed_child_exit_is_quarantined(self):
        class StuckChild(FakeProcess):
            def kill(self):
                self.killed += 1
        child = StuckChild(ignore_term=True)
        service = self.manager(process_factory=lambda *a, **k: child,
                               warmup=lambda backend: (_ for _ in ()).throw(OwnedLayaError("fixture_start_failure")))
        self.assert_code("service_exit_not_confirmed", service.start)
        self.assertEqual(service.status()["state"], "restart_required")
        self.assertTrue(service.status()["restart_required"])
        self.assertIsNone(child.poll())
        self.assertNotIn("exit_confirmed_monotonic_ns", service.history[-1])
        child.returncode = -9  # Fixture finally permits cleanup; no real PID operation.

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
