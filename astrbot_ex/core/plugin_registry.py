from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from astrbot_ex.core.plugin_actor import PluginActor


@dataclass(slots=True)
class PluginSlot:
    kind: str
    plugin: Any
    actor: PluginActor
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return str(getattr(self.plugin, "id", self.plugin.__class__.__name__))

    @property
    def name(self) -> str:
        return str(getattr(self.plugin, "name", self.plugin.__class__.__name__))

    def has_method(self, method: str) -> bool:
        return self.actor.has_method(method)

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return self.actor.call(method, *args, **kwargs)

    def cast(self, method: str, *args: Any, coalesce_key: str | None = None, **kwargs: Any) -> bool:
        return self.actor.cast(method, *args, coalesce_key=coalesce_key, **kwargs)


class PluginRegistry:
    def __init__(self) -> None:
        self._slots: dict[str, PluginSlot] = {}
        self._lock = RLock()
        self._runtime_active = False

    def register(
        self,
        kind: str,
        plugin: Any,
        *,
        enabled: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        plugin_id = getattr(plugin, "id", plugin.__class__.__name__)
        with self._lock:
            if plugin_id in self._slots:
                raise ValueError(f"Plugin already registered: {plugin_id}")
            runtime_active = self._runtime_active

        actor = PluginActor(plugin)
        actor.start()
        try:
            actor.call("on_load")
            if enabled:
                actor.call("on_enable")
                if runtime_active:
                    actor.call("on_runtime_start")
        except Exception:
            for method, args in (
                ("on_runtime_stop", ("plugin registration failed",)),
                ("on_disable", ()),
                ("on_unload", ()),
            ):
                try:
                    actor.call(method, *args)
                except Exception:
                    pass
            actor.stop()
            raise

        slot = PluginSlot(
            kind=kind,
            plugin=plugin,
            actor=actor,
            enabled=enabled,
            metadata=metadata or {},
        )
        with self._lock:
            if plugin_id in self._slots:
                actor.stop()
                raise ValueError(f"Plugin already registered: {plugin_id}")
            self._slots[plugin_id] = slot

    def unregister(self, plugin_id: str) -> None:
        with self._lock:
            slot = self._slots.pop(plugin_id)
            runtime_active = self._runtime_active
        try:
            if slot.enabled and runtime_active:
                slot.call("on_runtime_stop", "plugin unregistered")
            if slot.enabled:
                slot.call("on_disable")
            slot.call("on_unload")
        finally:
            slot.actor.stop()

    def get_one(self, kind: str) -> Any | None:
        slot = self.get_slot(kind)
        return slot.plugin if slot else None

    def get_slot(self, kind: str) -> PluginSlot | None:
        with self._lock:
            for slot in self._slots.values():
                if slot.kind == kind and slot.enabled:
                    return slot
            return None

    def get(self, plugin_id: str) -> PluginSlot | None:
        with self._lock:
            return self._slots.get(plugin_id)

    def list(self) -> list[PluginSlot]:
        with self._lock:
            return list(self._slots.values())

    def enable(self, plugin_id: str) -> None:
        with self._lock:
            slot = self._slots[plugin_id]
            runtime_active = self._runtime_active
        if not slot.enabled:
            slot.call("on_enable")
            slot.enabled = True
            if runtime_active:
                slot.call("on_runtime_start")

    def disable(self, plugin_id: str) -> None:
        with self._lock:
            slot = self._slots[plugin_id]
            runtime_active = self._runtime_active
        if slot.enabled:
            if runtime_active:
                slot.call("on_runtime_stop", "plugin disabled")
            slot.call("on_disable")
            slot.enabled = False

    def start_runtime(self) -> None:
        with self._lock:
            if self._runtime_active:
                return
            self._runtime_active = True
            slots = [slot for slot in self._slots.values() if slot.enabled]
        for slot in slots:
            try:
                slot.call("on_runtime_start")
            except Exception:
                continue

    def stop_runtime(self, reason: str) -> None:
        with self._lock:
            if not self._runtime_active:
                return
            self._runtime_active = False
            slots = [slot for slot in self._slots.values() if slot.enabled]
        first_error: Exception | None = None
        for slot in slots:
            try:
                slot.call("on_runtime_stop", reason)
            except Exception as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error
