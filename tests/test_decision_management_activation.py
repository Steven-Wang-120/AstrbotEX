"""External activation on real runtime wiring and fixture-only loopback HTTP."""
import copy
import json
import ssl
import threading
import unittest
from unittest.mock import patch

from astrbot_ex.core.decision.backends import jev, laya
from astrbot_ex.core.decision.backends.registry import create_backend
from astrbot_ex.core.decision.management import ManagementSettings
from tests.test_decision_management_http import ManagementHTTPFixture
from tests import test_decision_management_provider_http as provider_http
from tests.test_laya_backend import FixtureTransport


class DecisionManagementActivationTests(ManagementHTTPFixture, unittest.TestCase):
    supplier = provider_http.DecisionManagementProviderHTTPTests.supplier
    selection = provider_http.DecisionManagementProviderHTTPTests.selection

    def view(self):
        code, value, _ = self.request("/api/v1/ex/decision/view")
        self.assertEqual(code, 200)
        return value

    def mode(self, mode="execute"):
        code, accepted, _ = self.write("/mode", {"mode": mode})
        self.assertEqual(code, 202, accepted)
        return self.operation(accepted)

    def stop(self, version=None):
        code, accepted, _ = self.write("/stop", version=version)
        self.assertEqual(code, 202, accepted)
        operation = self.operation(accepted)
        self.assertEqual(operation["state"], "succeeded", operation)
        self.assert_idle()
        return operation

    def assert_no_work(self):
        service = self.server.decision_service
        self.assertIsNone(service.goals.active)
        self.assertIsNone(service.goals.pending_replace)
        self.assertEqual(self.server.action_ledger.list_commands().result(1), ())
        self.assertEqual(self.server.decision_management.history.page()["items"], [])

    def test_saved_secret_probe_execute_stop_production_transports_zero_goals_actions(self):
        management = self.server.decision_management
        with patch("astrbot_ex.core.decision.management.OwnedLayaService",
                   side_effect=AssertionError("external process ownership")):
            for provider, backend_type in (("jev", jev.JevBackend), ("laya", laya.LayaBackend)):
                with self.subTest(provider=provider):
                    url, calls = self.supplier(provider)
                    original = self.get_config()
                    saved = self.selection(provider, url)
                    self.assertEqual(self.write("/config", {"config": saved})[0], 200)
                    marker = provider + "-synthetic-activation-only"
                    code, _, _ = self.write("/secret", {"provider": provider, "action": "set", "value": marker})
                    self.assertEqual(code, 200)
                    before = self.get_config()
                    path = self.root / "profiles/default/decision.json"
                    disk = path.read_bytes()
                    self.assertEqual(before["effective"], original["effective"])
                    self.assert_idle()
                    self.assertEqual(calls, [])

                    code, accepted, _ = self.write("/test")
                    self.assertEqual(code, 202)
                    probe = self.operation(accepted)
                    self.assertEqual(probe["state"], "succeeded", probe)
                    self.assertEqual(probe["result"]["scope"], "fixed_wait_inference")
                    self.assertTrue(probe["result"]["inference_ok"])
                    self.assertTrue(probe["result"]["binding"]["current_config_verified"])
                    self.assertEqual(self.view()["connection"]["state"], "verified")
                    self.assertEqual(self.get_config(), before)
                    self.assert_idle()
                    self.assert_no_work()
                    self.assertEqual([call[0] for call in calls], ["POST"] if provider == "jev" else ["GET", "POST"])
                    for _, _, authorization, body in calls:
                        self.assertEqual(authorization, "Bearer " + marker)
                        if body is not None:
                            self.assertEqual(len(body["questions"]), 1)
                            question = next(iter(body["questions"].values()))
                            self.assertEqual(len(question["criteria"]), 1)
                            self.assertIn("wait", json.dumps(question).lower())
                    probe_calls = copy.deepcopy(calls)

                    operation = self.mode()
                    self.assertEqual(operation["state"], "succeeded", operation)
                    backend = self.server.decision_service.backend
                    self.assertIsInstance(backend, backend_type)
                    self.assertEqual(backend.execution_capability["transport"], "live")
                    self.assertTrue(backend.execution_allowed)
                    self.assertEqual(self.server.controller.runtime.state.value, "running")
                    status = self.server.decision_service.status()
                    self.assertEqual(status["control_mode"], "decision")
                    self.assertEqual(status["mode"], "execute")
                    self.assertEqual(self.view()["ex"]["state"], "running")
                    current = self.get_config()
                    self.assertEqual(current["saved"], before["saved"])
                    self.assertEqual(current["revision"], before["revision"])
                    self.assertEqual(current["ex_session"], before["ex_session"])
                    self.assertEqual(current["effective_revision"], before["revision"])
                    effective = current["effective"][provider]
                    self.assertTrue(effective["allow_live_http"])
                    if provider == "jev":
                        self.assertEqual(effective["mode"], "execute")
                        self.assertEqual(before["saved"][provider]["mode"], "shadow")
                    else:
                        self.assertTrue(effective["enabled"])
                        self.assertTrue(effective["execution_enabled"])
                        self.assertFalse(before["saved"][provider]["enabled"])
                        self.assertFalse(before["saved"][provider]["execution_enabled"])
                    self.assertFalse(before["saved"][provider]["allow_live_http"])
                    self.assertEqual(path.read_bytes(), disk)
                    self.assertEqual(self.request("/api/v1/ex/decision/status")[1]["service"],
                                     {"mode": "external", "managed": False})
                    for suffix in ("/service/start", "/service/stop", "/service/recover"):
                        code, value, _ = self.write(suffix)
                        self.assertEqual(code, 400)
                        self.assertEqual(value["code"], "selected_service_not_owned" if provider == "jev" else "external_service_not_owned")
                    self.assertIsNone(management._laya)
                    self.assert_no_work()
                    self.assertEqual(calls, probe_calls)
                    self.assertNotIn(marker, json.dumps(operation))
                    self.assertEqual(self.write("/mode", {"mode": "execute"}, version=original)[0], 409)
                    self.stop(version=original)
                    self.assertFalse(self.server.decision_service.status()["gate_open"])
                    self.assertEqual(self.view()["ex"]["state"], "disabled")
                    self.assertTrue(self.view()["ex"]["can_start"])
                    after = self.get_config()
                    self.assertEqual(after["saved"], before["saved"])
                    self.assertEqual(after["revision"], before["revision"])
                    self.assertEqual(path.read_bytes(), disk)
                    self.assertEqual(calls, probe_calls)
                    self.assertIsNone(management._laya)
                    self.assert_no_work()
            management.close()
            self.assertIsNone(management._laya)

    def test_jev_observed_probe_failure_success_and_saved_identity_do_not_bleed(self):
        status = [401]
        url, calls = self.supplier("jev", inference_status=lambda: status[0])
        saved = self.selection("jev", url)
        saved["jev"]["min_interval_ms"] = 0
        self.assertEqual(self.write("/config", {"config": saved})[0], 200)
        self.assertEqual(self.write("/secret", {"provider": "jev", "action": "set",
                                              "value": "synthetic-provider-view-only"})[0], 200)
        self.assertEqual(self.mode()["state"], "succeeded")
        backend = self.server.decision_service.backend
        self.assertIsInstance(backend, jev.JevBackend)
        self.assertFalse(hasattr(backend, "status"))
        for http_status, code in ((401, "http_401"), (403, "http_403"), (422, "http_422"),
                                  (529, "http_529"), (500, "http_error")):
            status[0] = http_status
            result = backend.probe()
            self.assertEqual(result["error_code"], code)
            self.assertEqual(backend.last_record.reject_code, code)
            before_calls = len(calls)
            view = self.view()
            self.assertEqual(view["connection"]["state"], "disconnected")
            self.assertEqual(view["connection"]["code"], code)
            self.assertEqual(view["error"]["code"], code)
            self.assertEqual(len(calls), before_calls)  # GET view never probes.
        for exception, code in ((ssl.SSLCertVerificationError("private-fixture-body"), "tls_failure"),
                                (OSError("private-fixture-body"), "transport_failure"),
                                (TimeoutError("private-fixture-body"), "deadline_exceeded")):
            with patch.object(jev, "open_connection", side_effect=exception):
                self.assertEqual(backend.probe()["error_code"], code)
            view = self.view()
            self.assertEqual(view["connection"]["state"], "disconnected")
            self.assertEqual(view["connection"]["code"], code)
            self.assertNotIn("private-fixture-body", json.dumps(view))
        status[0] = 200
        self.assertTrue(backend.probe()["inference_ok"])
        self.assertIsNone(backend.last_record.reject_code)
        self.assertEqual(self.view()["connection"]["state"], "unverified")
        self.assertIsNone(self.view()["error"])
        code, accepted, _ = self.write("/test")
        self.assertEqual(code, 202)
        self.assertEqual(self.operation(accepted)["state"], "succeeded")
        self.assertEqual(self.view()["connection"]["state"], "verified")
        status[0] = 401
        self.assertFalse(backend.probe()["ok"])
        self.assertEqual(self.view()["connection"]["state"], "disconnected")
        self.stop()
        self.assertEqual(self.write("/secret", {"provider": "jev", "action": "set",
                                              "value": "synthetic-new-provider-view-key"})[0], 200)
        self.assertEqual(self.view()["connection"]["state"], "unverified")
        self.assertIsNone(self.view()["connection"]["code"])
        self.assertIsNone(self.view()["error"])
        self.assertEqual(self.mode()["state"], "succeeded")
        self.assertFalse(self.server.decision_service.backend.probe()["ok"])
        self.assertEqual(self.view()["connection"]["state"], "disconnected")
        self.stop()
        saved = copy.deepcopy(self.get_config()["saved"])
        saved["jev"]["service_connection"]["base_url"] = url + "/new-prefix"
        self.assertEqual(self.write("/config", {"config": saved})[0], 200)
        self.assertEqual(self.view()["connection"]["state"], "unverified")
        self.assertEqual(self.mode()["state"], "succeeded")
        self.assertFalse(self.server.decision_service.backend.probe()["ok"])
        self.assertEqual(self.view()["connection"]["state"], "disconnected")
        self.stop()
        saved = copy.deepcopy(self.get_config()["saved"])
        saved["backend"] = "laya"
        self.assertEqual(self.write("/config", {"config": saved})[0], 200)
        self.assertEqual(self.view()["provider"], "laya")
        self.assertEqual(self.view()["connection"]["state"], "unverified")
        self.assertIsNone(self.view()["error"])
        self.assert_no_work()

    def test_jev_installed_credential_identity_fences_out_of_band_secret_change(self):
        url, calls = self.supplier("jev", inference_status=401)
        saved = self.selection("jev", url)
        saved["jev"]["min_interval_ms"] = 0
        self.assertEqual(self.write("/config", {"config": saved})[0], 200)
        self.assertEqual(self.write("/secret", {"provider": "jev", "action": "set",
                                              "value": "synthetic-credential-original"})[0], 200)
        self.assertEqual(self.mode()["state"], "succeeded")
        backend = self.server.decision_service.backend
        self.assertFalse(backend.probe()["ok"])
        self.assertEqual(self.view()["connection"]["code"], "http_401")
        # Simulate credential identity replacement through the storage boundary, no file edits.
        with patch.object(self.server.decision_management.store.secrets, "read",
                          return_value="synthetic-credential-replaced"):
            self.assertEqual(self.view()["connection"]["state"], "unverified")
            self.assertIsNone(self.view()["error"])
        self.assertEqual(self.view()["connection"]["code"], "http_401")
        self.assert_no_work()
        self.stop()

    def test_inactive_owned_laya_setting_cannot_acquire_external_jev_process_ownership(self):
        url, calls = self.supplier("jev")
        saved = self.selection("jev", url)
        saved["laya"]["service_connection"]["mode"] = "owned"
        with patch("astrbot_ex.core.decision.management.OwnedLayaService",
                   side_effect=AssertionError("inactive owned provider constructed")):
            self.assertEqual(self.write("/config", {"config": saved})[0], 200)
            self.assertEqual(self.write("/secret", {"provider": "jev", "action": "set",
                                                  "value": "synthetic-jev-key-only"})[0], 200)
            self.assertEqual(self.request("/api/v1/ex/decision/status")[1]["service"],
                             {"mode": "external", "managed": False})
            self.assertTrue(self.view()["ex"]["can_start"])
            for suffix in ("/service/start", "/service/stop", "/service/recover"):
                code, value, _ = self.write(suffix)
                self.assertEqual(code, 400)
                self.assertEqual(value["code"], "selected_service_not_owned")
            self.assertEqual(self.mode()["state"], "succeeded")
            self.assertEqual(self.view()["ex"]["state"], "running")
            self.stop()
            self.server.decision_management.close()
            self.assertIsNone(self.server.decision_management._laya)
            self.assertEqual(calls, [])
            self.assert_no_work()

    def test_missing_required_key_never_activates_or_installs_backend(self):
        for provider in ("jev", "laya"):
            with self.subTest(provider=provider):
                url, calls = self.supplier(provider)
                self.assertEqual(self.write("/config", {"config": self.selection(provider, url)})[0], 200)
                before, backend = self.get_config(), self.server.decision_service.backend
                with patch.object(self.server.controller, "change_mode", side_effect=AssertionError("missing key changed mode")), \
                     patch.object(self.server.controller, "start", side_effect=AssertionError("missing key started runtime")):
                    operation = self.mode()
                self.assertEqual(operation["state"], "failed", operation)
                self.assertEqual(operation["error_code"], "missing_or_invalid_secret")
                self.assertIs(self.server.decision_service.backend, backend)
                self.assertEqual(self.get_config(), before)
                self.assertEqual(calls, [])
                self.assert_idle()
                self.assert_no_work()
                self.assertTrue(self.view()["ex"]["can_start"])
                self.stop()

    def test_failed_runtime_start_keeps_retry_available_then_real_retry_succeeds(self):
        url, calls = self.supplier("laya")
        saved = copy.deepcopy(self.get_config()["saved"])
        saved["backend"] = "laya"
        saved["laya"]["service_connection"]["base_url"] = url
        self.assertEqual(self.write("/config", {"config": saved})[0], 200)
        before = self.get_config()
        with patch.object(self.server.controller, "start", side_effect=RuntimeError("synthetic startup failure")):
            operation = self.mode()
        self.assertEqual(operation["state"], "failed", operation)
        self.assertEqual(operation["error_code"], "management_operation_failed")
        self.assert_idle()
        self.assertFalse(self.server.decision_service.status()["gate_open"])
        view = self.view()
        self.assertEqual(view["ex"]["state"], "failed")
        self.assertTrue(view["ex"]["can_start"])
        self.assertEqual(view["error"]["code"], "activation_failed")
        self.assertEqual(self.mode()["state"], "succeeded")
        self.assertEqual(self.view()["ex"]["state"], "running")
        self.assertIsNone(self.view()["error"])
        self.assertEqual(self.get_config()["saved"], before["saved"])
        self.assertEqual(self.get_config()["revision"], before["revision"])
        self.assertEqual(calls, [])
        self.assert_no_work()
        self.stop()

    def test_mode_metadata_is_atomic_before_worker_and_drives_inflight_view(self):
        saved = copy.deepcopy(self.get_config()["saved"])
        saved["backend"] = "laya"
        saved["laya"]["service_connection"]["base_url"] = "http://127.0.0.1:1"
        self.assertEqual(self.write("/config", {"config": saved})[0], 200)
        management = self.server.decision_management
        original = management._mode
        for mode, state in (("execute", "starting"), ("disabled", "stopping")):
            with self.subTest(mode=mode):
                entered, release = threading.Event(), threading.Event()
                captured = []
                def paused(operation, selected_mode, config):
                    captured.append(operation.get("_mode"))
                    entered.set()
                    if not release.wait(2):
                        raise RuntimeError("fixture release timeout")
                    return original(operation, selected_mode, config)
                with patch.object(management, "_mode", side_effect=paused):
                    try:
                        code, accepted, _ = self.write("/mode", {"mode": mode})
                        self.assertEqual(code, 202)
                        self.assertTrue(entered.wait(1))
                        self.assertEqual(captured, [mode])
                        view = self.view()
                        self.assertEqual(view["ex"]["state"], state)
                        self.assertFalse(view["ex"]["can_start"])
                        self.assertTrue(view["ex"]["can_stop"])
                        code, public, _ = self.request("/api/v1/ex/decision/operations/" + accepted["operation_id"])
                        self.assertEqual(code, 200)
                        self.assertEqual(public["operation"]["state"], "running")
                        self.assertNotIn("_mode", public["operation"])
                    finally:
                        release.set()
                    self.assertEqual(self.operation(accepted)["state"], "succeeded")
        self.assert_idle()


class TrustedIsolatedActivationTests(ManagementHTTPFixture, unittest.TestCase):
    def setUp(self):
        settings = ManagementSettings(allow_test_execution=True, test_isolation=True)
        with patch("tests.test_decision_management_http.ManagementSettings", return_value=settings):
            super().setUp()

    def test_injected_transport_requires_trusted_isolated_composition(self):
        saved = copy.deepcopy(self.get_config()["saved"])
        saved["backend"] = "laya"
        self.assertEqual(self.write("/config", {"config": saved})[0], 200)
        def injected(name, **kwargs):
            if name == "laya":
                kwargs["transport"] = FixtureTransport()
            return create_backend(name, **kwargs)
        with patch("astrbot_ex.core.decision.management.create_backend", side_effect=injected):
            code, accepted, _ = self.write("/mode", {"mode": "execute"})
            self.assertEqual(code, 202)
            operation = self.operation(accepted)
            self.assertEqual(operation["state"], "succeeded", operation)
        backend = self.server.decision_service.backend
        self.assertEqual(backend.execution_capability["transport"], "injected")
        self.assertTrue(backend.execution_allowed)
        self.assertEqual(self.server.controller.runtime.state.value, "running")
        self.assertIsNone(self.server.decision_service.goals.active)
        self.assertEqual(self.server.action_ledger.list_commands().result(1), ())
        code, accepted, _ = self.write("/stop")
        self.assertEqual(code, 202)
        self.assertEqual(self.operation(accepted)["state"], "succeeded")
        self.assert_idle()
