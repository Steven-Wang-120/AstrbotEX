from __future__ import annotations
import copy
import json
import threading
import time
import uuid
import os
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any
from .contracts import environment_config
from .models import ENVIRONMENT_MODES, EnvironmentBusyError, EnvironmentRevisionConflict, EnvironmentSnapshot
from .normal import NormalEnvironmentAdapter
from .ros2.adapter import Ros2EnvironmentAdapter

class EnvironmentManager:
    """Serializes external resources; the supplied TopicBus is never mutated."""
    def __init__(self, *, data_root, event_bus, topic_bus, adapter_factory=None):
        self.data_root = Path(data_root)
        self.config_path = self.data_root / "profiles/default/environments.json"
        self.event_bus, self.topic_bus = event_bus, topic_bus
        self._adapter_factory = adapter_factory or self._default_adapter
        self._lock = threading.RLock()
        self.resources_lock = threading.RLock()
        self._owners = {}
        self._operations = {}
        self._worker = None
        self._closed = False
        self.accepting = False
        self.runtime_running = lambda: False
        self.action_service = None
        self.decision_service = None
        self.quiesce_timeout = 3.0
        self._stop_token = None
        self._stop_owners = set()
        self._stop_deadline = 0.0
        self._stop_errors = []
        self._config = self._load_config()
        self._adapter = NormalEnvironmentAdapter()
        self._adapter.start()
        self._snapshot = EnvironmentSnapshot(1, uuid.uuid4().hex, 1, "normal", "normal", "idle", "ok", 1, None, None)

    @staticmethod
    def _default_adapter(mode, config):
        return Ros2EnvironmentAdapter(config) if mode == "ros2" else NormalEnvironmentAdapter()

    @staticmethod
    def _default_config():
        return {"schema_version": 1, "selected_mode": "normal", "ros2": environment_config({})}

    def _load_config(self):
        config = self._default_config()
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict): return config
            config["ros2"] = environment_config(raw.get("ros2", {}))
            if raw.get("selected_mode") in ENVIRONMENT_MODES: config["selected_mode"] = raw["selected_mode"]
        except (OSError, ValueError, TypeError):
            pass
        return config

    def _save_config(self, config):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.config_path)

    def snapshot(self):
        with self._lock: return self._snapshot.to_dict()

    def status(self):
        with self._lock:
            snapshot = self._snapshot.to_dict()
            adapter = self._adapter
            config = copy.deepcopy(self._config)
            operations = copy.deepcopy(list(self._operations.values())[-10:])
        active = adapter.status()
        if snapshot['active_mode'] == 'ros2' and snapshot['phase'] == 'idle':
            snapshot['health'] = active.get('health', 'unknown')
        probe = Ros2EnvironmentAdapter.probe()
        effective, locked = self._effective_config(config['ros2'])
        return {"ok": True, "environment": snapshot, "config": config, "operations": operations,
                "effective_config": copy.deepcopy(getattr(adapter, 'config', effective)), "locked_config": locked,
                "active_adapter": active,
                "adapters": {
                    "normal": {"mode": "normal", "label": "普通环境", "available": True, "active": snapshot["active_mode"] == "normal", "health": "ok"},
                    "ros2": {**probe, "mode": "ros2", "label": "ROS 2", "active": snapshot["active_mode"] == "ros2"}
                }}

    def _effective_config(self, config):
        locked = {}
        for field, variable in (('domain_id', 'ROS_DOMAIN_ID'), ('namespace', 'ASTRBOTEX_ROS_NAMESPACE'),
                                ('node_name', 'ASTRBOTEX_ROS_NODE_NAME')):
            value = os.getenv(variable)
            if value:
                locked[field] = {'source': variable, 'value': int(value) if field == 'domain_id' else value}
        effective = environment_config({**config, **{key: item['value'] for key, item in locked.items()}})
        return effective, locked

    def stop_token_valid(self, token, owner_id):
        return bool(token and token == self._stop_token and owner_id in self._stop_owners
                    and time.monotonic() < self._stop_deadline)

    def stop_permission(self, facade):
        token = getattr(facade._hook_local, 'token', None)
        return token if self.stop_token_valid(token, facade.owner_id) else None

    def stop_failed(self, reason):
        self._stop_errors.append(reason)

    def _quiesce(self, reason, operation_id):
        with self.resources_lock:
            owners = list(self._owners.values())
        ports = [port for owner in owners for port in owner.ports()]
        controls = [owner for owner in owners if any(port.native is not None and
                    port.declaration['requires_runtime_running'] for port in owner.ports())]
        self._stop_deadline = time.monotonic() + self.quiesce_timeout
        self._stop_token = operation_id
        self._stop_owners = {owner.owner_id for owner in controls}
        self._stop_errors = []
        if self.action_service is not None:
            self.action_service.request_stops(reason)
        for owner in controls:
            if owner.actor is not None:
                owner.actor.set_stop_token(operation_id)
        try:
            for port in ports:
                port.discard_queue()
            while any(port.status()['in_flight'] for port in ports):
                if time.monotonic() >= self._stop_deadline:
                    raise EnvironmentBusyError('quiesce deadline: publication still in progress')
                time.sleep(0.005)
            for owner in controls:
                actor = owner.actor
                if actor is None or not actor.has_method('on_environment_deactivating'):
                    if self.runtime_running():
                        raise EnvironmentBusyError('ROS controller has no deactivation hook; stop the runtime first')
                    continue
                remaining = self._stop_deadline - time.monotonic()
                if remaining <= 0:
                    raise EnvironmentBusyError('quiesce deadline exceeded')
                try:
                    result = actor.call('on_environment_deactivating', 'ros2', reason, timeout=remaining,
                                        _astrbotex_environment_token=operation_id)
                except FutureTimeoutError as exc:
                    raise EnvironmentBusyError(f'{owner.plugin_id}: deactivation hook exceeded deadline') from exc
                if result is False:
                    raise EnvironmentBusyError('plugin refused environment deactivation')
            if self.action_service is not None and not self.action_service.await_stop_proof(reason):
                raise EnvironmentBusyError(self.action_service.status()['error'] or 'action stop not proven')
            while any(port.status()['queue_depth'] or port.status()['in_flight'] for port in ports
                      if port.declaration['direction'] == 'publish'):
                if time.monotonic() >= self._stop_deadline:
                    raise EnvironmentBusyError('quiesce deadline: stop publication did not finish')
                time.sleep(0.005)
            if self._stop_errors:
                raise EnvironmentBusyError('; '.join(self._stop_errors))
        finally:
            self._stop_token = None
            self._stop_owners = set()
            for owner in controls:
                if owner.actor is not None:
                    owner.actor.set_stop_token(None)
            for port in ports:
                port.discard_queue()

    def _check_revision(self, expected_revision=None, expected_session=None):
        if expected_session is not None and expected_session != self._snapshot.session_id:
            raise EnvironmentRevisionConflict("environment session changed; refresh first")
        if expected_revision is not None and (isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision != self._snapshot.revision):
            raise EnvironmentRevisionConflict("environment revision changed; refresh first")

    def select(self, mode, *, expected_revision=None, expected_session=None):
        if mode not in ENVIRONMENT_MODES: raise ValueError(f"unsupported environment: {mode}")
        with self._lock:
            if self._closed: raise EnvironmentBusyError("environment manager is closed")
            self._check_revision(expected_revision, expected_session)
            if self._snapshot.phase in ("starting", "stopping"):
                if mode == self._snapshot.desired_mode:
                    return {"ok": True, "accepted": True, "changed": False, "operation_id": self._snapshot.operation_id, "environment": self.snapshot()}
                raise EnvironmentBusyError("another environment operation is running")
            if mode == self._snapshot.active_mode and self._snapshot.phase == "idle":
                return {"ok": True, "accepted": False, "changed": False, "environment": self.snapshot()}
            if self.decision_service is not None:
                self.decision_service.request_stop('environment_switch')
            if self.action_service is not None:
                self.action_service.revoke()
            if mode == 'ros2' and self._snapshot.active_mode == 'ros2':
                raise EnvironmentBusyError('finish deactivating the retained ROS environment before enabling it again')
            # Never tear down a live controller's output path without its stop protocol.
            if self._snapshot.active_mode == "ros2" and self.runtime_running():
                if any(p.declaration["requires_runtime_running"] and p.native is not None and
                       (f.actor is None or not f.actor.has_method('on_environment_deactivating'))
                       for f in list(self._owners.values()) for p in f.ports()):
                    raise EnvironmentBusyError("ROS 控制输出正在运行；请先停止相关运行任务再切换环境")
            op = uuid.uuid4().hex
            previous = self._snapshot.active_mode
            self._snapshot.desired_mode = mode
            self._snapshot.phase = "stopping" if previous == "ros2" else "starting"
            self._snapshot.last_error = None
            self._snapshot.operation_id = op
            self._snapshot.revision += 1
            self.accepting = False
            self._operations[op] = {"operation_id": op, "mode": mode, "from": previous, "phase": self._snapshot.phase, "started_at": time.time()}
            while len(self._operations) > 32: self._operations.pop(next(iter(self._operations)))
            self._worker = threading.Thread(target=self._transition, args=(mode, op), name="ex-environment-switch", daemon=True)
            self._worker.start()
        if self.decision_service is not None:
            self.decision_service.request_stop('environment_switch')
        self._emit_change("environment switching")
        return {"ok": True, "accepted": True, "changed": True, "operation_id": op, "environment": self.snapshot()}

    def _transition(self, mode, op):
        candidate = None
        persisted = False
        old = self._adapter
        original_config = copy.deepcopy(self._config)
        try:
            if mode == 'normal' and self._snapshot.active_mode == 'ros2':
                self._quiesce('switch to normal', op)
            elif self.action_service is not None and not self.action_service.stop_actions('environment switch'):
                raise EnvironmentBusyError(self.action_service.status()['error'] or 'action stop not proven')
            with self.resources_lock:
                with self._lock:
                    old = self._adapter
                    config = copy.deepcopy(self._config)
                    generation = self._snapshot.generation + 1
                if mode == "ros2":
                    candidate = self._adapter_factory("ros2", self._effective_config(config['ros2'])[0])
                    if hasattr(candidate, 'set_change_callback'):
                        candidate.set_change_callback(self._adapter_changed)
                    candidate.start()
                    for facade in list(self._owners.values()):
                        for port in facade.ports(): candidate.attach(port, generation)
                else:
                    config['selected_mode'] = mode
                    self._save_config(config)
                    persisted = True
                    old.close("switch to normal")
                    for facade in list(self._owners.values()):
                        for port in facade.ports(): port.unavailable()
                    candidate = NormalEnvironmentAdapter()
                    candidate.start()
                config["selected_mode"] = mode
                if not persisted:
                    self._save_config(config)
                    persisted = True
                with self._lock:
                    self._adapter = candidate
                    self._config = config
                    self._snapshot.active_mode = mode
                    self._snapshot.phase = "idle"
                    self._snapshot.generation = generation
                    self._snapshot.health = "ok"
                    self._snapshot.revision += 1
                    self.accepting = mode == "ros2"
                    self._operations[op].update(phase="completed", finished_at=time.time(), result="ok")
        except Exception as exc:
            if candidate is not None and candidate is not self._adapter:
                try: candidate.close("failed transition")
                except Exception: pass
            if persisted:
                try:
                    self._save_config(original_config)
                except Exception as rollback_error:
                    exc = RuntimeError(f'{exc}; config rollback failed: {rollback_error}')
            with self._lock:
                self._snapshot.phase = "failed"
                self._snapshot.health = "degraded"
                code = 'quiesce_failed' if isinstance(exc, (EnvironmentBusyError, TimeoutError)) else 'environment_transition_failed'
                self._snapshot.last_error = {"code": code, "message": str(exc), "mode": mode}
                self._snapshot.revision += 1
                self._operations[op].update(phase="failed", finished_at=time.time(), error=copy.deepcopy(self._snapshot.last_error))
        finally:
            self._emit_change("environment operation finished")

    def get_operation(self, operation_id):
        with self._lock: return copy.deepcopy(self._operations.get(operation_id))

    def register_owner(self, facade):
        with self.resources_lock:
            if self._closed: raise EnvironmentBusyError("environment manager is closed")
            self._owners[facade.owner_id] = facade

    def unregister_owner(self, facade):
        with self.resources_lock: self._owners.pop(facade.owner_id, None)
        self._emit_change("ROS plugin owner removed", plugin_id=facade.plugin_id)

    def attach_port(self, port):
        with self.resources_lock:
            if self._snapshot.active_mode == "ros2" and self._snapshot.phase == "idle":
                self._adapter.attach(port, self._snapshot.generation)
            else: port.unavailable()
        self._emit_change("ROS endpoint changed", plugin_id=port.facade.plugin_id)

    def detach_port(self, port):
        with self.resources_lock:
            detach = getattr(self._adapter, "detach", None)
            if detach: detach(port)
            else: port.unavailable()
        self._emit_change("ROS endpoint removed", plugin_id=port.facade.plugin_id)

    def graph(self):
        with self._lock: adapter = self._adapter; snapshot = self.snapshot()
        return {**adapter.graph(), "environment": snapshot, "generation": snapshot["generation"]}

    def endpoints(self):
        with self.resources_lock:
            rows = [p.status() for f in self._owners.values() for p in f.ports()]
        snapshot = self.snapshot()
        return {"received": [r for r in rows if r["direction"] == "subscribe"],
                "sent": [r for r in rows if r["direction"] == "publish"],
                "refreshed_at": time.time(), "environment": snapshot, "generation": snapshot["generation"]}

    def configure_ros2(self, raw, *, expected_revision=None, expected_session=None):
        with self._lock:
            self._check_revision(expected_revision, expected_session)
            if self._snapshot.phase in ("starting", "stopping"): raise EnvironmentBusyError("switch in progress")
            config = copy.deepcopy(self._config)
            _, locked = self._effective_config(config['ros2'])
            if isinstance(raw, dict):
                for key, item in locked.items():
                    if key in raw and raw[key] != item['value']:
                        raise ValueError(f'{key} is locked by {item["source"]}')
            config["ros2"] = environment_config(raw, config["ros2"])
            self._save_config(config)
            self._config = config
            self._snapshot.revision += 1
            active = self._snapshot.active_mode == "ros2"
        self._emit_change("environment config saved")
        return {"ok": True, "saved": True, "applied": not active, "restart_required": active,
                "config": copy.deepcopy(config["ros2"]), "environment": self.snapshot()}

    def refresh(self):
        adapter = self._adapter
        if hasattr(adapter, "refresh"): adapter.refresh()
        return {"ok": True, "accepted": True, "environment": self.snapshot()}

    def interfaces(self):
        if self._snapshot.active_mode == "ros2":
            result = self._adapter.interfaces()
            result['environment'] = self.snapshot()
            return result
        return {"packages": [], "checks": [], "state": "waiting_environment", "restart_required_for_new_packages": True}

    def check_interface(self, message_type):
        from .contracts import TYPE_NAME
        if not isinstance(message_type, str) or not TYPE_NAME.fullmatch(message_type): raise ValueError("invalid message_type")
        if self._snapshot.active_mode != "ros2":
            return {"message_type": message_type, "available": False, "code": "waiting_environment", "message": "请先启用 ROS 2 环境"}
        return self._adapter.check_interface(message_type)

    def reload(self):
        with self._lock: self._config = self._load_config()

    def restore_selected_mode(self):
        selected = self._config['selected_mode']
        if selected != 'normal':
            return self.select(selected)
        return None

    def reset_after_restore(self):
        if self.action_service is not None:
            self.action_service.revoke()
            if not self.action_service.stop_actions('snapshot restore'):
                raise EnvironmentBusyError(self.action_service.status()['error'] or 'action stop not proven')
        self._finish_worker()
        with self.resources_lock:
            self.accepting = False
            self._adapter.close("snapshot restore")
            for owner in list(self._owners.values()): owner.close()
            self._adapter = NormalEnvironmentAdapter()
            self._adapter.start()
            with self._lock:
                self._snapshot.active_mode = self._snapshot.desired_mode = "normal"
                self._snapshot.phase = "idle"
                self._snapshot.health = "ok"
                self._snapshot.last_error = self._snapshot.operation_id = None
                self._snapshot.generation += 1
                self._snapshot.revision += 1
        self._emit_change("environment reset after restore")

    def _finish_worker(self):
        worker = self._worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=5.0)
            if worker.is_alive(): raise EnvironmentBusyError("environment operation has not stopped")

    def close(self, reason="shutdown"):
        if self.action_service is not None:
            self.action_service.revoke()
        self._finish_worker()
        self.accepting = False
        errors = []
        if self._snapshot.active_mode == 'ros2':
            try:
                self._quiesce(reason, uuid.uuid4().hex)
            except Exception as exc:
                errors.append(str(exc))
        elif self.action_service is not None and not self.action_service.stop_actions(reason):
            errors.append(self.action_service.status()['error'] or 'action stop not proven')
        if errors:
            raise EnvironmentBusyError('; '.join(errors))
        with self.resources_lock:
            self._closed = True
            try:
                self._adapter.close(reason)
            except Exception as exc:
                errors.append(str(exc))
            for owner in list(self._owners.values()):
                try:
                    owner.close()
                except Exception as exc:
                    errors.append(str(exc))
        if errors:
            raise EnvironmentBusyError('; '.join(errors))

    def _adapter_changed(self, kind):
        self._emit_change('ROS resources changed', event_type=kind)

    def _emit_change(self, message, event_type='environment_changed', **data):
        snapshot = self.snapshot()
        if self.action_service is not None:
            self.action_service.update_versions(environment_revision=snapshot['revision'])
        self.event_bus.emit(event_type, message, session_id=snapshot["session_id"], revision=snapshot["revision"],
                            generation=snapshot["generation"], **data)
