"""Loopback management contract tests; no weights, robot plugins or model evaluation."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
import zipfile

from astrbot_ex.core.api_server import build_server
from astrbot_ex.core.decision.management import ManagementSettings


class ManagementHTTPFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        with patch.dict(os.environ, {"ASTRBOTEX_DATA_DIR": str(self.root),
                                    "ASTRBOTEX_STT_ENABLED": "", "ASTRBOTEX_TTS_ENABLED": ""}):
            self.server = build_server("127.0.0.1", 0, 20, management_settings=ManagementSettings())
        self.token = (self.root / "secrets/admin.token").read_text().strip()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        try:
            self.server.shutdown()
            self.thread.join(3)
            self.server.server_close()
        finally:
            self.temp.cleanup()

    def request(self, path, data=None, *, method=None, authorization=True, headers=None, raw=False):
        request_headers = {"Content-Type": "application/json"}
        if authorization is True:
            request_headers["Authorization"] = "Bearer " + self.token
        elif isinstance(authorization, str):
            request_headers["Authorization"] = authorization
        request_headers.update(headers or {})
        body = data if isinstance(data, bytes) else json.dumps(data).encode() if data is not None else None
        req = Request(self.base + path, data=body, headers=request_headers, method=method)
        try:
            response = self.opener.open(req, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            value = response.read()
            return response.status, value if raw else json.loads(value), dict(response.headers)

    def get_config(self):
        code, config, _ = self.request("/api/v1/ex/decision/config")
        self.assertEqual(code, 200)
        return config

    def write(self, suffix, data=None, *, version=None):
        config = version or self.get_config()
        payload = {"expected_revision": config["revision"], "ex_session": config["ex_session"], **(data or {})}
        return self.request("/api/v1/ex/decision" + suffix, payload)

    def operation(self, accepted):
        operation_id = accepted["operation_id"]
        end = time.monotonic() + 3
        while time.monotonic() < end:
            code, result, _ = self.request("/api/v1/ex/decision/operations/" + operation_id)
            self.assertEqual(code, 200)
            operation = result["operation"]
            if operation["state"] in {"succeeded", "failed", "blocked", "superseded"}:
                return operation
            threading.Event().wait(.003)
        self.fail("operation did not reach a public terminal state")

    def assert_idle(self):
        self.assertEqual(self.server.decision_service.status()["mode"], "disabled")
        self.assertIsNone(self.server.decision_service.goals.active)
        self.assertIsNone(self.server.decision_service.goals.pending_replace)
        self.assertEqual(self.server.action_ledger.list_commands().result(1), ())
        self.assertEqual(self.server.controller.runtime.state.value, "idle")


class DecisionManagementHTTPTests(ManagementHTTPFixture, unittest.TestCase):
    def test_all_sensitive_old_and_new_reads_require_credential(self):
        paths = ("/api/status", "/api/v1/ex/status", "/api/events", "/api/v1/ex/events",
            "/api/plugins", "/api/v1/ex/plugins", "/api/connections", "/api/v1/ex/connections",
            "/api/v1/ex/environments", "/api/v1/ex/environments/ros2/graph",
            "/api/v1/ex/backups/nonexistent.zip", "/api/v1/ex/decision/status",
            "/api/v1/ex/decision/config", "/api/v1/ex/decision/actions", "/api/v1/ex/decision/decisions")
        for path in paths:
            with self.subTest(path=path):
                code, value, _ = self.request(path, authorization=False)
                self.assertEqual(code, 401)
                self.assertFalse(value["ok"])
                self.assertNotIn(self.token, json.dumps(value))
        code, _, _ = self.request("/api/v1/ex/decision/config", authorization="Bearer wrong-test-credential")
        self.assertEqual(code, 401)
        self.assert_idle()

    def test_old_control_aliases_writes_upload_and_restore_are_not_auth_bypasses(self):
        requests = [("POST", path) for path in ("/api/runtime/start", "/api/v1/ex/runtime/start",
            "/api/runtime/stop", "/api/v1/ex/runtime/stop", "/api/v1/ex/runtime/control-mode",
            "/api/v1/ex/environments/select", "/api/v1/ex/backups", "/api/v1/ex/backups/upload",
            "/api/v1/ex/plugins/upload", "/api/plugins/absent/enable",
            "/api/v1/ex/plugins/absent/config", "/api/v1/ex/decision/service/start")]
        requests += [("PUT", "/api/v1/ex/connections/absent"), ("DELETE", "/api/v1/ex/plugins/absent")]
        for method, path in requests:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(path, {}, method=method, authorization=False)[0], 401)
        self.assert_idle()

    def test_host_origin_are_checked_and_script_without_origin_still_needs_auth(self):
        port = self.server.server_address[1]
        for headers in ({"Host": "localhost.evil:" + str(port)}, {"Host": "attacker.example"},
                        {"Origin": "http://attacker.example"}, {"Origin": "null"},
                        {"Origin": "https://127.0.0.1:" + str(port)}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/v1/ex/decision/status", headers=headers)[0], 403)
        self.assertEqual(self.request("/api/v1/ex/decision/status", headers={"Origin": self.base})[0], 200)
        self.assertEqual(self.request("/api/v1/ex/decision/status")[0], 200)
        self.assertEqual(self.request("/api/v1/ex/decision/status", authorization=False)[0], 401)

    def test_sensitive_json_has_no_wildcard_cors_and_is_not_cached(self):
        for path in ("/api/status", "/api/v1/ex/decision/config", "/api/v1/ex/decision/snapshot"):
            code, value, headers = self.request(path)
            self.assertEqual(code, 200)
            self.assertNotEqual(headers.get("Access-Control-Allow-Origin"), "*")
            self.assertIn("no-store", headers.get("Cache-Control", ""))
            self.assertNotIn(self.token, json.dumps(value))

    def test_save_updates_saved_only_without_probe_process_mode_runtime_or_goal(self):
        original = self.get_config()
        saved = copy.deepcopy(original["saved"])
        saved["backend"] = "mock"
        with patch.object(self.server.decision_service, "replace_backend", wraps=self.server.decision_service.replace_backend) as replace_backend, \
             patch.object(self.server.decision_service, "set_mode", wraps=self.server.decision_service.set_mode) as set_mode:
            code, result, _ = self.write("/config", {"config": saved}, version=original)
            self.assertEqual(code, 200)
            self.assertEqual(result["revision"], original["revision"] + 1)
            current = self.get_config()
            self.assertEqual(current["saved"], saved)
            self.assertEqual(current["effective_revision"], original["effective_revision"])
            self.assertEqual(current["effective"], original["effective"])
            replace_backend.assert_not_called()
            set_mode.assert_not_called()
        self.assert_idle()

    def test_concurrent_cas_only_one_save_succeeds_and_old_write_cannot_overwrite(self):
        original = self.get_config()
        barrier, results = threading.Barrier(3), []
        def save():
            barrier.wait()
            results.append(self.write("/config", {"config": copy.deepcopy(original["saved"])}, version=original))
        threads = [threading.Thread(target=save) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(item[0] for item in results), [200, 409])
        current = self.get_config()
        self.assertEqual(current["revision"], original["revision"] + 1)
        self.assertEqual(self.write("/config", {"config": original["saved"]}, version=original)[0], 409)
        self.assertEqual(self.get_config(), current)

    def test_invalid_session_and_readonly_execution_model_fields_do_not_write(self):
        original = self.get_config()
        wrong = {**original, "ex_session": "another-session"}
        self.assertEqual(self.write("/config", {"config": original["saved"]}, version=wrong)[0], 409)
        for key, value in (("allow_test_execution", True), ("python", "/bin/sh"), ("model", "other"), ("revision", "latest")):
            with self.subTest(key=key):
                invalid = copy.deepcopy(original["saved"])
                invalid[key] = value
                self.assertEqual(self.write("/config", {"config": invalid}, version=original)[0], 400)
                self.assertEqual(self.get_config(), original)
        for key, value in (("allow_test_execution", True), ("model", "other"), ("revision", "latest"), ("port", 1)):
            with self.subTest(laya_key=key):
                invalid = copy.deepcopy(original["saved"])
                invalid["laya"][key] = value
                self.assertEqual(self.write("/config", {"config": invalid}, version=original)[0], 400)
                self.assertEqual(self.get_config(), original)

    def test_atomic_save_failure_preserves_disk_saved_effective_and_revision(self):
        before = self.get_config()
        path = self.root / "profiles/default/decision.json"
        content = path.read_bytes() if path.exists() else None
        with patch("os.replace", side_effect=OSError("injected atomic-write failure")):
            code, result, _ = self.write("/config", {"config": before["saved"]}, version=before)
        self.assertGreaterEqual(code, 400)
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_bytes() if path.exists() else None, content)
        self.assertEqual(self.get_config(), before)
        self.assert_idle()

    def test_secret_set_keep_clear_no_implicit_empty_clear_and_permissions(self):
        marker = "B08-TEST-SUPPLIER-SECRET-ONLY"
        original = self.get_config()
        code, set_value, _ = self.write("/secret", {"action": "set", "value": marker}, version=original)
        self.assertEqual(code, 200)
        current = self.get_config()
        self.assertNotIn(marker, json.dumps(set_value))
        self.assertNotIn(marker, json.dumps(current))
        self.assertEqual(current["revision"], original["revision"] + 1)
        secret_files = list((self.root / "secrets").iterdir())
        self.assertEqual((self.root / "secrets").stat().st_mode & 0o777, 0o700)
        self.assertTrue(any(path.read_text() == marker or path.read_text().strip() == marker
                            for path in secret_files if path.is_file()))
        for path in secret_files:
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        code, kept, _ = self.write("/secret", {"action": "keep"}, version=current)
        self.assertEqual(code, 200)
        self.assertEqual(kept["revision"], current["revision"])
        self.assertEqual(self.write("/secret", {"action": "set", "value": ""}, version=current)[0], 400)
        self.assertEqual(self.get_config()["revision"], current["revision"])
        self.assertEqual(self.write("/secret", {"action": "clear"}, version=current)[0], 200)
        self.assertFalse(any(marker in path.read_text() for path in secret_files if path.exists() and path.is_file()))

    def test_backends_show_real_capabilities_and_probe_has_no_action_side_effect(self):
        code, value, _ = self.request("/api/v1/ex/decision/backends")
        self.assertEqual(code, 200)
        self.assertIn("mock", json.dumps(value).lower())
        self.assertIn("jev", json.dumps(value).lower())
        self.assertIn("laya", json.dumps(value).lower())
        code, accepted, _ = self.write("/test")
        self.assertEqual(code, 202)
        operation = self.operation(accepted)
        self.assertIn(operation["state"], {"succeeded", "failed", "blocked"})
        self.assertIn(operation["kind"], {"test", "probe"})
        self.assert_idle()

    def test_mode_is_async_and_does_not_start_runtime_or_goal(self):
        code, accepted, _ = self.write("/mode", {"mode": "shadow"})
        self.assertEqual(code, 202)
        operation = self.operation(accepted)
        self.assertEqual(operation["state"], "succeeded")
        self.assertEqual(self.server.decision_service.status()["mode"], "shadow")
        self.assertEqual(self.server.controller.runtime.state.value, "idle")
        self.assertIsNone(self.server.decision_service.goals.active)
        self.assertEqual(self.server.action_ledger.list_commands().result(1), ())

    def test_stop_ignores_stale_revision_but_requires_current_session_and_proof(self):
        original = self.get_config()
        self.write("/config", {"config": original["saved"]}, version=original)
        code, accepted, _ = self.write("/stop", {"reason": "fixture stop"}, version=original)
        self.assertEqual(code, 202)
        operation = self.operation(accepted)
        self.assertEqual(operation["state"], "succeeded")
        self.assertEqual(self.server.decision_service.status()["stop"]["state"], "proven")
        self.assertFalse(self.server.decision_service.status()["gate_open"])
        wrong = {**self.get_config(), "ex_session": "stale-ex-session"}
        self.assertEqual(self.write("/stop", version=wrong)[0], 409)

    def test_ordinary_assembly_cannot_gain_laya_execution_from_http(self):
        original = self.get_config()
        saved = copy.deepcopy(original["saved"])
        saved["backend"] = "laya"
        self.assertEqual(self.write("/config", {"config": saved}, version=original)[0], 200)
        code, accepted, _ = self.write("/mode", {"mode": "execute"})
        if code == 202:
            operation = self.operation(accepted)
            self.assertIn(operation["state"], {"failed", "blocked"})
        else:
            self.assertEqual(code, 409)
        self.assertEqual(self.server.decision_service.status()["mode"], "disabled")
        self.assertEqual(self.server.action_ledger.list_commands().result(1), ())

    def test_backup_excludes_secret_credentials_and_restore_creates_new_version(self):
        marker = "B08-BACKUP-SECRET-MUST-NOT-EXPORT"
        self.assertEqual(self.write("/secret", {"action": "set", "value": marker})[0], 200)
        code, created, _ = self.request("/api/v1/ex/backups", {})
        self.assertEqual(code, 200)
        code, content, headers = self.request(created["backup"]["download_url"], raw=True)
        self.assertEqual(code, 200)
        self.assertNotEqual(headers.get("Access-Control-Allow-Origin"), "*")
        self.assertIn("no-store", headers.get("Cache-Control", ""))
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertFalse(any(name.startswith("data/secrets/") or "actions.sqlite3" in name for name in archive.namelist()))
            for name in archive.namelist():
                body = archive.read(name)
                self.assertNotIn(self.token.encode(), body)
                self.assertNotIn(marker.encode(), body)
        before = self.get_config()
        self.assertEqual(self.write("/config", {"config": before["saved"]}, version=before)[0], 200)
        revision_before_restore = self.get_config()["revision"]
        boundary = "b08-fixture-restore"
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="snapshot.zip"\r\n'
                'Content-Type: application/zip\r\n\r\n').encode() + content + f'\r\n--{boundary}--\r\n'.encode()
        code, restored, _ = self.request("/api/v1/ex/backups/upload", body,
                                        headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        self.assertEqual(code, 200)
        self.assertTrue(restored["ok"])
        self.assertGreater(self.get_config()["revision"], revision_before_restore)
        self.assert_idle()

    def test_pagination_limits_and_unknown_operation_have_explicit_errors(self):
        self.assertEqual(self.request("/api/v1/ex/decision/operations/nonexistent")[0], 404)
        for suffix in ("/decisions?limit=101", "/actions?limit=101", "/decisions?limit=0", "/actions?limit=bad"):
            self.assertEqual(self.request("/api/v1/ex/decision" + suffix)[0], 400)


if __name__ == "__main__":
    unittest.main()
