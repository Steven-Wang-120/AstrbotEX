"""Stage A HTTP provider contracts, using only loopback protocol fixtures."""
from __future__ import annotations

import copy
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from astrbot_ex.core.decision import config as config_module
from astrbot_ex.core.decision.backends import jev, laya
from astrbot_ex.core.decision.config import ManagementError
from tests.test_decision_management_http import ManagementHTTPFixture
from tests.test_laya_backend import health, response
from tests.test_provider_connection import jev_response


class DecisionManagementProviderHTTPTests(ManagementHTTPFixture, unittest.TestCase):
    def supplier(self, provider, *, inference_status=200):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def send(self, value, status):
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                calls.append(("GET", self.path, self.headers.get("Authorization"), None))
                self.send(health(), 200)
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(("POST", self.path, self.headers.get("Authorization"), request))
                value = jev_response(request) if provider == "jev" else response(request["questions"])
                self.send(value, inference_status() if callable(inference_status) else inference_status)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(close)
        return "http://127.0.0.1:" + str(server.server_port) + "/fixture", calls

    def selection(self, provider, url):
        saved = copy.deepcopy(self.get_config()["saved"])
        saved["backend"] = provider
        saved[provider]["service_connection"]["base_url"] = url
        saved[provider]["service_connection"]["auth_mode"] = "bearer"
        return saved

    def probe(self, data=None):
        code, accepted, _ = self.write("/test", data)
        self.assertEqual(code, 202, accepted)
        return accepted, self.operation(accepted)

    def public_probe(self, accepted):
        code, value, _ = self.request("/api/v1/ex/decision/operations/" + accepted["operation_id"])
        self.assertEqual(code, 200)
        return value["operation"]

    def status_probe(self):
        code, value, _ = self.request("/api/v1/ex/decision/status")
        self.assertEqual(code, 200)
        return value["probe"]

    def test_provider_secrets_keep_set_clear_cas_and_separate_storage(self):
        original = self.get_config()
        for provider in ("jev", "laya"):
            code, kept, _ = self.write("/secret", {"provider": provider, "action": "keep"})
            self.assertEqual(code, 200)
            self.assertEqual(kept["revision"], original["revision"])
            self.assertFalse(kept["secrets"][provider]["configured"])
        for provider in ("jev", "laya"):
            before = self.get_config()
            marker = provider + "-HTTP-FIXTURE-SECRET"
            code, saved, _ = self.write("/secret", {"provider": provider, "action": "set", "value": marker})
            self.assertEqual(code, 200)
            self.assertEqual(saved["revision"], before["revision"] + 1)
            self.assertTrue(saved["secrets"][provider]["configured"])
            key = "secret_ref" if provider == "jev" else "laya_secret_ref"
            reference = saved["saved"][key]
            self.assertTrue(reference.startswith(provider + "-"))
            self.assertEqual((self.root / "secrets" / (reference + ".secret")).read_text(), marker)
            self.assertNotIn(marker, json.dumps(saved))
            self.assertEqual(self.write("/secret", {"provider": provider, "action": "clear"}, version=before)[0], 409)
            self.assertEqual(self.write("/secret", {"provider": provider, "action": "set", "value": ""})[0], 400)
            self.assertEqual(self.get_config(), saved)
        before = self.get_config()
        self.assertEqual(self.write("/secret", {"provider": "laya", "action": "clear"})[0], 200)
        after = self.get_config()
        self.assertFalse(after["secrets"]["laya"]["configured"])
        self.assertTrue(after["secrets"]["jev"]["configured"])
        self.assertEqual(after["saved"]["secret_ref"], before["saved"]["secret_ref"])
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assert_idle()

    def test_secret_cleanup_error_reports_committed_fresh_revision(self):
        self.assertEqual(self.write("/secret", {"provider": "laya", "action": "set", "value": "old-fixture-key"})[0], 200)
        before = self.get_config()
        secrets = self.server.decision_management.store.secrets
        with patch.object(secrets, "remove", side_effect=OSError("fixture-cleanup")):
            code, result, _ = self.write("/secret", {"provider": "laya", "action": "set", "value": "new-fixture-key"})
        self.assertEqual(code, 500)
        self.assertEqual(result["code"], "secret_cleanup_failed")
        self.assertEqual(result["revision"], before["revision"] + 1)
        self.assertEqual(self.get_config()["revision"], result["revision"])
        self.assertEqual(self.write("/secret", {"action": "keep"}, version=before)[0], 409)
        self.assertNotIn("new-fixture-key", json.dumps(result))
        self.assert_idle()

    def test_uncertain_secret_publication_reports_readable_revision_and_fences(self):
        before = self.get_config()
        atomic = config_module.atomic_json
        def uncertain(path, value, **kwargs):
            atomic(path, value, **kwargs)
            raise ManagementError("storage_write_uncertain", 500)
        with patch.object(config_module, "atomic_json", side_effect=uncertain):
            code, result, _ = self.write("/secret", {"provider": "laya", "action": "set", "value": "uncertain-fixture-key"})
        self.assertEqual(code, 500)
        self.assertEqual(result["code"], "storage_write_uncertain")
        self.assertEqual(result["revision"], before["revision"] + 1)
        current = self.get_config()
        self.assertTrue(current["storage_uncertain"])
        self.assertTrue(current["secrets"]["laya"]["configured"])
        self.assertEqual(self.write("/test")[0], 500)
        self.assertEqual(self.write("/mode", {"mode": "execute"})[0], 500)
        self.assert_idle()

    def test_draft_providers_ephemeral_key_only_probe_and_close(self):
        management = self.server.decision_management
        before = self.get_config()
        files = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*")
                 if path.is_file() and path.suffix not in {".sqlite3", ".sqlite3-wal", ".sqlite3-shm"}}
        for provider, cls in (("jev", jev.JevBackend), ("laya", laya.LayaBackend)):
            with self.subTest(provider=provider):
                url, calls = self.supplier(provider)
                draft = self.selection(provider, url)
                # The explicit provider must override the draft selection only for this probe.
                draft["backend"] = "mock"
                marker = provider + "-temporary-fixture-key"
                original_close = cls.close
                closed = []
                def close(backend):
                    closed.append(backend)
                    return original_close(backend)
                with patch.object(cls, "close", close), \
                     patch.object(management, "_apply", side_effect=AssertionError("probe applied config")), \
                     patch.object(management.service, "replace_backend", side_effect=AssertionError("probe replaced backend")), \
                     patch("astrbot_ex.core.decision.management.OwnedLayaService", side_effect=AssertionError("probe created owned manager")):
                    accepted, operation = self.probe({"provider": provider, "config": draft, "value": marker})
                    self.assertEqual(operation["state"], "succeeded", operation)
                    result = operation["result"]
                    self.assertTrue(result["ok"], result)
                    self.assertTrue(result["inference_ok"])
                    self.assertFalse(result["binding"]["config_matches_saved"])
                    self.assertFalse(result["binding"]["provider_matches_saved"])
                    self.assertFalse(result["binding"]["credential_matches_saved"])
                    self.assertFalse(result["binding"]["current_config_verified"])
                    self.assertEqual(len(closed), 1)
                    self.assertFalse(closed[0].execution_allowed)
                    self.assertIsNone(management._laya)
                post = next(call for call in calls if call[0] == "POST")
                self.assertEqual(post[1], "/fixture/v1/systemone")
                self.assertEqual(post[2], "Bearer " + marker)
                self.assertEqual(len(post[3]["questions"]), 1)
                self.assertEqual(len(next(iter(post[3]["questions"].values()))["criteria"]), 1)
                self.assertEqual(self.get_config(), before)
                for value in (operation, self.public_probe(accepted), self.status_probe()):
                    self.assertNotIn(marker, json.dumps(value))
                self.assert_idle()
        after = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*")
                 if path.is_file() and path.suffix not in {".sqlite3", ".sqlite3-wal", ".sqlite3-shm"}}
        self.assertEqual(after, files)
        self.assertEqual(management.history.page()["items"], [])

    def test_health_200_inference_401_is_auth_failure_and_backend_closed(self):
        url, calls = self.supplier("laya", inference_status=401)
        closed = []
        original = laya.LayaBackend.close
        def close(backend):
            closed.append(backend)
            original(backend)
        with patch.object(laya.LayaBackend, "close", close):
            _, operation = self.probe({"provider": "laya", "config": self.selection("laya", url), "value": "auth-fixture-key"})
        result = operation["result"]
        self.assertEqual(operation["state"], "succeeded")
        self.assertTrue(result["health_ok"])
        self.assertFalse(result["ok"])
        self.assertFalse(result["inference_ok"])
        self.assertTrue(result["inference_called"])
        self.assertEqual(result["error_code"], "http_401")
        self.assertEqual(result["error_category"], "authentication")
        self.assertFalse(result["binding"]["current_config_verified"])
        self.assertEqual(len(closed), 1)
        self.assertEqual([call[0] for call in calls], ["GET", "POST"])
        self.assert_idle()

    def test_probe_exception_always_closes_and_never_publishes_verified(self):
        class BrokenBackend:
            closed = False
            def probe(self):
                raise RuntimeError("exception-fixture-key")
            def close(self):
                self.closed = True
        backend = BrokenBackend()
        with patch.object(self.server.decision_management, "_make_backend", return_value=backend):
            _, operation = self.probe({"provider": "laya", "value": "exception-fixture-key"})
        self.assertTrue(backend.closed)
        self.assertEqual(operation["state"], "failed")
        self.assertEqual(operation["error_code"], "management_operation_failed")
        self.assertIsNone(self.status_probe())
        self.assertNotIn("exception-fixture-key", json.dumps(operation))
        self.assert_idle()

    def test_saved_verification_dynamic_config_session_provider_key_and_sequence(self):
        url, _ = self.supplier("laya")
        self.assertEqual(self.write("/config", {"config": self.selection("laya", url)})[0], 200)
        self.assertEqual(self.write("/secret", {"provider": "laya", "action": "set", "value": "saved-fixture-key"})[0], 200)
        accepted, operation = self.probe()
        self.assertTrue(operation["result"]["binding"]["current_config_verified"])
        management = self.server.decision_management
        # Presence alone is not proof: a changed file must invalidate credential binding on read.
        secret = self.root / "secrets" / (management.store.saved["laya_secret_ref"] + ".secret")
        secret.write_text("changed-fixture-key")
        for result in (self.public_probe(accepted)["result"], self.status_probe()):
            self.assertFalse(result["binding"]["credential_matches_saved"])
            self.assertFalse(result["binding"]["current_config_verified"])
        secret.write_text("saved-fixture-key")
        session = management.store.ex_session
        management.store.ex_session = "new-fixture-session"
        try:
            for result in (self.public_probe(accepted)["result"], self.status_probe()):
                self.assertFalse(result["binding"]["session_matches"])
                self.assertFalse(result["binding"]["current_config_verified"])
        finally:
            management.store.ex_session = session
        newer, operation = self.probe()
        self.assertTrue(operation["result"]["binding"]["current_config_verified"])
        self.assertFalse(self.public_probe(accepted)["result"]["binding"]["current_config_verified"])
        self.assertEqual(self.status_probe()["operation_id"], newer["operation_id"])
        saved = copy.deepcopy(self.get_config()["saved"])
        saved["backend"] = "jev"
        self.assertEqual(self.write("/config", {"config": saved})[0], 200)
        for result in (self.public_probe(newer)["result"], self.status_probe()):
            self.assertFalse(result["binding"]["current"])
            self.assertFalse(result["binding"]["config_matches_saved"])
            self.assertFalse(result["binding"]["provider_matches_saved"])
            self.assertFalse(result["binding"]["current_config_verified"])
        self.assert_idle()

    def test_old_probe_finishing_after_new_probe_cannot_overwrite_latest(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class Backend:
            def __init__(self, held=False):
                self.held, self.closed = held, False
            def probe(inner):
                if inner.held:
                    entered.set()
                    self.assertTrue(release.wait(2))
                return {"ok": True, "health_ok": True, "inference_ok": True, "inference_called": True, "error_code": None}
            def close(inner):
                inner.closed = True
        old, new = Backend(True), Backend()
        with patch.object(self.server.decision_management, "_make_backend", side_effect=[old, new]):
            code, first, _ = self.write("/test", {"provider": "laya"})
            self.assertEqual(code, 202)
            self.assertTrue(entered.wait(2))
            second, result = self.probe({"provider": "laya"})
            self.assertEqual(result["state"], "succeeded")
            release.set()
            self.assertEqual(self.operation(first)["state"], "superseded")
            self.assertEqual(self.status_probe()["operation_id"], second["operation_id"])
        self.assertTrue(old.closed)
        self.assertTrue(new.closed)
        self.assert_idle()

    def test_external_apply_status_stop_shutdown_never_construct_owned_manager(self):
        management = self.server.decision_management
        url, _ = self.supplier("laya")
        saved = self.selection("laya", url)
        saved["laya"].update(enabled=True, allow_live_http=True)
        with patch("astrbot_ex.core.decision.management.OwnedLayaService", side_effect=AssertionError("external owned manager")):
            self.assertEqual(self.write("/config", {"config": saved})[0], 200)
            self.assertEqual(self.request("/api/v1/ex/decision/status")[1]["service"], {"mode": "external", "managed": False})
            for suffix in ("/service/start", "/service/stop", "/service/recover"):
                code, value, _ = self.write(suffix)
                self.assertEqual(code, 400)
                self.assertEqual(value["code"], "external_service_not_owned")
            code, accepted, _ = self.write("/mode", {"mode": "shadow"})
            self.assertEqual(code, 202)
            self.assertEqual(self.operation(accepted)["state"], "succeeded")
            self.assertIsInstance(management.service.backend, laya.LayaBackend)
            code, accepted, _ = self.write("/stop")
            self.assertEqual(code, 202)
            self.assertEqual(self.operation(accepted)["state"], "succeeded")
            management.close()
            self.assertIsNone(management._laya)
        self.assert_idle()

    def test_invalid_draft_request_and_pinned_model_discovery(self):
        before = self.get_config()
        draft = copy.deepcopy(before["saved"])
        draft["laya"]["service_connection"]["model"] = "unpinned-model"
        for data in ({"config": draft}, {"provider": "mock"}, {"provider": []}, {"value": ""}, {"value": 123}, {"unknown": True}):
            self.assertEqual(self.write("/test", data)[0], 400)
            self.assertEqual(self.get_config(), before)
        body = ('{"ex_session":' + json.dumps(before["ex_session"]) + ',"expected_revision":0,"provider":"jev","provider":"laya"}').encode()
        self.assertEqual(self.request("/api/v1/ex/decision/test", body)[0], 400)
        code, value, _ = self.request("/api/v1/ex/decision/backends")
        self.assertEqual(code, 200)
        providers = {item["name"]: item for item in value["backends"]}
        self.assertEqual(providers["jev"]["model"], before["saved"]["jev"]["service_connection"]["model"])
        self.assertEqual(providers["laya"]["model"], before["saved"]["laya"]["service_connection"]["model"])
        self.assertEqual(self.request("/api/v1/ex/decision/view")[0], 200)
        self.assertEqual(self.write("/apply")[0], 404)
        self.assert_idle()


if __name__ == "__main__":
    unittest.main()
