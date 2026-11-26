"""Bounded real lifecycle callbacks and synthetic-only owned HTTP/launcher."""
import copy
import json
import socket
import sys
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from astrbot_ex.core.decision.config import ManagementError
from astrbot_ex.core.decision.owned_laya import Deployment, OwnedLayaService
from tests import test_decision_management_operations as operations
from tests.test_decision_management_http import ManagementHTTPFixture
from tests.test_laya_backend import health, response


class LifecyclePlugin:
    id = "lifecycle-fixture"
    def on_load(self):
        pass
    on_enable = on_load
    on_disable = on_load
    on_unload = on_load
    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.starts = self.stops = 0
        self.fail_stop = False
    def on_runtime_start(self):
        self.starts += 1
        self.entered.set()
        if not self.release.wait(2):
            raise RuntimeError("fixture start latch timeout")
    def on_runtime_stop(self, reason):
        self.stops += 1
        if self.fail_stop:
            raise RuntimeError("synthetic stop fault")


class RuntimeLifecycleTests(ManagementHTTPFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        saved = copy.deepcopy(self.get_config()["saved"])
        saved["backend"] = "laya"
        saved["laya"]["service_connection"]["base_url"] = "http://127.0.0.1:1"
        self.assertEqual(self.write("/config", {"config": saved})[0], 200)
        self.plugin = LifecyclePlugin()
        self.slot = self.server.controller.runtime.registry.register("trace", self.plugin)
    def tearDown(self):
        self.plugin.release.set()
        self.plugin.fail_stop = False
        super().tearDown()
    def begin(self, suffix, data=None):
        code, accepted, _ = self.write(suffix, data)
        self.assertEqual(code, 202, accepted)
        return accepted
    def test_stop_revokes_real_pending_registry_start_then_new_activation_survives(self):
        self.plugin.release.clear()
        old = self.begin("/mode", {"mode": "execute"})
        try:
            self.assertTrue(self.plugin.entered.wait(1))
            stop = self.begin("/stop")
            self.assertFalse(self.server.decision_service.status()["gate_open"])
            self.assertEqual(self.server.decision_service.mode, "disabled")
            self.assertFalse(self.plugin.release.is_set())
        finally:
            self.plugin.release.set()
        self.assertEqual(self.operation(old)["state"], "superseded")
        self.assertEqual(self.operation(stop)["state"], "succeeded")
        self.assertFalse(self.slot.runtime_started)
        self.assertGreaterEqual(self.plugin.stops, 1)
        new = self.begin("/mode", {"mode": "execute"})
        self.assertEqual(self.operation(new)["state"], "succeeded")
        self.assertEqual(self.server.controller.runtime.state.value, "running")
        self.assertTrue(self.slot.runtime_started)
        self.assertEqual(self.plugin.starts, 2)
        self.assertEqual(self.operation(self.begin("/stop"))["state"], "succeeded")
    def test_new_activation_queued_during_old_start_survives_old_cleanup(self):
        self.plugin.release.clear()
        old = self.begin("/mode", {"mode": "execute"})
        try:
            self.assertTrue(self.plugin.entered.wait(1))
            new = self.begin("/mode", {"mode": "execute"})
        finally:
            self.plugin.release.set()
        self.assertEqual(self.operation(old)["state"], "superseded")
        self.assertEqual(self.operation(new)["state"], "succeeded")
        self.assertEqual(self.server.controller.runtime.state.value, "running")
        self.assertTrue(self.slot.runtime_started)
        self.assertEqual(self.plugin.starts, 2)
        self.assertEqual(self.operation(self.begin("/stop"))["state"], "succeeded")

    def test_parked_shadow_preserves_submission_gate_context(self):
        management = self.server.decision_management
        saved = copy.deepcopy(self.get_config()["saved"])
        saved["laya"].update(enabled=True, allow_live_http=True)
        self.assertEqual(self.write("/config", {"config": saved})[0], 200)
        lock = management._apply_serial
        lock.acquire()
        try:
            old = self.begin("/mode", {"mode": "shadow"})
            self.server.decision_service.management_stop("external-framework-stop")
            from tests.test_decision_service import wait_for
            self.assertTrue(wait_for(lambda: self.server.decision_service.status()["stop"]["state"] == "proven"))
        finally:
            lock.release()
        operation = self.operation(old)
        self.assertEqual(operation["state"], "failed", operation)
        self.assertEqual(operation["error_code"], "backend_switch_superseded")
        self.assertIsNone(self.get_config()["effective"])
        self.assertEqual(self.server.decision_service.status()["backend"]["type"], "MockBackend")
        self.assert_idle()

    def test_config_http_save_waits_for_apply_serial_instead_of_invalidating_start(self):
        self.plugin.release.clear()
        before = self.get_config()
        old = self.begin("/mode", {"mode": "execute"})
        submitted, done = threading.Event(), threading.Event()
        results = []
        def save():
            submitted.set()
            results.append(self.write("/config", {"config": before["saved"]}, version=before))
            done.set()
        thread = threading.Thread(target=save)
        try:
            self.assertTrue(self.plugin.entered.wait(1))
            thread.start()
            self.assertTrue(submitted.wait(1))
            self.assertFalse(done.wait(.05))
            self.assertEqual(self.get_config()["revision"], before["revision"])
        finally:
            self.plugin.release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.operation(old)["state"], "succeeded")
        self.assertEqual(results[0][0], 409)  # Execute is not an idle config-save boundary.
        self.assertEqual(self.get_config()["revision"], before["revision"])
        self.assertEqual(self.operation(self.begin("/stop"))["state"], "succeeded")
    def test_external_invalidate_reclaims_stalled_runtime_without_new_stop(self):
        self.plugin.release.clear()
        old = self.begin("/mode", {"mode": "execute"})
        try:
            self.assertTrue(self.plugin.entered.wait(1))
            self.server.decision_management.invalidate("connection_replaced")
        finally:
            self.plugin.release.set()
        self.assertEqual(self.operation(old)["state"], "superseded")
        self.assert_idle()
        self.assertFalse(self.slot.runtime_started)
        self.assertEqual(self.operation(self.begin("/mode", {"mode": "execute"}))["state"], "succeeded")
        self.assertEqual(self.operation(self.begin("/stop"))["state"], "succeeded")
    def test_session_change_during_real_start_reclaims_old_runtime(self):
        self.plugin.release.clear()
        old = self.begin("/mode", {"mode": "execute"})
        management = self.server.decision_management
        original = management.store.ex_session
        try:
            self.assertTrue(self.plugin.entered.wait(1))
            management.store.ex_session = "synthetic-new-session"
            management.invalidate("new_session")
        finally:
            self.plugin.release.set()
        try:
            self.assertEqual(self.operation(old)["state"], "superseded")
            self.assert_idle()
            self.assertFalse(self.slot.runtime_started)
        finally:
            management.store.ex_session = original

    def test_motion_stop_fault_is_uncertain_even_with_idle_runtime(self):
        class Motion(LifecyclePlugin):
            id = "motion-stop-fixture"
            def stop(this, reason):
                if this.fail_stop:
                    raise RuntimeError("synthetic motion stop fault")
        motion = Motion()
        self.server.controller.runtime.registry.register("motion", motion)
        self.assertEqual(self.operation(self.begin("/mode", {"mode": "execute"}))["state"], "succeeded")
        motion.fail_stop = True
        try:
            operation = self.operation(self.begin("/stop"))
            self.assertEqual(operation["state"], "failed", operation)
            self.assertEqual(operation["error_code"], "runtime_stop_not_proven")
            self.assertEqual(self.server.controller.runtime.state.value, "idle")
            view = self.request("/api/v1/ex/decision/view")[1]
            self.assertEqual(view["ex"]["state"], "uncertain")
            self.assertFalse(view["ex"]["can_start"])
        finally:
            motion.fail_stop = False
        self.assertEqual(self.operation(self.begin("/stop"))["state"], "succeeded")

    def test_runtime_fault_view_blocks_retry_until_actual_stop(self):
        self.assertEqual(self.operation(self.begin("/mode", {"mode": "execute"}))["state"], "succeeded")
        self.server.controller.fail("synthetic-runtime-fault")
        view = self.request("/api/v1/ex/decision/view")[1]
        self.assertEqual(view["ex"]["state"], "failed")
        self.assertEqual(view["error"]["code"], "runtime_fault")
        self.assertFalse(view["ex"]["can_start"])
        self.assertEqual(self.operation(self.begin("/stop"))["state"], "succeeded")

    def test_swallowed_registry_stop_fault_is_uncertain_not_full_stop(self):
        self.assertEqual(self.operation(self.begin("/mode", {"mode": "execute"}))["state"], "succeeded")
        self.plugin.fail_stop = True
        operation = self.operation(self.begin("/stop"))
        self.assertEqual(operation["state"], "failed", operation)
        self.assertEqual(operation["error_code"], "runtime_stop_not_proven")
        self.assertEqual(self.server.controller.runtime.state.value, "idle")
        self.assertEqual(self.slot.state, "blocked")
        self.assertTrue(self.slot.stop_error)
        view = self.request("/api/v1/ex/decision/view")[1]
        self.assertEqual(view["ex"]["state"], "uncertain")
        self.assertFalse(view["ex"]["can_start"])
        self.assertTrue(view["ex"]["can_stop"])
        self.plugin.fail_stop = False
        self.assertEqual(self.operation(self.begin("/stop"))["state"], "succeeded")
        self.assertFalse(self.slot.runtime_started)


class OwnedRecoveryLockTests(operations.DecisionManagementOperationsTests):
    def test_recover_superseded_at_apply_wait_does_not_invert_locks_or_kill_new_generation(self):
        for suffix, data in (("/mode", {"mode": "execute"}), ("/stop", None)):
            with self.subTest(suffix=suffix):
                self.load_release.set()
                old_manager = self.management.laya
                if old_manager.status()["owned"]:
                    old_manager.stop(expected_generation=old_manager.generation)
                old_manager.start(warmup=False)
                old_manager.quarantine(old_manager.generation, "deadline_exceeded")
                apply_wait, release_recover = threading.Event(), threading.Event()
                lock = self.management._apply_serial
                calls = []
                class ObservedLock:
                    def __enter__(this):
                        calls.append(threading.get_ident())
                        if len(calls) == 1:
                            apply_wait.set()
                            if not release_recover.wait(3):
                                raise RuntimeError("recover apply latch timeout")
                        lock.acquire()
                        return this
                    def __exit__(this, *args):
                        lock.release()
                try:
                    with patch.object(self.management, "_apply_serial", ObservedLock()):
                        code, recover, _ = self.write("/service/recover")
                        self.assertEqual(code, 202)
                        self.assertTrue(apply_wait.wait(2))
                        recovered_generation = old_manager.generation
                        code, latest, _ = self.write(suffix, data)
                        self.assertEqual(code, 202)
                        # New authority must finish while recovery is parked before apply.
                        self.assertEqual(self.finished(latest)["state"], "succeeded")
                        release_recover.set()
                        self.assertEqual(self.finished(recover)["state"], "superseded")
                finally:
                    release_recover.set()
                self.assertIsNotNone(self.processes[-2].poll() if suffix == "/mode" else self.processes[-1].poll())
                if suffix == "/mode":
                    self.assertNotEqual(old_manager.generation, recovered_generation)
                    self.assertIsNone(self.processes[-1].poll())
                    self.assertEqual(self.server.controller.runtime.state.value, "running")
                    self.assertEqual(self.finished(self.write("/stop")[1])["state"], "succeeded")


class OwnedCredentialTests(ManagementHTTPFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root.joinpath("cache").mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.server.decision_management.store.port = port
        self.http = []
        self.children = []
        self.calls = []
        saved = copy.deepcopy(self.get_config()["saved"])
        saved["backend"] = "laya"
        saved["laya"]["service_connection"] = {"mode": "owned", "base_url": "http://127.0.0.1:" + str(port),
            "model": "typed-decisions", "auth_mode": "bearer"}
        saved["laya"]["deployment"] = {"launcher": "subprocess", "python": sys.executable,
                                         "cache": str(self.root / "cache"), "device": "cpu"}
        self.assertEqual(self.write("/config", {"config": saved})[0], 200)
        outer = self
        def launcher(command, **kwargs):
            marker = kwargs["env"]["LAYA_API_KEY"]
            outer.calls.append((command, kwargs["env"]))
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def send(self, value):
                    outer.http.append(self.headers.get("Authorization"))
                    body = json.dumps(value).encode()
                    self.send_response(200 if self.headers.get("Authorization") == "Bearer " + marker else 401)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                def do_GET(self):
                    self.send(health())
                def do_POST(self):
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    self.send(response(request["questions"]))
            supplier = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            supplier.daemon_threads = True
            thread = threading.Thread(target=supplier.serve_forever, kwargs={"poll_interval": .01})
            thread.start()
            class Child:
                pid = 97000 + len(outer.children)
                code = None
                def poll(this):
                    return this.code
                def terminate(this):
                    if this.code is None:
                        supplier.shutdown()
                        supplier.server_close()
                        thread.join(2)
                        this.code = -15
                kill = terminate
                def wait(this, timeout=None):
                    return this.code
            child = Child()
            outer.children.append(child)
            return child
        self.launch_patch = patch("astrbot_ex.core.decision.owned_laya.subprocess.Popen", side_effect=launcher)
        self.launch_patch.start()
    def tearDown(self):
        try:
            for child in self.children:
                child.terminate()
            super().tearDown()
        finally:
            self.launch_patch.stop()
    def secret(self, value):
        self.assertEqual(self.write("/secret", {"provider": "laya", "action": "set", "value": value})[0], 200)
    def start_stop(self):
        code, accepted, _ = self.write("/mode", {"mode": "execute"})
        self.assertEqual(code, 202)
        result = self.operation(accepted)
        self.assertEqual(result["state"], "succeeded", result)
        manager = self.server.decision_management.laya
        code, stopped, _ = self.write("/stop")
        self.assertEqual(code, 202)
        self.assertEqual(self.operation(stopped)["state"], "succeeded")
        return manager
    def test_restart_same_config_reinstalls_owned_generation_without_key_change(self):
        self.secret("synthetic-stable-key")
        old = self.start_stop()
        generation = old.generation
        revision = self.get_config()["revision"]
        code, accepted, _ = self.write("/mode", {"mode": "execute"})
        self.assertEqual(code, 202)
        self.assertEqual(self.operation(accepted)["state"], "succeeded")
        self.assertNotEqual(old.generation, generation)
        self.assertEqual(self.server.decision_service.backend.generation, old.generation)
        self.assertEqual(self.get_config()["revision"], revision)
        code, stopped, _ = self.write("/stop")
        self.assertEqual(code, 202)
        self.assertEqual(self.operation(stopped)["state"], "succeeded")

    def test_key_rotation_after_exit_recreates_manager_and_uses_new_key_for_launcher_and_http(self):
        self.secret("synthetic-old-key")
        old_ref = self.get_config()["saved"]["laya_secret_ref"]
        old = self.start_stop()
        self.assertIsNotNone(old.history[-1]["exit_confirmed_monotonic_ns"])
        self.secret("synthetic-new-key")
        self.assertFalse((self.root / "secrets" / (old_ref + ".secret")).exists())
        new = self.start_stop()
        self.assertIsNot(old, new)
        self.assertEqual([env["LAYA_API_KEY"] for _, env in self.calls], ["synthetic-old-key", "synthetic-new-key"])
        self.assertIn("Bearer synthetic-new-key", self.http)
        self.assertTrue(all(child.poll() is not None for child in self.children))
        self.assertIsNone(self.server.decision_service.goals.active)
        self.assertEqual(self.server.action_ledger.list_commands().result(1), ())
    def test_unknown_ownership_cannot_be_replaced_by_key_rotation_or_stop_claim(self):
        self.secret("synthetic-unknown-key")
        management = self.server.decision_management
        manager = management.laya
        manager._ownership_unknown = True
        self.secret("synthetic-next-key")
        with self.assertRaises(ManagementError):
            management.laya
        self.assertIs(management._laya, manager)
        code, accepted, _ = self.write("/stop")
        self.assertEqual(code, 202)
        self.assertEqual(self.operation(accepted)["state"], "blocked")
        manager._ownership_unknown = False  # Fixture teardown only, not a product recovery path.

    def test_quarantined_key_change_cannot_replace_manager_even_after_exit(self):
        self.secret("synthetic-quarantine-key")
        management = self.server.decision_management
        manager = management.laya
        manager.start()
        generation = manager.generation
        manager.quarantine(generation, "deadline_exceeded")
        manager.stop(expected_generation=generation)
        self.secret("synthetic-rotated-key")
        with self.assertRaises(ManagementError):
            management.laya
        self.assertIs(management._laya, manager)
        view = self.request("/api/v1/ex/decision/view")[1]
        self.assertEqual(view["ex"]["state"], "failed")
        self.assertFalse(view["ex"]["can_start"])

    def test_live_key_change_requires_stop_and_preserves_captured_credential(self):
        self.secret("synthetic-live-key")
        management = self.server.decision_management
        manager = management.laya
        manager.start()
        generation = manager.generation
        self.secret("synthetic-next-key")
        with self.assertRaises(ManagementError) as error:
            management.laya
        self.assertEqual(error.exception.code, "owned_deployment_stop_required")
        self.assertIs(management._laya, manager)
        self.assertEqual(manager.generation, generation)
        self.assertEqual(manager._environment()["LAYA_API_KEY"], "synthetic-live-key")
        manager.stop(expected_generation=generation)
        self.assertIsNot(management.laya, manager)
