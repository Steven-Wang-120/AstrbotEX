"""SystemOne URL, production transport and fixed probes on fixture-only loopback."""
from __future__ import annotations

import json
import ssl
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from astrbot_ex.core.decision.backends import connection, jev, laya
from tests.test_laya_backend import FixtureTransport, health, reply, response


def jev_response(request):
    return {"model": jev.PINNED_MODEL, "usage": {"input_tokens": 12, "output_tokens": 0},
            "answers": {owner: {"type": "choice", "choice": next(iter(question["criteria"])),
                               "confidence": 1, "probabilities": {option: int(index == 0)
                                  for index, option in enumerate(question["criteria"])}}
                        for owner, question in request["questions"].items()}}


class ProviderConnectionTests(unittest.TestCase):
    def test_urls_localhost_literals_prefix_and_rejected_inputs(self):
        for url in ("http://localhost:8769/prefix/", "http://127.0.0.1:8769", "http://[::1]:8769",
                    "http://127.0.0.2", "https://remote.example.invalid/prefix", "https://[2001:db8::1]/prefix"):
            with self.subTest(url=url):
                self.assertTrue(connection.service_url(url))
                self.assertTrue(connection.endpoint(url, "/health").endswith("/health"))
        invalid = ("http://localhost.evil/", "http://localhost./", "http://remote.example.invalid",
                   "http://192.168.1.2", "http://127.1", "http://2130706433", "ftp://127.0.0.1",
                   "http://user:key@127.0.0.1", "https://user:key@remote.example.invalid", "http://127.0.0.1?q=key",
                   "https://remote.example.invalid#key", "http://127.0.0.1:0", "http://127.0.0.1:65536",
                   "http://127.0.0.1:", "http://127.0.0.1/../x", "https://remote.example.invalid/%2e%2e/x",
                   "http://127.0.0.1/%2fattack", "http://127.0.0.1/\\attack", "http://127.0.0.1/\nattack",
                   "http://[::1%25evil]", "https://bad_host", "https://%65xample.invalid")
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError):
                connection.service_url(url)
        with patch.object(connection.http.client, "HTTPConnection") as local:
            connection.open_connection("http://localhost:8769/prefix", 1.5)
            local.assert_called_once_with("127.0.0.1", 8769, timeout=1.5)

    def test_remote_tls_uses_stdlib_verified_context_and_reports_tls_failure(self):
        with patch.object(connection.http.client, "HTTPSConnection") as secure:
            connection.open_connection("https://remote.example.invalid:9443/prefix", 2)
            secure.assert_called_once_with("remote.example.invalid", 9443, timeout=2)
            self.assertNotIn("context", secure.call_args.kwargs)
        settings = jev.JevConfig(mode="shadow", allow_live_http=True, min_interval_ms=0)
        backend = jev.JevBackend(settings, secret_provider=lambda: "fixture-key-only")
        self.addCleanup(backend.close)
        with patch.object(jev, "_http_transport", side_effect=ssl.SSLCertVerificationError("fixture-key-only")):
            result = backend.probe()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "tls_failure")
        self.assertNotIn("fixture-key-only", repr(result))
        backend = laya.LayaBackend(laya.LayaConfig(enabled=True, allow_live_http=True))
        self.addCleanup(backend.close)
        with patch.object(laya, "_http_transport", side_effect=ssl.SSLCertVerificationError("fixture-key-only")):
            result = backend.probe()
        self.assertEqual(result["error_code"], "tls_failure")
        self.assertFalse(result["health_ok"])
        self.assertFalse(result["inference_called"])

    def server(self, *, provider, status=200, mutate=None, health_status=200, health_value=None, peers=None):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1" if peers is not None else "HTTP/1.0"
            def log_message(self, *args):
                pass
            def send(self, value, code):
                if peers is not None:
                    peers.append(self.client_address)
                body = json.dumps(value).encode()
                self.send_response(code)
                if 300 <= code < 400:
                    self.send_header("Location", "/must-not-follow")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                calls.append((self.command, self.path, self.headers.get("Authorization"), None))
                self.send(health() if health_value is None else health_value, health_status)
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.command, self.path, self.headers.get("Authorization"), request))
                raw = jev_response(request) if provider == "jev" else response(request["questions"])
                if mutate:
                    mutate(raw)
                self.send(raw, status)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(1)
        self.addCleanup(close)
        return "http://localhost:" + str(server.server_port) + "/gateway/", calls

    def backend(self, provider, url, **kwargs):
        if provider == "jev":
            config = jev.JevConfig(base_url=url, mode="shadow", allow_live_http=True, min_interval_ms=0, **kwargs)
            backend = jev.JevBackend(config, secret_provider=lambda: "jev-fixture-key-only")
        else:
            config = laya.LayaConfig(base_url=url, enabled=True, allow_live_http=True, auth_mode="bearer", **kwargs)
            backend = laya.LayaBackend(config, secret_provider=lambda: "laya-fixture-key-only")
        self.addCleanup(backend.close)
        return backend

    def test_native_backend_reuses_http11_between_model_calls(self):
        for provider in ("jev", "laya"):
            with self.subTest(provider=provider):
                peers = []
                url, calls = self.server(provider=provider, peers=peers)
                backend = self.backend(provider, url)
                self.assertTrue(backend.probe()["ok"])
                self.assertTrue(backend.probe()["ok"])
                self.assertEqual(sum(method == "POST" for method, *_ in calls), 2)
                self.assertEqual(len(set(peers)), 1)

    def test_builtin_probe_fixed_wait_prefix_auth_and_production_decoding(self):
        for provider in ("jev", "laya"):
            with self.subTest(provider=provider):
                url, calls = self.server(provider=provider)
                backend = self.backend(provider, url)
                result = backend.probe()
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["inference_called"])
                self.assertTrue(result["inference_ok"])
                self.assertIs(result["health_ok"], True if provider == "laya" else None)
                self.assertFalse(backend.execution_allowed)
                post = next(call for call in calls if call[0] == "POST")
                self.assertEqual(post[1], "/gateway/v1/systemone")
                self.assertEqual(post[2], "Bearer " + provider + "-fixture-key-only")
                request = post[3]
                self.assertEqual(len(request["questions"]), 1)
                self.assertEqual(len(next(iter(request["questions"].values()))["criteria"]), 1)
                if provider == "jev":
                    snapshot = request["state"]["snapshot"]
                    self.assertEqual(snapshot["goal"]["allowed_actions"], [])
                    self.assertEqual(snapshot["observations"], [])
                    self.assertEqual(snapshot["owners"][0]["candidates"][0]["kind"], "wait")
                else:
                    self.assertEqual(calls[0][1], "/gateway/health")
                    self.assertEqual(json.loads(request["state"])["allowed_actions"], [])

    def test_health_good_inference_auth_denied_and_redirects_not_followed(self):
        for provider in ("jev", "laya"):
            for status in (401, 403, 302):
                with self.subTest(provider=provider, status=status):
                    url, calls = self.server(provider=provider, status=status)
                    result = self.backend(provider, url).probe()
                    self.assertFalse(result["ok"])
                    self.assertTrue(result["inference_called"])
                    self.assertEqual(result["error_code"], "http_redirect" if status == 302 else "http_" + str(status))
                    self.assertIs(result["health_ok"], True if provider == "laya" else None)
                    self.assertEqual(len([call for call in calls if call[0] == "POST"]), 1)
                    self.assertFalse(any("must-not-follow" in call[1] for call in calls))

    def test_invalid_probe_identity_choices_and_health_are_not_success(self):
        for provider in ("jev", "laya"):
            for mutate in (lambda x: x.update(model="unverified-model"),
                           lambda x: next(iter(x["answers"].values())).update(choice="invented")):
                with self.subTest(provider=provider, mutate=mutate):
                    url, calls = self.server(provider=provider, mutate=mutate)
                    result = self.backend(provider, url).probe()
                    self.assertFalse(result["ok"])
                    self.assertFalse(result["inference_ok"])
                    self.assertTrue(result["inference_called"])
        url, calls = self.server(provider="laya", health_value=health(revisions={"typed-decisions": "wrong"}))
        result = self.backend("laya", url).probe()
        self.assertEqual(result["error_code"], "revision_mismatch")
        self.assertFalse(result["inference_called"])
        self.assertEqual(len(calls), 1)

    def test_execution_gate_builtin_and_injected_fixture_separation(self):
        jev_config = jev.JevConfig(mode="execute", allow_live_http=True, min_interval_ms=0)
        laya_config = laya.LayaConfig(enabled=True, execution_enabled=True, allow_live_http=True)
        constructors = ((jev.JevBackend, jev_config, lambda *a: jev.HTTPReply(200, b"{}")),
                        (laya.LayaBackend, laya_config, FixtureTransport()))
        for cls, settings, transport in constructors:
            with self.subTest(cls=cls):
                live = cls(settings, secret_provider=lambda: "fixture-key-only")
                injected = cls(settings, transport=transport, secret_provider=lambda: "fixture-key-only")
                fixture = cls(settings, transport=transport, secret_provider=lambda: "fixture-key-only", allow_test_execution=True)
                for backend in (live, injected, fixture):
                    self.addCleanup(backend.close)
                self.assertTrue(live.execution_allowed)
                self.assertFalse(injected.execution_allowed)
                self.assertTrue(fixture.execution_allowed)
                with self.assertRaises(AttributeError):
                    injected.execution_allowed = True
                capability = injected.execution_capability
                capability["allowed"] = True
                self.assertFalse(injected.execution_allowed)
                live.close()
                self.assertFalse(live.execution_allowed)
        backend = jev.JevBackend(jev_config)
        self.addCleanup(backend.close)
        self.assertFalse(backend.execution_allowed)
        with self.assertRaises(AttributeError):
            backend.min_confidence = 0
        for model in ("jev-latest", "jev-1.14.0", "jev-2.0.0"):
            self.assertEqual(replace(jev_config, model=model).model, model)
        for invalid in ("latest", "jev-1.14", "jev-1.14.0/extra"):
            with self.assertRaises(jev.JevBackendError):
                replace(jev_config, model=invalid)

    def test_disabled_jev_probe_does_not_enable_mode(self):
        backend = jev.JevBackend(jev.JevConfig(mode="disabled", min_interval_ms=0),
                                 transport=lambda body, *a: jev.HTTPReply(200, json.dumps(jev_response(json.loads(body))).encode()),
                                 secret_provider=lambda: "fixture-key-only")
        self.addCleanup(backend.close)
        self.assertTrue(backend.probe()["ok"])
        self.assertEqual(backend.config.mode, "disabled")
        self.assertFalse(backend.execution_allowed)
        with self.assertRaises(jev.JevBackendError):
            backend.decide(connection.probe_snapshot())

    def test_disabled_laya_probe_does_not_enable_config_or_bypass_quarantine(self):
        backend = laya.LayaBackend(laya.LayaConfig(), transport=FixtureTransport())
        self.addCleanup(backend.close)
        result = backend.probe()
        self.assertTrue(result["ok"], result)
        self.assertFalse(backend.config.enabled)
        self.assertFalse(backend.execution_allowed)
        transport = FixtureTransport(mutate=lambda x: x.update(model="wrong"))
        backend = laya.LayaBackend(laya.LayaConfig(), transport=transport)
        self.addCleanup(backend.close)
        self.assertFalse(backend.probe()["ok"])
        count = len(transport.requests)
        result = backend.probe()
        self.assertEqual(result["error_code"], "restart_required")
        self.assertFalse(result["inference_called"])
        self.assertEqual(len(transport.requests), count)

    def test_probe_after_previous_call_does_not_reuse_inference_flag(self):
        backend = jev.JevBackend(jev.JevConfig(mode="shadow", min_interval_ms=60000),
                                 transport=lambda body, *a: jev.HTTPReply(200, json.dumps(jev_response(json.loads(body))).encode()),
                                 secret_provider=lambda: "fixture-key-only")
        self.addCleanup(backend.close)
        self.assertTrue(backend.probe()["ok"])
        result = backend.probe()
        self.assertEqual(result["error_code"], "rate_limited")
        self.assertFalse(result["inference_called"])
        backend = jev.JevBackend(jev.JevConfig(mode="shadow", min_interval_ms=0), transport=lambda *a: None)
        self.addCleanup(backend.close)
        result = backend.probe()
        self.assertEqual(result["error_code"], "missing_or_invalid_secret")
        self.assertFalse(result["inference_called"])



class PersistentConnectionTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.accepted = []
        class Server(ThreadingHTTPServer):
            daemon_threads = True
            def get_request(server):
                sock, addr = super().get_request()
                self.accepted.append(addr)
                return sock, addr
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args):
                pass
            def do_POST(handler):
                body = handler.rfile.read(int(handler.headers["Content-Length"]))
                self.requests.append(body)
                if body == b"lost-reply":
                    handler.close_connection = True
                    return
                handler.send_response(200)
                handler.send_header("Content-Length", "2")
                if body == b"server-close":
                    handler.send_header("Connection", "close")
                handler.end_headers()
                handler.wfile.write(b"ok")
        self.server = Server(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.pool = connection.ConnectionPool()
        self.addCleanup(self.pool.close)

    def post(self, body):
        metrics = {}
        with self.pool.connection(self.url, 2, timings=metrics) as conn:
            conn.request("POST", "/", body)
            self.assertEqual(conn.getresponse().read(), b"ok")
        return metrics

    def test_twenty_posts_reuse_one_connection_across_ephemeral_workers(self):
        from concurrent.futures import ThreadPoolExecutor
        for index in range(20):
            with ThreadPoolExecutor(max_workers=1) as worker:
                metrics = worker.submit(self.post, str(index).encode()).result(3)
            self.assertEqual(metrics["connection_reused"], index > 0)
            if index:
                self.assertEqual(metrics["connect_tls_ms"], 0)
        self.assertEqual(len(self.accepted), 1)
        self.assertEqual(self.requests, [str(i).encode() for i in range(20)])
        self.pool.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.post(b"after-close")

    def test_close_and_lost_reply_never_replay_post(self):
        import http.client
        self.post(b"server-close")
        self.assertFalse(self.post(b"next")["connection_reused"])
        with self.assertRaises(http.client.RemoteDisconnected):
            self.post(b"lost-reply")
        self.assertFalse(self.post(b"explicit-next")["connection_reused"])
        self.assertEqual(self.requests, [b"server-close", b"next", b"lost-reply", b"explicit-next"])
        self.assertEqual(len(self.accepted), 3)

    def test_tailnet_literal_opt_in_only(self):
        url = "http://100.114.26.5:11434/v1"
        with self.assertRaises(ValueError):
            connection.service_url(url)
        self.assertEqual(connection.endpoint(url, "/chat/completions", allow_tailnet_http=True),
                         "/v1/chat/completions")
        for host in ("100.63.255.255", "100.128.0.0", "workstation.example", "192.168.1.21"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                connection.service_url("http://" + host, allow_tailnet_http=True)


if __name__ == "__main__":
    unittest.main()
