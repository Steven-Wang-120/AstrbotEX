from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from astrbot_ex.core.environments.models import (
    ENVIRONMENT_MODES,
    EnvironmentBusyError,
    EnvironmentRevisionConflict,
    EnvironmentSnapshot,
)
from astrbot_ex.core.environments.normal import NormalEnvironmentAdapter
from astrbot_ex.core.environments.ros2.adapter import Ros2EnvironmentAdapter
from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.topic_bus import TopicBus


class EnvironmentManager:
    """Own external communication adapters while leaving TopicBus untouched."""

    def __init__(
        self,
        *,
        data_root: Path,
        event_bus: EventBus,
        topic_bus: TopicBus,
        adapter_factory: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.data_root = data_root
        self.config_path = data_root / "profiles" / "default" / "environments.json"
        self.event_bus = event_bus
        self.topic_bus = topic_bus
        self._adapter_factory = adapter_factory or self._default_adapter
        self._lock = threading.RLock()
        self._operations: dict[str, dict[str, Any]] = {}
        self._session_id = uuid.uuid4().hex
        self._config = self._load_config()
        self._adapter: Any = NormalEnvironmentAdapter()
        self._adapter.start()
        self._snapshot = EnvironmentSnapshot(
            schema_version=1,
            session_id=self._session_id,
            revision=1,
            desired_mode="normal",
            active_mode="normal",
            phase="idle",
            health="ok",
            generation=1,
            operation_id=None,
            last_error=None,
            topic_bus_available=True,
        )

    @staticmethod
    def _default_config() -> dict[str, Any]:
        return {
            "schema_version": 1,
            "selected_mode": "normal",
            "ros2": {
                "domain_id": 0,
                "namespace": "/astrbotex",
                "node_name": "environment",
                "discovery_interval_sec": 1.0,
                "statistics_interval_sec": 1.0,
                "include_hidden_topics": False,
            },
        }

    def _load_config(self) -> dict[str, Any]:
        config = self._default_config()
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return config
        except (OSError, json.JSONDecodeError):
            return config
        if not isinstance(raw, dict):
            return config
        ros2 = raw.get("ros2")
        if isinstance(ros2, dict):
            config["ros2"].update(ros2)
        selected = str(raw.get("selected_mode", "normal")).strip().lower()
        if selected in ENVIRONMENT_MODES:
            config["selected_mode"] = selected
        return config

    def _save_config(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.config_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(self._config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(self.config_path)

    @staticmethod
    def _default_adapter(mode: str, config: dict[str, Any]) -> Any:
        if mode == "normal":
            return NormalEnvironmentAdapter()
        if mode == "ros2":
            return Ros2EnvironmentAdapter(config)
        raise ValueError(f"unsupported environment mode: {mode}")

    @staticmethod
    def _validate_mode(mode: Any) -> str:
        normalized = str(mode or "").strip().lower()
        if normalized not in ENVIRONMENT_MODES:
            raise ValueError(f"unsupported environment mode: {normalized or '<empty>'}")
        return normalized

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot.to_dict()

    def status(self) -> dict[str, Any]:
        with self._lock:
            snapshot = self._snapshot.to_dict()
            active = self._adapter.status()
            config = json.loads(json.dumps(self._config))
            operations = list(self._operations.values())[-10:]
        adapters = {
            "normal": {
                "mode": "normal",
                "available": True,
                "active": snapshot["active_mode"] == "normal",
                "label": "普通环境",
                "health": "ok",
                "reason": None,
            },
            "ros2": Ros2EnvironmentAdapter.probe(),
        }
        adapters["ros2"].update(
            {
                "mode": "ros2",
                "label": "ROS 2",
                "active": snapshot["active_mode"] == "ros2",
                "health": active.get("health")
                if snapshot["active_mode"] == "ros2"
                else ("unavailable" if not adapters["ros2"].get("available") else "unknown"),
            }
        )
        return {
            "ok": True,
            "environment": snapshot,
            "active_adapter": active,
            "adapters": adapters,
            "config": config,
            "operations": operations,
        }

    def select(self, mode: Any, *, expected_revision: int | None = None) -> dict[str, Any]:
        target = self._validate_mode(mode)
        with self._lock:
            if expected_revision is not None and int(expected_revision) != self._snapshot.revision:
                raise EnvironmentRevisionConflict(
                    f"environment revision mismatch: expected {expected_revision}, current {self._snapshot.revision}"
                )
            if self._snapshot.phase in {"starting", "stopping"}:
                raise EnvironmentBusyError("another environment operation is in progress")
            if target == self._snapshot.active_mode and self._snapshot.phase == "idle":
                return {
                    "ok": True,
                    "changed": False,
                    "operation_id": self._snapshot.operation_id,
                    "environment": self._snapshot.to_dict(),
                }
            operation_id = uuid.uuid4().hex
            previous = self._snapshot.active_mode
            self._snapshot.desired_mode = target
            self._snapshot.phase = "starting"
            self._snapshot.operation_id = operation_id
            self._snapshot.revision += 1
            self._snapshot.last_error = None
            self._operations[operation_id] = {
                "operation_id": operation_id,
                "mode": target,
                "from": previous,
                "phase": "starting",
                "started_at": time.time(),
            }
            config = json.loads(json.dumps(self._config.get(target, {})))
        new_adapter: Any | None = None
        try:
            new_adapter = self._adapter_factory(target, config)
            new_adapter.start()
            with self._lock:
                old_adapter = self._adapter
                self._adapter = new_adapter
                self._snapshot.active_mode = target
                self._snapshot.phase = "idle"
                self._snapshot.health = str(new_adapter.status().get("health", "unknown"))
                self._snapshot.generation += 1
                self._snapshot.revision += 1
                self._snapshot.last_error = None
                self._config["selected_mode"] = target
                self._save_config()
                operation = self._operations[operation_id]
                operation.update(
                    {
                        "phase": "completed",
                        "finished_at": time.time(),
                        "result": "ok",
                    }
                )
            old_adapter.close(f"switched to {target}")
            self._emit_change(
                "environment mode changed",
                operation_id=operation_id,
                previous=previous,
                mode=target,
            )
            return {
                "ok": True,
                "changed": True,
                "operation_id": operation_id,
                "environment": self.snapshot(),
            }
        except Exception as exc:
            if new_adapter is not None:
                try:
                    new_adapter.close("environment start failed")
                except Exception:
                    pass
            with self._lock:
                self._snapshot.phase = "failed"
                self._snapshot.health = "unavailable" if target == "ros2" else "unknown"
                self._snapshot.last_error = {
                    "code": "environment_start_failed",
                    "message": str(exc),
                    "mode": target,
                }
                operation = self._operations.get(operation_id)
                if operation is not None:
                    operation.update(
                        {
                            "phase": "failed",
                            "finished_at": time.time(),
                            "result": "error",
                            "error": self._snapshot.last_error,
                        }
                    )
            self._emit_change(
                "environment mode change failed",
                operation_id=operation_id,
                mode=target,
            )
            return {
                "ok": False,
                "changed": False,
                "operation_id": operation_id,
                "environment": self.snapshot(),
                "error": self._snapshot.last_error,
            }

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self._lock:
            operation = self._operations.get(operation_id)
            return json.loads(json.dumps(operation)) if operation is not None else None

    def graph(self) -> dict[str, Any]:
        with self._lock:
            generation = self._snapshot.generation
            snapshot = self._snapshot.to_dict()
            adapter = self._adapter
        payload = adapter.graph()
        payload.update({"environment": snapshot, "generation": generation})
        return payload

    def endpoints(self) -> dict[str, Any]:
        with self._lock:
            generation = self._snapshot.generation
            snapshot = self._snapshot.to_dict()
            adapter = self._adapter
        payload = adapter.endpoints()
        payload.update({"environment": snapshot, "generation": generation})
        return payload

    def configure_ros2(self, config: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(config, dict):
            raise ValueError("ros2 config must be an object")
        with self._lock:
            current = dict(self._config.get("ros2", {}))
            current.update(config)
            try:
                domain_id = int(current.get("domain_id", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("ros2.domain_id must be an integer") from exc
            if domain_id < 0:
                raise ValueError("ros2.domain_id must be non-negative")
            namespace = str(current.get("namespace", "/astrbotex")).strip() or "/astrbotex"
            if not namespace.startswith("/"):
                raise ValueError("ros2.namespace must start with '/'")
            current["domain_id"] = domain_id
            current["namespace"] = namespace
            self._config["ros2"] = current
            self._save_config()
            active_ros2 = self._snapshot.active_mode == "ros2"
        applied = False
        restart_required = active_ros2
        self._emit_change("environment configuration updated", mode="ros2", applied=applied)
        return {
            "ok": True,
            "applied": applied,
            "restart_required": restart_required,
            "config": json.loads(json.dumps(self._config.get("ros2", {}))),
            "environment": self.snapshot(),
        }

    def reload(self) -> None:
        with self._lock:
            self._config = self._load_config()
        self._emit_change("environment configuration reloaded")

    def reset_after_restore(self) -> None:
        """Return to a clean normal adapter after an instance snapshot restore."""
        with self._lock:
            adapter = self._adapter
            self._adapter = NormalEnvironmentAdapter()
            self._adapter.start()
            self._snapshot.desired_mode = "normal"
            self._snapshot.active_mode = "normal"
            self._snapshot.phase = "idle"
            self._snapshot.health = "ok"
            self._snapshot.operation_id = None
            self._snapshot.last_error = None
            self._snapshot.generation += 1
            self._snapshot.revision += 1
        try:
            adapter.close("instance snapshot restore")
        finally:
            self._emit_change("environment reset after snapshot restore")

    def close(self, reason: str = "environment manager closed") -> None:
        self.reset_after_restore()
        self._emit_change("environment manager closed", reason=reason)
    def _emit_change(self, message: str, **data: Any) -> None:
        self.event_bus.emit(
            "environment",
            message,
            session_id=self._session_id,
            revision=self._snapshot.revision,
            generation=self._snapshot.generation,
            **data,
        )
