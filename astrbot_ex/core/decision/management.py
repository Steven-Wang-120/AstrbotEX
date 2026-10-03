"""B08 local management. HTTP data cannot grant execution or process ownership."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..serialization import to_jsonable
from ..actions.ledger import OwnerBinding
from .backends.registry import create_backend
from .backends.laya import LayaConfig, LayaBackendError
from .backends.jev import JevConfig, API_HOST, API_PATH
from .config import DecisionConfigStore, ManagementError
from .history import RequestHistory, display
from .owned_laya import Deployment, OwnedLayaService


PREFIX = "/api/v1/ex/decision"


@dataclass(frozen=True)
class ManagementSettings:
    # Trusted composition only. None of these fields is accepted over HTTP.
    laya_deployment: Deployment | None = None
    allow_test_execution: bool = False
    test_isolation: bool = False
    laya_service_factory: object = None
    laya_transport_factory: object = None
    operation_timeout_s: float = 15
    max_active_operations: int = 8


class ManagedLayaBackend:
    def __init__(self, backend, manager, generation):
        self.inner, self.manager, self.generation = backend, manager, generation

    @property
    def execution_allowed(self):
        return self.inner.execution_allowed

    @property
    def last_record(self):
        return self.inner.last_record

    @property
    def config(self):
        return self.inner.config

    def status(self):
        result = self.inner.status()
        result.update(service_generation=self.generation,
                      service_restart_required=self.manager.status()["restart_required"])
        return result

    def decide(self, snapshot):
        with self.manager.decision_guard(self.generation, self.inner):
            return self.inner.decide(snapshot)

    def probe(self):
        return self.inner.probe()

    def cancel(self):
        self.inner.cancel()
        if self.inner.status()["restart_required"]:
            self.manager.quarantine(self.generation, "canceled_after_post")

    def close(self):
        self.inner.close()
        if self.inner.status()["restart_required"]:
            self.manager.quarantine(self.generation, "closed_after_post")


class DecisionManagement:
    def __init__(self, decision_service, data_root, *, event_bus=None, settings=None):
        self.service, self.data_root, self.event_bus = decision_service, Path(data_root).resolve(), event_bus
        self.settings = settings or ManagementSettings()
        if not isinstance(self.settings, ManagementSettings):
            raise ManagementError("invalid_management_settings", 500)
        if self.settings.allow_test_execution and (not self.settings.test_isolation or
                not self.data_root.is_relative_to(Path(tempfile.gettempdir()).resolve()) or
                (self.data_root / "plugins").exists() and any((self.data_root / "plugins").rglob("plugin.json"))):
            raise ManagementError("test_execution_requires_isolated_test_composition", 500)
        if self.settings.operation_timeout_s <= 0 or not 1 <= self.settings.max_active_operations <= 8:
            raise ManagementError("invalid_management_settings", 500)
        root = Path(__file__).resolve().parents[3]
        deployment = self.settings.laya_deployment or Deployment(
            python=Path(os.environ.get("ASTRBOTEX_LAYA_PYTHON", str(root / "runtime/laya/.venv/bin/python"))),
            cache=Path(os.environ.get("ASTRBOTEX_LAYA_CACHE", "/data/shared/AstrEX_project_data/models/pretrained/laya/hub")),
            output=self.data_root / "execution/laya", state_path=self.data_root / "execution/laya/service-state.json")
        self.store = DecisionConfigStore(self.data_root, self.service.goals.ex_session, port=deployment.port)
        self.credential_path = self.store.secrets.credential_path
        self.laya = (self.settings.laya_service_factory or OwnedLayaService)(deployment)
        self._lock = threading.RLock()
        self._service_serial = threading.Lock()
        self._apply_serial = threading.Lock()
        self._operations = OrderedDict()
        self._intent = 0
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="decision-management")
        self.history = RequestHistory(notify=self._history_notice, redact=self.store.secrets.redact)
        self.service.configure_management_trace(self.history.queue)

    def _history_notice(self, notice):
        if self.event_bus is not None:
            self.event_bus.emit("decision_changed", "decision changed", **notice)

    def sanitize(self, value):
        return self.store.secrets.redact(to_jsonable(value))

    def sse_payload(self, value):
        raw = self.sanitize(value)
        if isinstance(raw, dict) and raw.get("type") == "decision_changed":
            raw["data"] = {k: v for k, v in raw.get("data", {}).items()
                           if k in {"request_id", "snapshot_id", "sequence", "kind"}}
        return raw

    def versions(self):
        return {"ex_session": self.store.ex_session, "revision": self.store.revision,
                "effective_revision": self.store.effective_revision,
                "framework_config_revision": self.service.status()["config_revision"]}

    def response(self, **values):
        return {"ok": True, **self.versions(), **values}

    def authorize(self, handler):
        if any(len(handler.headers.get_all(name, [])) > 1 for name in ("Host", "Origin", "Authorization", "Content-Length", "Transfer-Encoding")):
            handler._send_json({"ok": False, "code": "duplicate_security_header", "message": "duplicate header rejected"}, 400)
            return False
        if len(handler.path) > 4096 or "#" in handler.path:
            handler._send_json({"ok": False, "code": "invalid_request_target", "message": "invalid request target"}, 400)
            return False
        host = handler.headers.get("Host", "")
        try:
            parsed = urlparse("http://" + host)
            valid_host = (parsed.hostname in {"127.0.0.1", "localhost"} and not parsed.username and
                          not parsed.password and not parsed.path and not parsed.query and not parsed.fragment and
                          parsed.port is not None and 1 <= parsed.port <= 65535)
        except ValueError:
            valid_host = False
        origin = handler.headers.get("Origin")
        valid_origin = origin is None or origin == "http://" + host
        if not valid_host or not valid_origin:
            handler._send_json({"ok": False, "code": "invalid_host_or_origin", "message": "local same-origin access required"}, 403)
            return False
        if handler._path().startswith("/api/") and not self.store.secrets.authorized(handler.headers.get("Authorization")):
            handler._send_json({"ok": False, "code": "unauthorized", "message": "management credential required"}, 401)
            return False
        return True

    def invalidate(self, reason="external_change"):
        with self._lock:
            self._intent += 1
            for operation in self._operations.values():
                if operation["state"] in {"pending", "running"}:
                    operation["_cancel"].set()
                    future = operation.get("_future")
                    if operation["state"] == "pending" and future is not None and future.cancel():
                        operation.update(state="superseded", error_code="operation_superseded",
                                         completed_monotonic_ns=time.monotonic_ns())
            self._prune()
            intent = self._intent
        self.laya.interrupt(reason)
        return intent

    def _current(self, operation):
        with self._lock:
            return (not self._closed and not operation["_cancel"].is_set() and
                    operation["_intent"] == self._intent and operation["revision"] == self.store.revision and
                    operation["ex_session"] == self.service.goals.ex_session)

    def _assert_current(self, operation):
        if not self._current(operation):
            raise ManagementError("operation_superseded")

    @staticmethod
    def _public(operation):
        return copy.deepcopy({key: value for key, value in operation.items() if not key.startswith("_")})

    def operation(self, operation_id):
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise ManagementError("operation_not_found", 404)
            return self._public(operation)

    def _prune(self):
        completed = sorted((oid for oid, op in self._operations.items() if op["state"] not in {"pending", "running"}),
                           key=lambda oid: self._operations[oid]["completed_monotonic_ns"] or 0)
        for oid in completed[:-128]:
            self._operations.pop(oid, None)

    def _submit(self, kind, work, *, supersede=True, receipt=None):
        with self._lock:
            if self._closed:
                raise ManagementError("management_closed")
            if kind == "service_start":
                for operation in self._operations.values():
                    if (operation["kind"] == kind and operation["state"] in {"pending", "running"} and
                            operation["revision"] == self.store.revision and self._current(operation)):
                        return self.response(operation_id=operation["operation_id"], reused=True)
            active = sum(op["state"] in {"pending", "running"} for op in self._operations.values())
            if active >= self.settings.max_active_operations:
                raise ManagementError("operation_capacity", 429)
            if supersede:
                self.invalidate(kind)
            operation_id = uuid.uuid4().hex
            operation = {"operation_id": operation_id, "kind": kind, "state": "pending",
                         **self.versions(), "backend": self.store.saved["backend"],
                         "service_generation": self.laya.generation,
                         "created_monotonic_ns": time.monotonic_ns(), "completed_monotonic_ns": None,
                         "result": None, "error_code": None, "_cancel": threading.Event(), "_intent": self._intent,
                         "_gate_epoch": self.service.status()["goals"]["gate_epoch"]}
            if receipt is not None:
                operation["stop_receipt"] = copy.deepcopy(receipt)
            self._operations[operation_id] = operation
            operation["_future"] = self._executor.submit(self._run_operation, operation, work)
            return self.response(operation_id=operation_id)

    def _run_operation(self, operation, work):
        try:
            self._assert_current(operation)
            with self._lock:
                operation["state"] = "running"
            result = work(operation)
            self._assert_current(operation)
            with self._lock:
                self._assert_current(operation)
                operation.update(state="succeeded", result=self.sanitize(result))
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code is None:
                # Only framework's bounded fixed error identifiers are exposed.
                text = str(exc)
                code = text if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", text) else "management_operation_failed"
            with self._lock:
                superseded = not self._current(operation) or code in {"operation_superseded", "owned_start_superseded"}
                blocked = code in {"stop_not_proven", "stop_operation_superseded", "ownership_unknown",
                                   "old_request_not_finished", "old_requests_pending", "stop_review_failed", "restart_required"}
                operation.update(state="superseded" if superseded else "blocked" if blocked else "failed", error_code=code)
        finally:
            with self._lock:
                operation["completed_monotonic_ns"] = time.monotonic_ns()
                self._prune()

    def _check_write(self, payload, *, stopping=False):
        if payload.get("ex_session") != self.store.ex_session:
            raise ManagementError("session_conflict")
        if not stopping:
            self.store.check(payload.get("expected_revision"), payload["ex_session"])

    def _idle(self):
        try:
            self.service.management_idle_check()
        except Exception as exc:
            code = str(exc)
            raise ManagementError(code if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code) else "decision_not_idle") from None

    def _wait_stop(self, operation, receipt, *, review=False):
        deadline = time.monotonic() + self.settings.operation_timeout_s
        while time.monotonic() < deadline:
            self._assert_current(operation)
            stop = self.service.status()["stop"]
            if not stop or stop["operation_id"] != receipt["operation_id"]:
                raise ManagementError("stop_operation_superseded")
            if stop["state"] == "proven":
                break
            if stop["state"] == "failed":
                raise ManagementError("stop_not_proven")
            operation["_cancel"].wait(.01)
        else:
            raise ManagementError("stop_not_proven")
        evidence = {"actor_stop": copy.deepcopy(stop)}
        if review:
            future = self.service.management_review(receipt["operation_id"])
            deadline = time.monotonic() + self.settings.operation_timeout_s
            while not future.done() and time.monotonic() < deadline:
                self._assert_current(operation)
                operation["_cancel"].wait(.01)
            if not future.done():
                raise ManagementError("stop_review_failed")
            try:
                evidence["review"] = future.result()
            except Exception:
                raise ManagementError("stop_review_failed") from None
            current = self.service.status()["stop"]
            if not current or current["state"] != "proven":
                raise ManagementError("stop_not_proven")
            evidence["review_stop"] = current
        return evidence

    def _stop_now(self, reason):
        receipt = self.service.management_stop(reason)
        self.invalidate(reason)
        self.service.cancel_backend_wait(receipt["operation_id"])
        return receipt

    def _make_backend(self, config):
        name = config["backend"]
        if name == "laya":
            generation = self.laya.generation
            self.laya.guard(generation)
            transport = (self.settings.laya_transport_factory(self.laya, generation)
                         if self.settings.laya_transport_factory else None)
            backend = create_backend("laya", config=LayaConfig(**config["laya"]), transport=transport,
                                     allow_test_execution=self.settings.allow_test_execution,
                                     trace_queue=self.history.queue)
            return ManagedLayaBackend(backend, self.laya, generation)
        if name == "jev":
            reference = config["secret_ref"]
            return create_backend("jev", config=JevConfig(**config["jev"]),
                                  secret_provider=lambda: self.store.secrets.read(reference))
        return create_backend("mock", **config["mock"])

    def _wait_requests(self, operation):
        deadline = time.monotonic() + self.settings.operation_timeout_s
        while time.monotonic() < deadline:
            self._assert_current(operation)
            if self.service.backend_requests_idle() and self.laya.requests_idle():
                return
            operation["_cancel"].wait(.01)
        raise ManagementError("old_request_not_finished")

    def _apply(self, operation, config, *, context=None):
        self._assert_current(operation)
        self._idle()
        if config["backend"] == "laya":
            if not config["laya"]["enabled"] or not config["laya"]["allow_live_http"]:
                raise ManagementError("laya_inference_not_enabled")
            self.laya.guard(self.laya.generation)
        gate_epoch, revision = context or (operation["_gate_epoch"], operation["framework_config_revision"])
        def factory():
            self._assert_current(operation)
            return self._make_backend(config)
        result = self.service.replace_backend(config["backend"], factory,
            expected_gate_epoch=gate_epoch, expected_config_revision=revision)
        self.store.mark_effective(operation["revision"], config)
        self._assert_current(operation)
        receipt = self.service.status()["stop"]
        self._wait_stop(operation, receipt)
        result["_mode_context"] = (receipt["gate_epoch"], result["config_revision"])
        return result

    def _mode(self, operation, mode, config):
        with self._apply_serial:
            self._assert_current(operation)
            if mode == "disabled":
                return self._wait_stop(operation, operation["stop_receipt"])
            if mode == "execute" and (config["backend"] == "jev" or
                    config["backend"] == "laya" and not self.settings.allow_test_execution):
                raise ManagementError("backend_execute_not_allowed")
            if self.store.effective_revision != operation["revision"]:
                applied = self._apply(operation, config)
            else:
                applied = {"applied": False, "already_effective": True}
                if config["backend"] == "laya":
                    self.laya.guard(self.laya.generation)
            self._assert_current(operation)
            gate_epoch, revision = applied.pop("_mode_context", (operation["_gate_epoch"], operation["framework_config_revision"]))
            self.service.management_set_mode(mode, gate_epoch=gate_epoch, config_revision=revision)
            receipt = self.service.status()["stop"]
            if receipt is not None:
                self._wait_stop(operation, receipt)
            return {"mode": mode, "backend_apply": applied, "new_goal_required": True}

    def _start_service(self, operation):
        with self._service_serial:
            self._assert_current(operation)
            self._idle()
            config = self.laya.start(cancel=operation["_cancel"], is_current=lambda: self._current(operation))
            generation = self.laya.generation
            try:
                self._assert_current(operation)
                self._idle()
                return {"service": self.laya.status(), "decision_mode": self.service.mode,
                        "inference_port": config.port}
            except Exception:
                self.laya.stop(expected_generation=generation)
                raise

    def _stop_service(self, operation):
        with self._service_serial:
            self._wait_stop(operation, operation["stop_receipt"])
            self._assert_current(operation)
            exit_evidence = self.laya.stop(expected_generation=operation["service_generation"])
            self._wait_requests(operation)
            return {"actor_stop": self.service.status()["stop"], "process_exit": exit_evidence}

    def _recover_service(self, operation, config):
        with self._service_serial:
            proof = self._wait_stop(operation, operation["stop_receipt"], review=True)
            self._assert_current(operation)
            exited = self.laya.stop(expected_generation=operation["service_generation"])
            self._wait_requests(operation)
            self._assert_current(operation)
            fresh_config = self.laya.start(cancel=operation["_cancel"], is_current=lambda: self._current(operation), recovery=True)
            generation = self.laya.generation
            try:
                self._assert_current(operation)
                if config["backend"] != "laya":
                    raise ManagementError("recover_requires_saved_laya_backend")
                with self._apply_serial:
                    self._assert_current(operation)
                    applied = self._apply(operation, config,
                        context=(proof["review_stop"]["gate_epoch"], self.service.status()["config_revision"]))
                applied.pop("_mode_context", None)
                proof["installed_stop"] = self.service.status()["stop"]
                return {"actor_stop_evidence": proof, "old_process_exit": exited,
                        "new_service": self.laya.status(), "backend_apply": applied,
                        "mode": "disabled", "new_goal_required": True, "replayed": False,
                        "port": fresh_config.port}
            except Exception:
                self.laya.stop(expected_generation=generation)
                raise

    def _test(self, operation, config):
        self._assert_current(operation)
        name = config["backend"]
        if name == "jev":
            return {"ok": False, "probe_supported": False, "code": "jev_live_probe_not_implemented",
                    "scope": "configuration_only", "cloud_called": False}
        if name == "mock":
            return {"ok": True, "probe_supported": False, "scope": "local_constructor",
                    "network_called": False, "inference_called": False}
        probe_config = copy.deepcopy(config["laya"])
        probe_config.update(enabled=False, allow_live_http=True)
        backend = create_backend("laya", config=LayaConfig(**probe_config))
        try:
            result = backend.probe()
            self._assert_current(operation)
            return {**result, "scope": "GET /health only", "inference_called": False,
                    "service_restart_required": self.laya.status()["restart_required"]}
        finally:
            backend.close()

    def status(self):
        decision = self.service.status()
        with self._lock:
            operation_counts = {state: sum(op["state"] == state for op in self._operations.values())
                                for state in ("pending", "running", "succeeded", "failed", "blocked", "superseded")}
        return self.response(decision=decision, service=self.laya.status(), operations=operation_counts,
                             model_quality={"robot_task_quality_verified": False,
                                            "known_replan_correct": 0, "known_replan_trials": 8})

    def backends(self):
        return [{"name": "mock", "execution_allowed": True, "probe": "local constructor only"},
                {"name": "jev", "execution_allowed": False, "address": "https://" + API_HOST + API_PATH,
                 "probe": "not implemented; no paid request", "modes": ["disabled", "shadow"]},
                {"name": "laya", "execution_allowed": self.settings.allow_test_execution,
                 "execution_scope": "isolated test Actor" if self.settings.allow_test_execution else "shadow only",
                 "probe": "GET /health only", "model": self.store.defaults()["laya"]["model"],
                 "revision": self.store.defaults()["laya"]["revision"],
                 "known_quality_limit": "replan selected start 8/8; robot semantic quality unverified"}]

    def actions(self, query):
        limit = self._page_int(query, "limit", 20, maximum=100)
        cursor = query.get("cursor", [""])[0]
        cid = query.get("command_id", [None])[0]
        status = query.get("status", [None])[0]
        if len(cursor) > 128 or cid is not None and len(cid) > 128:
            raise ManagementError("invalid_action_cursor", 400)
        rows = ((self.service.actions.ledger.get(cid).result(1),) if cid else
                self.service.actions.ledger.list_commands(after_id=cursor, limit=limit).result(1))
        ledger_events = self.service.actions.ledger.events(after_seq=0, limit=500).result(1)
        observed_states = {}
        for event in ledger_events:
            observed_states.setdefault(event.command_id, set()).add(event.status)
        event_window_partial = len(ledger_events) == 500
        items = []
        for row in rows:
            if row is None or status is not None and row.status != status:
                continue
            proof = self.service.actions.ledger.stop_proof(row.command_id, OwnerBinding(row.owner, row.generation)).result(1)
            command = json.loads(row.canonical_command)
            items.append({"command_id": row.command_id, "status": row.status, "owner": row.owner,
                          "generation": row.generation, "event_seq": row.event_seq,
                          "held_resources": list(row.held_resources), "reason_code": row.reason_code,
                          "command": command, "details": row.details,
                          "stop_evidence": to_jsonable(proof) if proof is not None else None,
                          "observed_event_states": sorted(observed_states.get(row.command_id, set())),
                          "event_window_partial": event_window_partial,
                          "facts": {"admitted": "admitted" in observed_states.get(row.command_id, set()),
                                    "actor_accepted": "accepted" in observed_states.get(row.command_id, set()),
                                    "running": "running" in observed_states.get(row.command_id, set()),
                                    "succeeded": row.status == "succeeded"}})
        return {"items": self.sanitize(items), "next_cursor": rows[-1].command_id if rows and rows[-1] else cursor}

    @staticmethod
    def _page_int(query, name, default, maximum=None):
        value = query.get(name, [str(default)])[0]
        if not re.fullmatch(r"[0-9]{1,12}", value):
            raise ManagementError("invalid_pagination", 400)
        value = int(value)
        if maximum and not 1 <= value <= maximum:
            raise ManagementError("invalid_pagination", 400)
        return value

    def before_restore(self):
        receipt = self._stop_now("configuration_restore")
        operation = {"_cancel": threading.Event(), "_intent": self._intent,
                     "revision": self.store.revision, "ex_session": self.store.ex_session}
        self._wait_stop(operation, receipt, review=True)
        self._wait_requests(operation)

    def after_restore(self):
        self.invalidate("configuration_restored")
        self.store.reload_after_restore()
        self.service.management_stop("configuration_restored")

    def _read_body(self, handler):
        length = handler.headers.get("Content-Length", "")
        if not length.isdecimal() or not 0 < int(length) <= 65536 or handler.headers.get("Transfer-Encoding"):
            raise ManagementError("invalid_body_size", 400)
        if handler.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            raise ManagementError("json_content_type_required", 400)
        raw = handler.rfile.read(int(length))
        def unique(pairs):
            value = {}
            for key, val in pairs:
                if key in value:
                    raise ValueError()
                value[key] = val
            return value
        try:
            body = json.loads(raw.decode(), object_pairs_hook=unique,
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
            if not isinstance(body, dict):
                raise ValueError()
            return body
        except Exception:
            raise ManagementError("invalid_json", 400) from None

    def handle_http(self, handler):
        path = handler._path()
        if path != PREFIX and not path.startswith(PREFIX + "/"):
            return False
        subpath = path[len(PREFIX):]
        try:
            if handler.command == "GET":
                query = parse_qs(urlparse(handler.path).query, max_num_fields=8, keep_blank_values=True)
                if any(len(values) != 1 for values in query.values()):
                    raise ManagementError("duplicate_query_field", 400)
                if subpath == "/status":
                    result = self.status()
                elif subpath == "/config":
                    result = self.response(**{k: v for k, v in self.store.get().items() if k not in self.versions()})
                elif subpath == "/backends":
                    result = self.response(backends=self.backends())
                elif subpath.startswith("/operations/"):
                    result = self.response(operation=self.operation(subpath[len("/operations/"):]))
                elif subpath == "/catalog":
                    result = self.response(catalog=self.service.catalog.snapshot().to_dict(),
                                           last_candidate_eligibility=self.service.snapshot(),
                                           decision_mode=self.service.mode)
                elif subpath == "/snapshot":
                    goal, _, phase = self.service.goals.current()
                    pending = self.service.goals.pending_replace
                    result = self.response(current_goal=goal.payload() if goal else None, goal_phase=phase,
                                           pending_goal=pending.payload() if pending else None,
                                           last_built=display(self.sanitize(self.service.snapshot())), last_submitted=self.history.latest(),
                                           last_result=self.history.latest(completed_only=True))
                elif subpath == "/decisions":
                    page = self.history.page(cursor=self._page_int(query, "cursor", 0),
                                             limit=self._page_int(query, "limit", 20, maximum=100),
                                             request_id=query.get("request_id", [None])[0])
                    result = self.response(**page)
                elif subpath == "/actions":
                    result = self.response(**self.actions(query))
                else:
                    raise ManagementError("route_not_found", 404)
                handler._send_json(result)
                return True
            if handler.command != "POST":
                raise ManagementError("method_not_allowed", 405)
            body = self._read_body(handler)
            self._check_write(body, stopping=subpath == "/stop")
            common = {"ex_session", "expected_revision"}
            extra = {"/config": {"config"}, "/secret": {"action", "value"}, "/stop": {"reason"}, "/mode": {"mode"}}
            if set(body) - (common | extra.get(subpath, set())):
                raise ManagementError("unknown_request_field", 400)
            config = copy.deepcopy(self.store.saved)
            if subpath == "/config":
                with self._apply_serial:
                    self._idle()
                    result = self.store.save(body.get("config"), body["expected_revision"], body["ex_session"])
                    self.invalidate("configuration_saved")
                handler._send_json(self.response(**{k: v for k, v in result.items() if k not in self.versions()}))
                return True
            if subpath == "/secret":
                with self._apply_serial:
                    self._idle()
                    result = self.store.update_secret(body.get("action"), body.get("value"),
                                                      body["expected_revision"], body["ex_session"])
                    if body.get("action") != "keep":
                        self.invalidate("secret_saved")
                handler._send_json(self.response(**{k: v for k, v in result.items() if k not in self.versions()}))
                return True
            if subpath == "/test":
                result = self._submit("test", lambda op: self._test(op, config), supersede=False)
            elif subpath == "/mode":
                mode = body.get("mode")
                if not isinstance(mode, str) or mode not in {"disabled", "shadow", "execute"}:
                    raise ManagementError("invalid_mode", 400)
                if mode == "execute" and (config["backend"] == "jev" or config["backend"] == "laya" and not self.settings.allow_test_execution):
                    raise ManagementError("backend_execute_not_allowed")
                if mode != "disabled" and config["backend"] == "laya":
                    self.laya.guard(self.laya.generation)
                receipt = self._stop_now("management_disabled") if mode == "disabled" else None
                result = self._submit("mode", lambda op: self._mode(op, mode, config), receipt=receipt)
            elif subpath == "/stop":
                reason = body.get("reason", "management_stop")
                if not isinstance(reason, str) or not 1 <= len(reason) <= 128:
                    raise ManagementError("invalid_stop_reason", 400)
                receipt = self._stop_now(reason)
                result = self._submit("stop", lambda op: self._wait_stop(op, receipt), receipt=receipt)
            elif subpath == "/service/start":
                result = self._submit("service_start", self._start_service)
            elif subpath == "/service/stop":
                receipt = self._stop_now("model_service_stop")
                result = self._submit("service_stop", self._stop_service, receipt=receipt)
            elif subpath == "/service/recover":
                if config["backend"] != "laya":
                    raise ManagementError("recover_requires_saved_laya_backend")
                receipt = self._stop_now("model_service_recover")
                result = self._submit("service_recover", lambda op: self._recover_service(op, config), receipt=receipt)
            else:
                raise ManagementError("route_not_found", 404)
            handler._send_json(result, 202)
        except ManagementError as exc:
            handler._send_json({"ok": False, **self.versions(), "code": exc.code, "message": exc.code}, exc.status)
        except ValueError:
            handler._send_json({"ok": False, **self.versions(), "code": "invalid_query", "message": "invalid_query"}, 400)
        except Exception as exc:
            code = getattr(exc, "code", "management_request_failed")
            status = 409 if code in {"restart_required", "owned_service_not_ready", "service_not_ready"} else 500
            handler._send_json({"ok": False, **self.versions(), "code": code, "message": code}, status)
        return True

    def close(self):
        self.invalidate("management_shutdown")
        with self._lock:
            self._closed = True
        try:
            if not self.service.status()["closed"]:
                receipt = self.service.management_stop("management_shutdown")
                self.service.cancel_backend_wait(receipt["operation_id"])
            self.laya.stop()
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self.service.configure_management_trace(None)
            self.history.close()
