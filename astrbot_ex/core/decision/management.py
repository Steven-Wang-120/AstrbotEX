"""B08 local management. HTTP data cannot grant execution or process ownership."""
from __future__ import annotations

import copy
import hmac
import json
import math
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
from ..contracts import require_id, require_sequence
from ..actions.ledger import OwnerBinding
from .backends.registry import create_backend
from .backends.jev import API_HOST, API_PATH
from .backends.connection import bearer_headers
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
        self._deployment = deployment
        self._laya = None
        self._laya_credential = None
        self._laya_candidate = None
        self._runtime_stop_uncertain = False
        self._lock = threading.RLock()
        self._probe_sequence = 0
        self._latest_probe = None
        self._latest_probe_context = None
        self._service_serial = threading.Lock()
        self._apply_serial = threading.Lock()
        self._operations = OrderedDict()
        self._completion_time = 0
        self._intent = 0
        self._closed = False
        self.controller = None
        self.connections = None
        self._activation_error = None
        self._installed_backend_binding = None
        self._projection_sequence = 0
        self._projection_identity = None
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="decision-management")
        self.history = RequestHistory(notify=self._history_notice, redact=self.store.secrets.redact)
        self.service.configure_management_trace(self.history.queue)

    def attach_runtime(self, controller, connections):
        # Composition root only; no wire request can supply lifecycle authority.
        self.controller, self.connections = controller, connections

    @property
    def laya(self):
        with self._lock:
            config = self.store.saved
            if not self._selected_owned(config):
                raise ManagementError("selected_service_not_owned", 400)
            deployment = self.store.laya_deployment(config, output=self._deployment.output,
                                                    state_path=self._deployment.state_path)
            reference = config["laya_secret_ref"]
            credential = self.store.secrets.read(reference)
            identity = (reference, credential)
            if self._laya is not None and (self._laya.deployment != deployment or
                                          self._laya_credential != identity):
                status = self._laya.status()
                exited = (status["generation"] is None or any(
                    item.get("generation") == status["generation"] and
                    item.get("exit_confirmed_monotonic_ns") for item in status["history"]))
                if (not exited or status["state"] not in {"stopped", "failed", "disabled"} or
                        status["restart_required"] or status["ownership_unknown"] or
                        not self._laya.requests_idle()):
                    raise ManagementError("owned_deployment_stop_required")
                self._laya = None
            if self._laya is None:
                factory = self.settings.laya_service_factory
                self._laya = (factory(deployment) if factory else OwnedLayaService(deployment,
                    secret_provider=lambda: credential))
                self._laya_credential = identity
            return self._laya

    @staticmethod
    def _owned(config):
        return config["laya"]["service_connection"]["mode"] == "owned"

    @classmethod
    def _selected_owned(cls, config):
        return config["backend"] == "laya" and cls._owned(config)

    def _service_status(self):
        # Read actual ownership without constructing a service for an inactive provider.
        if self._laya is not None:
            return self._laya.status()
        if self._selected_owned(self.store.saved):
            return self.laya.status()
        return {"mode": "external", "managed": False}

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
            if self._latest_probe is not None:
                self._latest_probe["binding"].update(current=False, current_config_verified=False)
        if self._laya is not None:
            self._laya.interrupt(reason)
        return intent

    def _current(self, operation):
        with self._lock:
            return (not self._closed and not operation["_cancel"].is_set() and
                    operation["_intent"] == self._intent and operation["revision"] == self.store.revision and
                    operation["ex_session"] == self.store.ex_session == self.service.goals.ex_session)

    def _assert_current(self, operation):
        if not self._current(operation):
            raise ManagementError("operation_superseded")

    @staticmethod
    def _public(operation):
        return copy.deepcopy({key: value for key, value in operation.items() if not key.startswith("_")})

    def _probe_binding(self, operation, config, credential):
        saved = self.store.saved
        name = config["backend"]
        reference = saved.get("secret_ref" if name == "jev" else "laya_secret_ref")
        saved_key = self.store.secrets.read(reference) if name != "mock" else ""
        binding = {"config_matches_saved": config == saved,
                   "session_matches": operation["ex_session"] == self.store.ex_session == self.service.goals.ex_session,
                   "provider_matches_saved": name == saved["backend"],
                   "credential_matches_saved": name == "mock" or hmac.compare_digest(
                       (credential or "").encode(), (saved_key or "").encode()),
                   "current": self._current(operation) and not self.store.storage_uncertain and
                              operation.get("_probe_sequence") == self._probe_sequence}
        result = operation.get("result") or {}
        binding["current_config_verified"] = (name != "mock" and result.get("ok") is True and
            result.get("inference_ok") is True and all(binding.values()) and self._provider_fault() is None)
        return binding

    def operation(self, operation_id):
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise ManagementError("operation_not_found", 404)
            result = self._public(operation)
            if result["result"] is not None and "_probe_config" in operation:
                result["result"]["binding"] = self._probe_binding(
                    operation, operation["_probe_config"], operation["_probe_credential"])
            return result

    def _prune(self):
        completed = sorted((oid for oid, op in self._operations.items() if op["state"] not in {"pending", "running"}
                            and op["completed_monotonic_ns"] is not None),
                           key=lambda oid: self._operations[oid]["completed_monotonic_ns"] or 0)
        for oid in completed[:-128]:
            self._operations.pop(oid, None)

    def _submit(self, kind, work, *, supersede=True, receipt=None, probe_sequence=None, mode=None):
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
                         "service_generation": self._laya.generation if self._laya is not None else None,
                         "created_monotonic_ns": time.monotonic_ns(), "completed_monotonic_ns": None,
                         "result": None, "error_code": None, "_cancel": threading.Event(), "_intent": self._intent,
                         "_gate_epoch": self.service.status()["goals"]["gate_epoch"]}
            if mode is not None:
                operation["_mode"] = mode
            if probe_sequence is not None:
                operation["_probe_sequence"] = probe_sequence
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
                if "_probe_sequence" in operation and operation["_probe_sequence"] != self._probe_sequence:
                    raise ManagementError("operation_superseded")
                operation.update(state="succeeded", result=self.sanitize(result))
                if "_probe_sequence" in operation:
                    self._latest_probe = {"operation_id": operation["operation_id"],
                                          "ex_session": operation["ex_session"], "revision": operation["revision"],
                                          **copy.deepcopy(operation["result"])}
                    self._latest_probe_context = operation
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
                self._completion_time = max(time.monotonic_ns(), self._completion_time + 1)
                operation["completed_monotonic_ns"] = self._completion_time
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
        if self.controller is not None:
            self.controller.runtime.request_stop()
        receipt = self.service.management_stop(reason)
        self.invalidate(reason)
        self.service.cancel_backend_wait(receipt["operation_id"])
        return receipt

    def _make_backend(self, config, *, probe=False, secret=None):
        name = config["backend"]
        if name == "mock":
            return create_backend("mock", **config["mock"])
        if probe:
            if name == "jev":
                config = copy.deepcopy(config)
                config["jev"]["mode"] = "disabled"
            elif name == "laya":
                config = copy.deepcopy(config)
                config["laya"].update(enabled=False, execution_enabled=False)
        backend_config = self.store.backend_config(name, config)
        reference = config["secret_ref" if name == "jev" else "laya_secret_ref"]
        credential = self.store.secrets.read(reference) if secret is None else secret
        kwargs = {"config": backend_config, "secret_provider": lambda: credential}
        if name == "laya":
            kwargs.update(allow_test_execution=False if probe else self.settings.allow_test_execution,
                          trace_queue=None if probe else self.history.queue)
            if not probe and self._owned(config):
                manager = self.laya
                generation = manager.generation
                manager.guard(generation)
                if self.settings.laya_transport_factory:
                    kwargs["transport"] = self.settings.laya_transport_factory(manager, generation)
                return ManagedLayaBackend(create_backend(name, **kwargs), manager, generation)
        return create_backend(name, **kwargs)

    def _wait_requests(self, operation):
        deadline = time.monotonic() + self.settings.operation_timeout_s
        while time.monotonic() < deadline:
            self._assert_current(operation)
            if self.service.backend_requests_idle() and (self._laya is None or self._laya.requests_idle()):
                return
            operation["_cancel"].wait(.01)
        raise ManagementError("old_request_not_finished")

    def _apply(self, operation, config, *, context=None):
        self._assert_current(operation)
        self._idle()
        if config["backend"] == "laya":
            if not config["laya"]["enabled"] or not config["laya"]["allow_live_http"]:
                raise ManagementError("laya_inference_not_enabled")
            if self._owned(config):
                self.laya.guard(self.laya.generation)
        gate_epoch, revision = context or (operation["_gate_epoch"], operation["framework_config_revision"])
        name = config["backend"]
        reference = config.get("secret_ref" if name == "jev" else "laya_secret_ref")
        credential = self.store.secrets.read(reference) if name in {"jev", "laya"} else None
        def factory():
            self._assert_current(operation)
            return self._make_backend(config, secret=credential)
        result = self.service.replace_backend(config["backend"], factory,
            expected_gate_epoch=gate_epoch, expected_config_revision=revision)
        self.store.mark_effective(operation["revision"], config)
        self._installed_backend_binding = (self.service.backend, operation["revision"],
                                           copy.deepcopy(config), credential)
        self._assert_current(operation)
        receipt = self.service.status()["stop"]
        self._wait_stop(operation, receipt)
        return result

    def _runtime_stop(self, reason, *, transition=None):
        faults = []
        runtime = self.controller.runtime
        unsubscribe = runtime.event_bus.subscribe(lambda event: faults.append(event)
            if event.type in {"plugin_fault", "fault"} else None)
        try:
            if transition is None:
                self.controller.stop(reason)
            else:
                transition()
        except Exception:
            self._runtime_stop_uncertain = True
            raise
        finally:
            unsubscribe()
        slots = runtime.registry.list()
        proven = (runtime.state.value == "idle" and not faults and not any(
            slot.stop_error or slot.state in {"blocked", "starting", "stopping"} or
            slot.runtime_started or slot.runtime_starting or
            slot.action_owner and slot.stop_requested and not slot.stop_proven for slot in slots))
        self._runtime_stop_uncertain = not proven
        if not proven:
            raise ManagementError("runtime_stop_not_proven")
        return {"state": "idle", "plugin_shutdown_proven": True}

    def _complete_stop(self, operation):
        with self._apply_serial:
            # Superseded startup cleanup can advance runtime stop epochs, never authority.
            receipt = self.service.status()["stop"]
            proof = self._wait_stop(operation, receipt)
            self._assert_current(operation)
            stop_error = None
            if self.controller is not None and (self._runtime_stop_uncertain or
                    self.controller.runtime.state.value != "idle" or any(
                    slot.runtime_started or slot.runtime_starting or slot.stop_error
                    for slot in self.controller.runtime.registry.list())):
                try:
                    proof["runtime_shutdown"] = self._runtime_stop("management_stop")
                except Exception as exc:
                    self._activation_error = "activation_stop_uncertain"
                    stop_error = exc
                receipt = self.service.status()["stop"]
                proof["runtime_stop"] = self._wait_stop(operation, receipt)
            if self._laya is not None:
                with self._service_serial:
                    self._assert_current(operation)
                    manager = self._laya
                    proof["process_exit"] = manager.stop(expected_generation=manager.generation)
                    self._wait_requests(operation)
            if stop_error is not None:
                raise stop_error
            self._activation_error = None
            return proof

    @staticmethod
    def _effective_config(config, mode):
        config = copy.deepcopy(config)
        if mode == "execute":
            if config["backend"] == "jev":
                config["jev"].update(mode="execute", allow_live_http=True)
            elif config["backend"] == "laya":
                config["laya"].update(enabled=True, execution_enabled=True, allow_live_http=True)
        return config

    def _mode(self, operation, mode, config):
        if mode == "disabled":
            return self._complete_stop(operation)
        with self._apply_serial:
            self._assert_current(operation)
            config = self._effective_config(config, mode)
            generation, manager = None, None
            runtime_attempted = False
            try:
                if mode == "execute":
                    if self.controller is None:
                        raise ManagementError("runtime_controller_unavailable")
                    name = config["backend"]
                    if name in {"jev", "laya"}:
                        reference = config["secret_ref" if name == "jev" else "laya_secret_ref"]
                        try:
                            bearer_headers(config[name]["service_connection"]["auth_mode"],
                                           lambda: self.store.secrets.read(reference))
                        except ValueError:
                            raise ManagementError("missing_or_invalid_secret", 400) from None
                    if config["backend"] == "laya" and self._owned(config):
                        with self._service_serial:
                            self._assert_current(operation)
                            manager = self.laya
                            if self._laya_candidate == (manager, manager.generation):
                                manager.stop(expected_generation=manager.generation)
                                self._laya_candidate = None
                            if manager.status()["state"] == "ready":
                                manager.guard(manager.generation)
                            else:
                                manager.start(cancel=operation["_cancel"], is_current=lambda: self._current(operation))
                                generation = manager.generation
                    self._assert_current(operation)
                    if self._runtime_stop_uncertain:
                        raise ManagementError("runtime_stop_not_proven")
                    if self.service.status()["control_mode"] != "decision":
                        self._runtime_stop("control_mode_change",
                            transition=lambda: self.controller.change_mode("decision"))
                    receipt = self.service.status()["stop"]
                    if receipt is not None:
                        self._wait_stop(operation, receipt)
                self._assert_current(operation)
                status = self.service.status()
                context = ((status["goals"]["gate_epoch"], status["config_revision"]) if mode == "execute" else
                           (operation["_gate_epoch"], operation["framework_config_revision"]))
                installed_generation = (getattr(self.service.backend, "generation", None)
                    if config["backend"] == "laya" and self._owned(config) else None)
                if (self.store.effective_revision != operation["revision"] or self.store.effective != config or
                        self._selected_owned(config) and self._laya is not None and
                        installed_generation != self._laya.generation):
                    applied = self._apply(operation, config, context=context)
                else:
                    applied = {"applied": False, "already_effective": True}
                    if config["backend"] == "laya" and self._owned(config):
                        self.laya.guard(self.laya.generation)
                self._assert_current(operation)
                if mode == "execute":
                    runtime_attempted = True
                    self.controller.start()
                    self._assert_current(operation)
                    if (self.controller.runtime.state.value != "running" or
                            self.service.status()["control_mode"] != "decision"):
                        raise ManagementError("runtime_start_failed")
                status = self.service.status()
                self._assert_current(operation)
                if self._selected_owned(config):
                    with self._service_serial:
                        self._assert_current(operation)
                        manager = self.laya
                        manager.guard(manager.generation)
                if self._runtime_stop_uncertain:
                    raise ManagementError("runtime_stop_not_proven")
                gate_epoch, revision = (status["goals"]["gate_epoch"], status["config_revision"])
                self.service.management_set_mode(mode, gate_epoch=gate_epoch, config_revision=revision)
                receipt = self.service.status()["stop"]
                if receipt is not None:
                    self._wait_stop(operation, receipt)
                self._assert_current(operation)
                if mode == "execute" and not self._running():
                    raise ManagementError("activation_not_running")
                self._activation_error = None
                return {"mode": mode, "backend_apply": applied, "new_goal_required": True}
            except Exception:
                # Apply serialization excludes a newer activation throughout cleanup.
                current = self._current(operation)
                if current or runtime_attempted:
                    try:
                        if current:
                            receipt = self.service.management_stop("activation_failed")
                            self._wait_stop(operation, receipt)
                        if self.controller is not None and runtime_attempted:
                            self._runtime_stop("activation_failed")
                        if manager is not None and generation is not None:
                            with self._service_serial:
                                if manager.generation == generation:
                                    manager.stop(expected_generation=generation)
                    except Exception:
                        self._activation_error = "activation_stop_uncertain"
                    else:
                        if current:
                            self._activation_error = "activation_failed"
                elif manager is not None and generation is not None:
                    with self._service_serial:
                        if manager.generation == generation:
                            manager.stop(expected_generation=generation)
                raise

    def _running(self):
        return (self.controller is not None and self.controller.runtime.state.value == "running" and
                self.service.status()["control_mode"] == "decision" and self.service.mode == "execute" and
                getattr(self.service.backend, "execution_allowed", False) is True)

    def _start_service(self, operation):
        with self._service_serial:
            self._assert_current(operation)
            self._idle()
            manager = self.laya
            config = manager.start(cancel=operation["_cancel"], is_current=lambda: self._current(operation))
            generation = manager.generation
            try:
                self._assert_current(operation)
                self._idle()
                return {"service": manager.status(), "decision_mode": self.service.mode,
                        "inference_port": config.port}
            except Exception:
                manager.stop(expected_generation=generation)
                raise

    def _stop_service(self, operation):
        with self._service_serial:
            self._wait_stop(operation, operation["stop_receipt"])
            self._assert_current(operation)
            manager = self.laya
            exit_evidence = manager.stop(expected_generation=operation["service_generation"])
            self._wait_requests(operation)
            return {"actor_stop": self.service.status()["stop"], "process_exit": exit_evidence}

    def _recover_service(self, operation, config):
        manager, generation = None, None
        try:
            # Never nest apply under service: cold loading permits configuration saves.
            with self._service_serial:
                proof = self._wait_stop(operation, operation["stop_receipt"], review=True)
                self._assert_current(operation)
                manager = self.laya
                exited = manager.stop(expected_generation=operation["service_generation"])
                self._wait_requests(operation)
                self._assert_current(operation)
                fresh_config = manager.start(cancel=operation["_cancel"],
                    is_current=lambda: self._current(operation), recovery=True)
                generation = manager.generation
                self._laya_candidate = (manager, generation)
                if not self._current(operation):
                    manager.stop(expected_generation=generation)
                    raise ManagementError("operation_superseded")
            with self._apply_serial, self._service_serial:
                self._assert_current(operation)
                if self._laya is not manager or manager.generation != generation:
                    raise ManagementError("operation_superseded")
                manager.guard(generation)
                self._laya_candidate = None
                applied = self._apply(operation, config,
                    context=(proof["review_stop"]["gate_epoch"], self.service.status()["config_revision"]))
                proof["installed_stop"] = self.service.status()["stop"]
                return {"actor_stop_evidence": proof, "old_process_exit": exited,
                        "new_service": manager.status(), "backend_apply": applied,
                        "mode": "disabled", "new_goal_required": True, "replayed": False,
                        "port": fresh_config.port}
        except Exception:
            if manager is not None and generation is not None:
                with self._service_serial:
                    if manager.generation == generation:
                        manager.stop(expected_generation=generation)
                    if self._laya_candidate == (manager, generation):
                        self._laya_candidate = None
            raise

    def _test(self, operation, config, credential, sequence):
        self._assert_current(operation)
        name = config["backend"]
        probe_config = copy.deepcopy(config)
        if name != "mock":
            # Explicit test authorization is temporary and cannot open execution.
            probe_config[name]["allow_live_http"] = True
        backend = self._make_backend(probe_config, probe=True, secret=credential)
        try:
            result = backend.probe() if name != "mock" else {
                "ok": True, "probe_supported": False, "network_called": False,
                "inference_called": False, "inference_ok": False, "error_code": None}
            with self._lock:
                self._assert_current(operation)
                if sequence != self._probe_sequence:
                    raise ManagementError("operation_superseded")
                operation["_probe_config"] = copy.deepcopy(config)
                operation["_probe_credential"] = credential
                binding = self._probe_binding(operation, config, credential)
                binding["current_config_verified"] = (name != "mock" and result.get("ok") is True and
                    result.get("inference_ok") is True and all(v for k, v in binding.items()
                                                               if k != "current_config_verified"))
                code = result.get("error_code")
                return {**result, "provider": name, "scope": "fixed_wait_inference" if name != "mock" else "local_constructor",
                        "error_category": "authentication" if code in {"http_401", "http_403", "missing_secret", "missing_or_invalid_secret"} else
                                          "inference" if code else None,
                        "binding": binding}
        finally:
            backend.close()

    def status(self):
        decision = self.service.status()
        with self._lock:
            operation_counts = {state: sum(op["state"] == state for op in self._operations.values())
                                for state in ("pending", "running", "succeeded", "failed", "blocked", "superseded")}
            probe = copy.deepcopy(self._latest_probe)
            if probe:
                operation = self._latest_probe_context
                probe["binding"] = self._probe_binding(
                    operation, operation["_probe_config"], operation["_probe_credential"])
        return self.response(decision=decision, service=self._service_status(), operations=operation_counts,
                             probe=probe, storage_uncertain=self.store.storage_uncertain)

    @staticmethod
    def _unavailable_task():
        return {"available": False, "phase": "unavailable", "title": "", "current_goal": None,
                "completed": 0, "total": 0, "can_cancel": False, "updated_at": 0,
                "message": "Task projection unavailable"}

    def _task_projection(self, *, with_context=False):
        unavailable = self._unavailable_task()
        def result(task, context=None):
            return (task, context) if with_context else task
        if self.connections is None:
            return result(unavailable)
        with self._lock:
            self._projection_sequence += 1
            sequence = self._projection_sequence
            session, intent = self.store.ex_session, self._intent
        try:
            route = self.connections.decision_connection_identity()
            connection = route[0]
            value, binary = self.connections.request_connection(connection, "text", "task.projection.get",
                {"schema_version": 1, "ex_session": session}, timeout_sec=1)
            fields = {"schema_version", "ex_session", "robot_id", "task_id", "generation", "turn_id",
                      *unavailable}
            if binary is not None or not isinstance(value, dict) or set(value) != fields:
                return result(unavailable)
            if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["ex_session"] != session:
                return result(unavailable)
            if value["available"] is not True or value["can_cancel"] is not False:
                return result(unavailable)
            for key, limit in (("title", 160), ("message", 256), ("phase", 32)):
                if not isinstance(value[key], str) or len(value[key]) > limit or any(ord(c) < 32 for c in value[key]):
                    return result(unavailable)
            # These are TaskStore/TaskCoordinator task statuses, not EX Goal phases.
            if value["phase"] not in {"idle", "planning", "executing", "waiting_input", "lease_lost",
                                      "resume_review", "completed", "canceling", "canceled"}:
                return result(unavailable)
            if (value["current_goal"] is not None and (not isinstance(value["current_goal"], str) or
                    not value["current_goal"].strip() or len(value["current_goal"]) > 160 or
                    any(ord(c) < 32 for c in value["current_goal"]))):
                return result(unavailable)
            if any(type(value[key]) is not int or not 0 <= value[key] <= 10000 for key in ("completed", "total")):
                return result(unavailable)
            if value["completed"] > value["total"]:
                return result(unavailable)
            updated = value["updated_at"]
            if type(updated) not in (int, float) or not math.isfinite(updated) or not 0 <= updated <= time.time() + 60:
                return result(unavailable)
            require_id(value["robot_id"], "robot_id")
            for key in ("task_id", "turn_id"):
                if value[key] is not None:
                    require_id(value[key], key)
            generation = value["generation"]
            if generation is not None:
                require_sequence(generation, "generation", minimum=1)
            if value["task_id"] is None:
                if (value["phase"] != "idle" or value["title"] or
                        any(value[k] is not None for k in ("generation", "turn_id", "current_goal")) or
                        value["completed"] or value["total"]):
                    return result(unavailable)
            elif generation is None or value["phase"] == "idle":
                return result(unavailable)
            with self._lock:
                context = (session, intent, route, sequence)
                if not self._projection_current(context):
                    return result(unavailable)
                previous = self._projection_identity
                identity = (session, route, value["robot_id"], value["task_id"], generation, value["turn_id"], updated)
                closed_turn = False
                if previous and previous[:4] == identity[:4] and previous[4] is not None:
                    if (generation is None or generation < previous[4] or updated < previous[6] or
                            generation == previous[4] and value["turn_id"] is not None and
                            (previous[5] != value["turn_id"] or previous[7])):
                        return result(unavailable)
                    if generation == previous[4]:
                        closed_turn = previous[7] or previous[5] is not None and value["turn_id"] is None
                self._projection_identity = (*identity, closed_turn)
                return result({key: value[key] for key in unavailable}, context)
        except Exception:
            return result(unavailable)

    def _projection_current(self, context):
        if context is None:
            return False
        session, intent, route, sequence = context
        try:
            return (session == self.store.ex_session == self.service.goals.ex_session and
                    intent == self._intent and sequence == self._projection_sequence and
                    route == self.connections.decision_connection_identity())
        except Exception:
            return False

    def _provider_fault(self):
        if self._selected_owned(self.store.saved) and self._laya is not None:
            if self._laya.status()["restart_required"]:
                return "restart_required"
        saved, effective = self.store.saved, self.store.effective
        binding = self._installed_backend_binding
        if (effective is None or self.store.effective_revision != self.store.revision or
                effective["backend"] != saved["backend"] or binding is None):
            return None
        backend, revision, installed, credential = binding
        if (backend is not self.service.backend or revision != self.store.revision or installed != effective or
                effective not in (saved, self._effective_config(saved, "execute"))):
            return None
        name = saved["backend"]
        if name in {"jev", "laya"}:
            reference = saved["secret_ref" if name == "jev" else "laya_secret_ref"]
            current_credential = self.store.secrets.read(reference)
            if not hmac.compare_digest((credential or "").encode(), (current_credential or "").encode()):
                return None
        backend_status = getattr(backend, "status", lambda: {})()
        if backend_status.get("restart_required"):
            return "restart_required"
        if self._selected_owned(self.store.saved) and self._laya is not None:
            status = self._laya.status()
            if status["restart_required"]:
                return "restart_required"
            if status["state"] == "ready":
                try:
                    process = self._laya.owned_process_handle(expected_generation=status["generation"])
                    if process.poll() is not None:
                        return "owned_service_exited"
                except Exception:
                    return "ownership_unknown"
        record = getattr(backend, "last_record", None) if name == "jev" else None
        code = getattr(record, "reject_code", None) if name == "jev" else backend_status.get("error_code")
        if code in {"http_401", "http_403", "http_422", "http_429", "http_529", "http_error", "http_redirect",
                    "tls_failure", "transport_failure", "http_failure", "deadline_exceeded", "retry_exceeds_deadline",
                    "missing_or_invalid_secret", "missing_secret"}:
            return code
        return None

    def view(self):
        task, projection_context = self._task_projection(with_context=True)
        status = self.service.status()
        with self._lock:
            if not self._projection_current(projection_context):
                task = self._unavailable_task()
            active = [op for op in self._operations.values() if self._current(op) and op["state"] in {"pending", "running"}]
            stopping = any(op["kind"] in {"stop", "service_stop"} or op.get("_mode") == "disabled" for op in active)
            starting = any(op["kind"] == "mode" and op.get("_mode") == "execute" for op in active)
            probe = self._latest_probe
            binding = self._probe_binding(self._latest_probe_context,
                self._latest_probe_context["_probe_config"], self._latest_probe_context["_probe_credential"]) if probe else None
            connection_state, code = "unverified", None
            if any(op["kind"] == "test" for op in active):
                connection_state = "testing"
            elif binding and binding["current_config_verified"]:
                connection_state = "verified"
            elif binding and all(binding[key] for key in ("current", "config_matches_saved", "session_matches",
                                                          "provider_matches_saved", "credential_matches_saved")):
                connection_state, code = "failed", probe.get("error_code")
            provider_fault = self._provider_fault()
            if provider_fault:
                connection_state, code = "disconnected", provider_fault
            stop = status["stop"]
            uncertain = (self.store.storage_uncertain or self._runtime_stop_uncertain or
                         self._activation_error == "activation_stop_uncertain" or
                         stop is not None and stop["state"] == "failed")
            quarantine = (self._laya is not None and self._laya.status()["restart_required"])
            fault = self.controller is not None and self.controller.runtime.state.value == "fault"
            state = ("stopping" if stopping else "starting" if starting else "uncertain" if uncertain else
                     "failed" if quarantine or fault or provider_fault else "running" if self._running() else
                     "failed" if self._activation_error else "disabled")
            runtime_live = self.controller is not None and self.controller.runtime.state.value != "idle"
            can_start = (self.store.saved["backend"] in {"jev", "laya"} and state in {"disabled", "failed"} and
                         status["mode"] == "disabled" and not runtime_live and not status["blocked"] and
                         not quarantine and not fault and provider_fault not in {"restart_required", "ownership_unknown"})
            return {"schema_version": 1, "ex_session": self.store.ex_session, "revision": self.store.revision,
                    "effective_revision": self.store.effective_revision, "provider": self.store.saved["backend"],
                    "connection": {"state": connection_state, "code": code,
                                   "message": {"unverified": "Connection not tested", "testing": "Testing connection",
                                               "verified": "Saved connection verified", "failed": "Connection test failed",
                                               "disconnected": "Provider unavailable"}[connection_state]},
                    "ex": {"state": state, "can_start": can_start,
                           "can_stop": starting or runtime_live or status["mode"] != "disabled" or uncertain,
                           "message": "EX " + state}, "task": task,
                    "error": {"code": "stop_uncertain", "message": "Stop not proven"} if uncertain else
                             {"code": "runtime_fault", "message": "Runtime fault"} if fault else
                             {"code": provider_fault, "message": "Provider unavailable"} if provider_fault else
                             {"code": self._activation_error, "message": "Activation failed"} if self._activation_error else None}

    def backends(self):
        defaults = self.store.defaults()
        return [{"name": "mock", "execution_allowed": True, "probe": "local constructor only"},
                {"name": "jev", "address": "https://" + API_HOST + API_PATH,
                 "probe": "fixed wait-only inference", "model": defaults["jev"]["service_connection"]["model"],
                 "modes": ["disabled", "shadow", "execute"], "execution_requires": "production backend gate"},
                {"name": "laya", "probe": "health and fixed wait-only inference",
                 "model": defaults["laya"]["service_connection"]["model"],
                 "revision": defaults["laya"]["revision"], "connection_modes": ["external", "owned"],
                 "execution_requires": "production backend gate; generation guard for owned"}]

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
                if subpath == "/view":
                    result = self.view()
                elif subpath == "/status":
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
            extra = {"/config": {"config"}, "/secret": {"provider", "action", "value"},
                     "/test": {"provider", "config", "value"}, "/stop": {"reason"}, "/mode": {"mode"}}
            if set(body) - (common | extra.get(subpath, set())):
                raise ManagementError("unknown_request_field", 400)
            config = copy.deepcopy(self.store.saved)
            if subpath == "/config":
                with self._apply_serial, self._lock:
                    self._idle()
                    before = self.store.revision
                    try:
                        result = self.store.save(body.get("config"), body["expected_revision"], body["ex_session"])
                    finally:
                        if self.store.revision != before or self.store.storage_uncertain:
                            self.invalidate("configuration_saved")
                handler._send_json(self.response(**{k: v for k, v in result.items() if k not in self.versions()}))
                return True
            if subpath == "/secret":
                provider = body.get("provider", "jev")
                if not isinstance(provider, str) or provider not in {"jev", "laya"}:
                    raise ManagementError("invalid_secret_provider", 400)
                with self._apply_serial, self._lock:
                    self._idle()
                    before = self.store.revision
                    try:
                        result = self.store.update_secret(body.get("action"), body.get("value"),
                            body["expected_revision"], body["ex_session"], provider=provider)
                    finally:
                        if self.store.revision != before or self.store.storage_uncertain:
                            self.invalidate("secret_saved")
                handler._send_json(self.response(**{k: v for k, v in result.items() if k not in self.versions()}))
                return True
            if subpath == "/test":
                with self._lock:
                    # Capture CAS, selection and credential together; probe never applies.
                    self._check_write(body)
                    config = self.store.validate(body["config"] if "config" in body else self.store.saved)
                    if "provider" in body:
                        provider = body["provider"]
                        if not isinstance(provider, str) or provider not in {"jev", "laya"}:
                            raise ManagementError("invalid_secret_provider", 400)
                        config["backend"] = provider
                    provider = config["backend"]
                    credential = body.get("value")
                    if credential is not None:
                        credential = self.store.secrets.remember(credential)
                    else:
                        reference = config["secret_ref" if provider == "jev" else "laya_secret_ref"]
                        credential = self.store.secrets.read(reference) if provider != "mock" else ""
                    sequence = self._probe_sequence + 1
                    result = self._submit("test", lambda op: self._test(op, config, credential, sequence),
                                          supersede=False, probe_sequence=sequence)
                    self._probe_sequence = sequence
            elif subpath == "/mode":
                mode = body.get("mode")
                if not isinstance(mode, str) or mode not in {"disabled", "shadow", "execute"}:
                    raise ManagementError("invalid_mode", 400)
                if mode != "disabled" and config["backend"] == "laya" and self._owned(config) and self._laya is not None:
                    if self._laya.status()["restart_required"]:
                        raise ManagementError("restart_required")
                receipt = self._stop_now("management_disabled") if mode == "disabled" else None
                result = self._submit("mode", lambda op: self._mode(op, mode, config), receipt=receipt, mode=mode)
            elif subpath == "/stop":
                reason = body.get("reason", "management_stop")
                if not isinstance(reason, str) or not 1 <= len(reason) <= 128:
                    raise ManagementError("invalid_stop_reason", 400)
                receipt = self._stop_now(reason)
                result = self._submit("stop", self._complete_stop, receipt=receipt)
            elif subpath == "/service/start":
                if config["backend"] != "laya":
                    raise ManagementError("selected_service_not_owned", 400)
                if not self._owned(config):
                    raise ManagementError("external_service_not_owned", 400)
                result = self._submit("service_start", self._start_service)
            elif subpath == "/service/stop":
                if config["backend"] != "laya":
                    raise ManagementError("selected_service_not_owned", 400)
                if not self._owned(config):
                    raise ManagementError("external_service_not_owned", 400)
                receipt = self._stop_now("model_service_stop")
                result = self._submit("service_stop", self._stop_service, receipt=receipt)
            elif subpath == "/service/recover":
                if config["backend"] != "laya":
                    raise ManagementError("selected_service_not_owned", 400)
                if not self._owned(config):
                    raise ManagementError("external_service_not_owned", 400)
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
            if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code):
                code = "management_request_failed"
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
            if self._laya is not None:
                self._laya.stop()
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self.service.configure_management_trace(None)
            self.history.close()
