"""Explicit ownership of one pinned loopback Laya child.

This supervisor never imports model libraries. Process ownership comes only
from this object's Popen handle. Persisted PIDs are audit data, not authority.
EX stop evidence and backend replacement remain the management service's job.
"""
from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from .backends.laya import BUNDLE_SHA, LayaBackend, LayaConfig
from .models import DecisionSnapshot


class OwnedLayaError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Deployment:
    """Trusted composition only. None of these paths comes from an HTTP body."""

    python: Path
    cache: Path
    output: Path
    port: int = 8769
    device: str = "cuda"
    state_path: Path | None = None
    startup_timeout_s: float = 120
    warmup_deadline_ms: int = 30000
    terminate_timeout_s: float = 10
    kill_timeout_s: float = 5

    def __post_init__(self):
        for name in ("python", "cache", "output"):
            object.__setattr__(self, name, Path(getattr(self, name)))
        state = self.state_path or self.output / "service-state.json"
        object.__setattr__(self, "state_path", Path(state))
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise OwnedLayaError("invalid_deployment")
        if self.device not in {"cuda", "cpu"}:
            raise OwnedLayaError("invalid_deployment")
        # Cold checkpoint loading can wait on storage. This trusted deployment
        # budget is separate from the bounded inference and process-stop budgets.
        if type(self.startup_timeout_s) not in (int, float) or not 0 < self.startup_timeout_s <= 3600:
            raise OwnedLayaError("invalid_deployment")
        for value in (self.terminate_timeout_s, self.kill_timeout_s):
            if type(value) not in (int, float) or not 0 < value <= 300:
                raise OwnedLayaError("invalid_deployment")
        if type(self.warmup_deadline_ms) is not int or not 1 <= self.warmup_deadline_ms <= 3600000:
            raise OwnedLayaError("invalid_deployment")


class _ColdWarmupBackend(LayaBackend):
    """Trusted startup-only wait; public/hot LayaConfig limits stay unchanged."""
    def __init__(self, config, budget_ms):
        super().__init__(config)
        self._cold_budget_ms=budget_ms

    def _begin(self, started, *, probe=False):
        call=super()._begin(started,probe=probe)
        call.deadline=started+self._cold_budget_ms/1000.
        return call


def fixed_warmup_snapshot():
    """The B07 move warmup, without a Goal submission or an Actor command."""
    sid = "real-move-warmup"
    return DecisionSnapshot.parse({
        "schema_version": 1, "snapshot_id": sid,
        "created_monotonic_ns": time.monotonic_ns(),
        "versions": {"ex_session": "laya-real-validation", "goal_revision": 1,
                     "config_revision": 1, "catalog_revision": 1,
                     "environment_generation": 1, "gate_epoch": 1,
                     "plugin_generations": {"arm": 1}},
        "goal": {"task_id": "laya-real-task", "goal_id": sid,
                 "goal_text_en": "Move safely.", "allowed_actions": ["arm.move.v1"],
                 "parameters": {"arm.move.v1": {"meters": 1}}},
        "observations": [], "owners": [{"owner": "arm", "plugin_generation": 1,
        "status": "available", "candidates": [
            {"option_id": sid + ":wait", "kind": "wait", "description": "Wait without dispatch or extending an action lease.", "eligible": True},
            {"option_id": sid + ":replan", "kind": "request_replan", "description": "Ask AEB to supply new goal parameters; never choose another goal.", "eligible": True},
            {"option_id": sid + ":start", "kind": "start", "description": "Move safely", "action_id": "arm.move.v1", "eligible": True},
        ]}],
    })


class OwnedLayaService:
    """One owned generation, one decide permit, sticky generation quarantine.

    start/stop are serialized. interrupt is nonblocking with respect to that
    operation lock, so a stop can revoke a startup's local health/warmup wait.
    Only explicit recovery can move from a quarantined, exited generation to
    a fresh generation. Recovery does not authorize an EX mode or Goal.
    """

    def __init__(self, deployment, *, process_factory=None, probe_factory=LayaBackend,
                 warmup=None, clock=time.monotonic):
        if not isinstance(deployment, Deployment):
            raise OwnedLayaError("invalid_deployment")
        self.deployment = deployment
        self._process_factory = process_factory or subprocess.Popen
        self._probe_factory = probe_factory
        self._warmup = warmup
        self._clock = clock
        self._lock = threading.RLock()
        self._operations = threading.RLock()
        self._session = uuid.uuid4().hex
        self._serial = 0
        self._process = None
        self._log = None
        self._generation = None
        self._state = "stopped"
        self._error = None
        self._health = None
        self._history = deque(maxlen=128)
        self._quarantined = False
        self._ownership_unknown = False
        self._intent = None
        self._startup_backend = None
        self._permit_backend = None
        self._permit_running = False
        self._draining = False
        self._load_metadata()

    @property
    def generation(self):
        with self._lock:
            return self._generation

    @property
    def history(self):
        with self._lock:
            return copy.deepcopy(list(self._history))

    def status(self):
        with self._lock:
            return {"state": self._state, "generation": self._generation,
                    "owned": self._process is not None,
                    "ownership_unknown": self._ownership_unknown,
                    "restart_required": self._quarantined or self._ownership_unknown,
                    "error_code": self._error, "draining": self._draining,
                    "health": copy.deepcopy(self._health), "history": self.history,
                    "port": self.deployment.port, "model": "typed-decisions",
                    "revision": BUNDLE_SHA}

    def owned_process_handle(self, *, expected_generation=None):
        """Trusted test/composition access only; never expose this through HTTP."""
        with self._lock:
            if self._ownership_unknown:
                raise OwnedLayaError("ownership_unknown")
            if expected_generation is not None and expected_generation != self._generation:
                raise OwnedLayaError("stale_service_generation")
            if self._process is None:
                raise OwnedLayaError("service_not_owned")
            return self._process

    def _load_metadata(self):
        path = self.deployment.state_path
        if not path.exists() and not path.is_symlink():
            return
        try:
            if path.is_symlink() or path.stat().st_size > 1048576:
                raise ValueError("unsafe metadata")
            raw = json.loads(path.read_text())
            if not isinstance(raw, dict) or raw.get("schema_version") != 1:
                raise ValueError("invalid metadata")
            items = raw.get("history", [])
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ValueError("invalid history")
            self._history.extend(items[-128:])
            self._generation = raw.get("generation")
            self._quarantined = raw.get("restart_required") is True
            current = self._history[-1] if self._history else {}
            if self._generation is not None and not current.get("exit_confirmed_monotonic_ns"):
                self._ownership_unknown = True
                self._quarantined = True
                self._state = "restart_required"
                self._error = "ownership_unknown"
            elif self._quarantined:
                self._state = "restart_required"
                self._error = raw.get("error_code") or "restart_required"
        except (OSError, ValueError, TypeError):
            self._ownership_unknown = True
            self._quarantined = True
            self._state = "restart_required"
            self._error = "ownership_unknown"

    def _persist_locked(self):
        path = self.deployment.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise OwnedLayaError("unsafe_service_state")
        raw = json.dumps({"schema_version": 1, "generation": self._generation,
                          "restart_required": self._quarantined,
                          "error_code": self._error, "history": list(self._history)},
                         ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
        fd, temporary = tempfile.mkstemp(prefix=".laya-state-", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _environment(self):
        env = dict(os.environ)
        env.update(LAYA_HOST="127.0.0.1", LAYA_PORT=str(self.deployment.port),
                   LAYA_DEVICE=self.deployment.device, LAYA_MODELS="typed-decisions",
                   LAYA_PRELOAD="1", LAYA_AUTO_TASK="0", LAYA_MAX_LOADED="1",
                   LAYA_MAX_CONCURRENT="1", LAYA_MAX_TOKEN_BUDGET="1024",
                   LAYA_REVISION=BUNDLE_SHA, HF_HUB_CACHE=str(self.deployment.cache),
                   HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                   TORCH_COMPILE_DISABLE="1", USE_TF="0", TOKENIZERS_PARALLELISM="false",
                   PYTHONDONTWRITEBYTECODE="1")
        env.pop("LAYA_API_KEY", None)
        env["LAYA_SHA256_DIGESTS"] = json.dumps({"typed-decisions": {
            "model.safetensors": "4fa56de72383a9d3efa9cfa78955733c81b9fc8067a587ca4beb82c78107a24e",
            "tokenizer/tokenizer.json": "6c8aaa9a542084f2457eab775d4eeb51f92a70c0fd9de28d5edb0ddec3c08d30",
        }})
        return env

    @staticmethod
    def _health_summary(value):
        if not isinstance(value, dict):
            return None
        summary = {key: copy.deepcopy(value[key]) for key in
                   ("status", "device", "device_is_preference") if key in value}
        loaded = value.get("loaded", [])
        summary["loaded"] = ["typed-decisions"] if "typed-decisions" in loaded else []
        for name in ("revisions", "checkpoint_devices", "cpu_fallbacks"):
            mapping = value.get(name, {})
            summary[name] = {"typed-decisions": copy.deepcopy(mapping["typed-decisions"])} if isinstance(mapping, dict) and "typed-decisions" in mapping else {}
        return summary

    def interrupt(self, reason="superseded", *, expected_generation=None):
        """Revoke a startup without waiting for its process operation lock."""
        with self._lock:
            if expected_generation is not None and expected_generation != self._generation:
                raise OwnedLayaError("stale_service_generation")
            intent, backend = self._intent, self._startup_backend
            if intent is not None:
                intent.set()
        if backend is not None:
            backend.cancel()

    def _check_intent(self, intent, cancel, is_current):
        if intent.is_set() or (cancel is not None and cancel.is_set()):
            raise OwnedLayaError("superseded")
        if is_current is not None and not is_current():
            intent.set()
            raise OwnedLayaError("superseded")

    def _set_startup_backend(self, backend, intent):
        with self._lock:
            self._startup_backend = backend
            canceled = intent.is_set()
        if canceled:
            backend.cancel()

    def requests_idle(self):
        """Observe adapter worker exit; close/cancel alone cannot clear a permit."""
        with self._lock:
            backend, running = self._permit_backend, self._permit_running
        if backend is None:
            return True
        if running or backend.status().get("busy") is True:
            return False
        with self._lock:
            if self._permit_backend is backend and not self._permit_running:
                self._permit_backend = None
                self._draining = False
            return self._permit_backend is None

    def start(self, *, cancel=None, is_current=None, recovery=False, warmup=True):
        with self._operations:
            if not self.requests_idle():
                raise OwnedLayaError("old_requests_pending")
            intent = threading.Event()
            self._check_intent(intent, cancel, is_current)
            with self._lock:
                if self._ownership_unknown:
                    raise OwnedLayaError("ownership_unknown")
                if self._process is not None and self._process.poll() is None:
                    raise OwnedLayaError("old_owned_service_still_running")
                if self._process is not None and self._history and not self._history[-1].get("exit_confirmed_monotonic_ns"):
                    raise OwnedLayaError("old_exit_not_confirmed")
                if self._quarantined and not recovery:
                    raise OwnedLayaError("restart_required")
                self._intent = intent
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", self.deployment.port))
                except OSError:
                    with self._lock:
                        self._intent = None
                    raise OwnedLayaError("service_port_occupied") from None
            self.deployment.output.mkdir(parents=True, exist_ok=True)
            started = time.monotonic_ns()
            with self._lock:
                self._serial += 1
                generation = self._session + ":" + str(self._serial)
                self._generation = generation
                self._state, self._error, self._health = "starting", None, None
                self._quarantined = False
                self._process = None
                log = (self.deployment.output / f"laya-serve-{self._session}-{self._serial}.txt").open("w")
                self._log = log
                item = {"generation": generation, "started_monotonic_ns": started,
                        "weight_revision": BUNDLE_SHA, "requested_device": self.deployment.device}
                self._history.append(item)
            process = None
            probe_backend = None
            try:
                with self._lock:
                    self._persist_locked()  # Persist intent before spawn; a crash cannot lose ownership.
                self._check_intent(intent, cancel, is_current)
                process = self._process_factory([str(self.deployment.python), "-m", "laya.serve"],
                                                env=self._environment(), stdout=log,
                                                stderr=subprocess.STDOUT)
                with self._lock:
                    self._process = process
                    item["pid"] = process.pid
                    self._persist_locked()
                config = LayaConfig(enabled=True, allow_live_http=True, port=self.deployment.port)
                probe_backend = self._probe_factory(config)
                self._set_startup_backend(probe_backend, intent)
                end = self._clock() + self.deployment.startup_timeout_s
                while True:
                    self._check_intent(intent, cancel, is_current)
                    if process.poll() is not None:
                        raise OwnedLayaError("service_exited_before_ready")
                    if self._clock() >= end:
                        raise OwnedLayaError("readiness_deadline")
                    readiness = probe_backend.probe()
                    self._check_intent(intent, cancel, is_current)
                    summary = self._health_summary(readiness.get("health"))
                    with self._lock:
                        item["last_probe"] = {"ok": readiness.get("ok"),
                                              "error_code": readiness.get("error_code"),
                                              "restart_required": readiness.get("restart_required"),
                                              "health": summary}
                    if readiness.get("ok") is True:
                        with self._lock:
                            self._health = summary
                            item["health"] = copy.deepcopy(self._health)
                            item["ready_monotonic_ns"] = time.monotonic_ns()
                            item["load_ready_ms"] = (item["ready_monotonic_ns"] - started) / 1e6
                        break
                    intent.wait(.1)
                probe_backend.close()
                probe_backend = None
                if warmup:
                    warm_config=replace(config, deadline_ms=min(60000,self.deployment.warmup_deadline_ms))
                    cold = (_ColdWarmupBackend(warm_config,self.deployment.warmup_deadline_ms)
                            if self._probe_factory is LayaBackend and self.deployment.warmup_deadline_ms>60000
                            else self._probe_factory(warm_config))
                    probe_backend = cold
                    self._set_startup_backend(cold, intent)
                    self._check_intent(intent, cancel, is_current)
                    warm_started = time.monotonic_ns()
                    if self._warmup is None:
                        decision = cold.decide(fixed_warmup_snapshot())
                        last = cold.last_record or {}
                        record = {"decision": decision.to_dict(), "snapshot_id": last.get("snapshot_id"),
                                  "input_sha256": last.get("input_sha256")}
                    else:
                        record = self._warmup(cold)
                    self._check_intent(intent, cancel, is_current)
                    with self._lock:
                        item["warmup"] = {"deadline_ms": self.deployment.warmup_deadline_ms,
                                          "elapsed_ms": (time.monotonic_ns() - warm_started) / 1e6,
                                          "result": copy.deepcopy(record)}
                self._check_intent(intent, cancel, is_current)
                if process.poll() is not None:
                    raise OwnedLayaError("service_exited_before_ready")
                with self._lock:
                    if intent.is_set():
                        raise OwnedLayaError("superseded")
                    self._state = "ready"
                    self._persist_locked()
                return config
            except Exception as exc:
                code = getattr(exc, "code", "service_start_failed")
                with self._lock:
                    self._error = code
                    if probe_backend is not None and probe_backend.status().get("restart_required"):
                        self._quarantined = True
                        item["quarantine_error"] = code
                    self._state = "restart_required" if self._quarantined else "failed"
                if process is not None:
                    self._terminate(process, generation)
                else:
                    with self._lock:
                        item["exit_code"] = None
                        item["exit_confirmed_monotonic_ns"] = time.monotonic_ns()
                with self._lock:
                    if code == "superseded" and not self._quarantined:
                        self._state = "stopped"
                    self._persist_locked()
                if isinstance(exc, OwnedLayaError):
                    raise
                raise OwnedLayaError(code) from None
            finally:
                if probe_backend is not None:
                    probe_backend.close()
                    if probe_backend.status().get("busy") is True:
                        with self._lock:
                            self._permit_backend = probe_backend
                            self._permit_running = False
                            self._draining = True
                with self._lock:
                    if self._intent is intent:
                        self._intent = None
                        self._startup_backend = None
                    if process is None and self._log is log:
                        self._log = None
                        log.close()

    def quarantine(self, generation, code="restart_required"):
        with self._lock:
            for item in self._history:
                if item.get("generation") == generation:
                    item["quarantine_error"] = str(code)
                    break
            if generation == self._generation:
                self._quarantined = True
                self._state = "restart_required"
                self._error = str(code)
            self._persist_locked()

    def _check_guard_locked(self, generation):
        if self._ownership_unknown:
            raise OwnedLayaError("ownership_unknown")
        if generation != self._generation:
            raise OwnedLayaError("stale_service_generation")
        if self._quarantined:
            raise OwnedLayaError("restart_required")
        if self._state != "ready" or self._process is None:
            raise OwnedLayaError("service_not_ready")
        if self._process.poll() is not None:
            self._state, self._error = "failed", "owned_service_exited"
            self._history[-1]["exit_code"] = self._process.poll()
            self._history[-1]["exit_confirmed_monotonic_ns"] = time.monotonic_ns()
            self._persist_locked()
            raise OwnedLayaError("owned_service_exited")

    def guard(self, generation):
        self.requests_idle()
        with self._lock:
            self._check_guard_locked(generation)
            if self._permit_backend is not None:
                raise OwnedLayaError("busy")

    @contextmanager
    def decision_guard(self, generation, backend):
        self.requests_idle()
        with self._lock:
            self._check_guard_locked(generation)
            if self._permit_backend is not None:
                raise OwnedLayaError("busy")
            self._permit_backend, self._permit_running = backend, True
        try:
            yield
        finally:
            status = backend.status()
            # Latch before releasing the permit, including across client replacement.
            if status.get("restart_required") is True:
                self.quarantine(generation, status.get("error_code") or "restart_required")
            with self._lock:
                if self._permit_backend is backend:
                    self._permit_running = False
                    self._draining = status.get("busy") is True
                    if not self._draining:
                        self._permit_backend = None

    def _terminate(self, process, generation):
        """Only the captured Popen handle is eligible for TERM/KILL."""
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=self.deployment.terminate_timeout_s)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=self.deployment.kill_timeout_s)
                except subprocess.TimeoutExpired:
                    raise OwnedLayaError("service_exit_not_confirmed") from None
        exit_code = process.poll()
        if exit_code is None:
            raise OwnedLayaError("service_exit_not_confirmed")
        with self._lock:
            for item in self._history:
                if item.get("generation") == generation:
                    item["exit_code"] = exit_code
                    item["exit_confirmed_monotonic_ns"] = time.monotonic_ns()
                    break
            if self._process is process and self._generation == generation:
                if self._log is not None:
                    self._log.close()
                    self._log = None
            self._persist_locked()
        return {"generation": generation, "exit_code": exit_code, "exit_confirmed": True}

    def stop(self, *, expected_generation=None):
        self.interrupt("service_stop", expected_generation=expected_generation)
        with self._operations:
            with self._lock:
                if self._ownership_unknown:
                    raise OwnedLayaError("ownership_unknown")
                generation, process = self._generation, self._process
                if expected_generation is not None and expected_generation != generation:
                    raise OwnedLayaError("stale_service_generation")
                if process is None:
                    self._state = "restart_required" if self._quarantined else "stopped"
                    return {"generation": generation, "exit_confirmed": True, "owned": False}
                self._state = "stopping"
            try:
                result = self._terminate(process, generation)
            except Exception:
                with self._lock:
                    self._state, self._error = "failed", "service_exit_not_confirmed"
                    self._persist_locked()
                raise
            with self._lock:
                self._state = "restart_required" if self._quarantined else "stopped"
                self._persist_locked()
            return result


class OwnedServer(OwnedLayaService):
    """Compatibility constructor for the explicit B07 verification script."""

    def __init__(self, python, cache, port, output, device):
        super().__init__(Deployment(python=python, cache=cache, port=port, output=output, device=device))
