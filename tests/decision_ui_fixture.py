"""C07 isolated browser contract fixture, NOT production activation acceptance.

Uses real temporary EX HTTP composition for neighboring pages and authentication.
Only decision endpoints are replaced here with the frozen stage-B contract. Probe
results, task projections, activation and stop are test data: no supplier requests,
model processes, robot plugins, Goal/Action admission or physical motion.
"""
from __future__ import annotations

import copy
import json
import sys
import threading
import time
import unittest

from astrbot_ex.core.decision.config import ManagementError
from tests.test_decision_management_http import ManagementHTTPFixture

PREFIX = "/api/v1/ex/decision"


class BrowserFixture(ManagementHTTPFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.lock = threading.RLock()
        self.operations = {}
        self.number = 0
        self.mode = "disabled"
        self.connection = "unverified"
        self.task = self.idle_task()
        self.fault = None
        self.test_result = {"ok": True, "inference_ok": True, "health_ok": True,
                            "inference_called": True, "error_code": None}
        self.delay = 0
        self.requests = []
        self.hold_config = None
        handler = type("C07FixtureHandler", (self.server.RequestHandlerClass,), {})
        original_get, original_post = handler.do_GET, handler.do_POST
        fixture = self

        def get(request):
            try:
                if request._path().startswith(PREFIX):
                    fixture.handle(request, False)
                else:
                    original_get(request)
            except ConnectionError:
                pass  # Browser intentionally aborts reads during credential/offline checks.

        def post(request):
            if request._path().startswith(PREFIX):
                fixture.handle(request, True)
            else:
                original_post(request)

        handler.do_GET, handler.do_POST = get, post
        self.server.RequestHandlerClass = handler

    @staticmethod
    def idle_task():
        return {"available": True, "phase": "idle", "title": "", "current_goal": None,
                "completed": 0, "total": 0, "can_cancel": False, "updated_at": 0, "message": "No active task"}

    @property
    def store(self):
        return self.server.decision_management.store

    def config(self):
        return {"ok": True, **self.store.get(), "framework_config_revision": 0}

    def view(self):
        return {"schema_version": 1, "ex_session": self.store.ex_session,
                "revision": self.store.revision, "effective_revision": self.store.effective_revision,
                "provider": self.store.saved["backend"],
                "connection": {"state": self.connection, "code": None, "message": "fixture connection"},
                "ex": {"state": self.mode, "can_start": self.mode in ("disabled", "failed"),
                       "can_stop": self.mode != "disabled", "message": "fixture EX state"},
                "task": copy.deepcopy(self.task), "error": None}

    def handle(self, handler, post):
        if not handler._authorize_http():
            return
        suffix = handler._path()[len(PREFIX):]
        try:
            body = json.loads(handler.rfile.read(int(handler.headers.get("Content-Length", 0)))) if post else None
            with self.lock:
                record = {"method": "POST" if post else "GET", "path": suffix, "body": copy.deepcopy(body)}
                if body and "value" in body:
                    record["body"]["value"] = "[redacted]"
                self.requests.append(record)
                if self.fault and self.fault["path"] == suffix:
                    fault, self.fault = self.fault, None
                    handler._send_json({"ok": False, "code": fault["code"]}, fault["status"])
                    return
                if not post:
                    if suffix == "/config":
                        result = self.config()
                        delay = self.hold_config or 0
                        self.hold_config = None
                    elif suffix == "/view":
                        result, delay = self.view(), 0
                    elif suffix.startswith("/operations/"):
                        op = self.operations[suffix.rsplit("/", 1)[1]]
                        if time.monotonic() >= op["ready_at"]:
                            op["state"] = "succeeded"
                            if op["kind"] == "mode":
                                self.mode = "running"
                            elif op["kind"] == "stop":
                                self.mode = "disabled"
                        result = {**self.config(), "operation": {k: v for k, v in op.items() if k != "ready_at"}}
                        delay = 0
                    else:
                        raise ManagementError("route_not_found", 404)
                else:
                    if suffix != "/stop":
                        self.store.check(body["expected_revision"], body["ex_session"])
                    elif body["ex_session"] != self.store.ex_session:
                        raise ManagementError("session_conflict")
                    if suffix == "/config":
                        if set(body) != {"ex_session", "expected_revision", "config"}:
                            raise ManagementError("invalid_fields", 400)
                        if self.mode == "running":
                            raise ManagementError("requires_disabled")
                        self.store.save(body["config"], body["expected_revision"], body["ex_session"])
                        self.connection = "unverified"
                        result, delay = self.config(), 0
                    elif suffix == "/secret":
                        if set(body) - {"ex_session", "expected_revision", "provider", "action", "value"}:
                            raise ManagementError("invalid_fields", 400)
                        self.store.update_secret(body["action"], body.get("value"), body["expected_revision"],
                                                 body["ex_session"], provider=body["provider"])
                        result, delay = self.config(), 0
                    elif suffix in ("/test", "/mode", "/stop"):
                        self.number += 1
                        operation_id = "c07-fixture-" + str(self.number)
                        kind = suffix[1:]
                        result_value = None
                        if kind == "test":
                            if set(body) - {"ex_session", "expected_revision", "provider", "config", "value"}:
                                raise ManagementError("invalid_fields", 400)
                            draft = body.get("config", self.store.saved)
                            self.store.validate(draft, allow_reference=True)
                            selected = body.get("provider", draft["backend"])
                            matches = draft == self.store.saved and "value" not in body and selected == self.store.saved["backend"]
                            result_value = {**self.test_result, "provider": selected, "scope": "TEST FIXTURE only",
                                            "binding": {"current_config_verified": matches and self.test_result["ok"],
                                                        "config_matches_saved": draft == self.store.saved,
                                                        "session_matches": True, "provider_matches_saved": selected == self.store.saved["backend"],
                                                        "credential_matches_saved": "value" not in body, "current": matches}}
                        elif kind == "mode":
                            if body["mode"] != "execute":
                                raise ManagementError("invalid_mode", 400)
                        else:
                            # Stop supersedes pending activation in the isolated contract fixture.
                            for old in self.operations.values():
                                if old["kind"] == "mode" and old["state"] == "pending":
                                    old["ready_at"] = float("inf")
                                    old["state"] = "superseded"
                        self.operations[operation_id] = {"operation_id": operation_id, "kind": kind, "state": "pending",
                                                         "result": result_value, "error_code": None,
                                                         "ready_at": time.monotonic() + self.delay}
                        handler._send_json({**self.config(), "operation_id": operation_id}, 202)
                        return
                    else:
                        raise ManagementError("route_not_found", 404)
            if delay:
                threading.Event().wait(delay)
            handler._send_json(result)
        except ManagementError as exc:
            handler._send_json({"ok": False, "code": exc.code, "ex_session": self.store.ex_session,
                                "revision": self.store.revision}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def command(self, request):
        kind = request["command"]
        with self.lock:
            if kind == "info":
                return {"base": self.base, "token": self.token, "scope": "isolated frozen HTTP contract fixture"}
            if kind == "requests":
                return copy.deepcopy(self.requests)
            if kind == "configure":
                self.fault = request.get("fault")
                self.delay = request.get("delay", 0)
                self.test_result.update(request.get("test_result", {}))
                if "task" in request:
                    self.task = {**self.idle_task(), **request["task"]}
                if "mode" in request:
                    self.mode = request["mode"]
                if "connection" in request:
                    self.connection = request["connection"]
                if "hold_config" in request:
                    self.hold_config = request["hold_config"]
                return {"ok": True}
            if kind == "change_config":
                value = copy.deepcopy(self.store.saved)
                value["jev"]["min_interval_ms"] += 1
                self.store.save(value, self.store.revision, self.store.ex_session)
                return {"revision": self.store.revision}
            if kind == "restart":
                self.store.ex_session = "c07-restarted-session"
                return {"ex_session": self.store.ex_session}
            if kind == "facts":
                return {"config": self.config(), "requests": copy.deepcopy(self.requests),
                        "goal": self.server.decision_service.goals.active is not None,
                        "actions": len(self.server.action_ledger.list_commands().result(1)),
                        "runtime": self.server.controller.runtime.state.value}
            raise ValueError("unknown fixture command")


def main():
    fixture = BrowserFixture()
    fixture.setUp()
    try:
        print(json.dumps({"ready": fixture.command({"command": "info"})}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request["command"] == "quit":
                break
            try:
                result = {"id": request["id"], "result": fixture.command(request)}
            except Exception:
                result = {"id": request["id"], "error": "fixture command failed"}
            print(json.dumps(result), flush=True)
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
