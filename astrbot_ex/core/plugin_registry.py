from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypeVar


TPlugin = TypeVar("TPlugin")


@dataclass(slots=True)
class PluginSlot:
    kind: str
    plugin: Any
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


class PluginRegistry:
    def __init__(self) -> None:
        self._slots: dict[str, PluginSlot] = {}

    def register(self, kind: str, plugin: Any, *, enabled: bool = True) -> None:
        plugin_id = getattr(plugin, "id", plugin.__class__.__name__)
        if plugin_id in self._slots:
            raise ValueError(f"Plugin already registered: {plugin_id}")
        if hasattr(plugin, "on_load"):
            plugin.on_load()
        self._slots[plugin_id] = PluginSlot(kind=kind, plugin=plugin, enabled=enabled)
        if enabled and hasattr(plugin, "on_enable"):
            plugin.on_enable()

    def get_one(self, kind: str) -> Any | None:
        for slot in self._slots.values():
            if slot.kind == kind and slot.enabled:
                return slot.plugin
        return None

    def list(self) -> list[PluginSlot]:
        return list(self._slots.values())

    def enable(self, plugin_id: str) -> None:
        slot = self._slots[plugin_id]
        if not slot.enabled:
            slot.enabled = True
            if hasattr(slot.plugin, "on_enable"):
                slot.plugin.on_enable()

    def disable(self, plugin_id: str) -> None:
        slot = self._slots[plugin_id]
        if slot.enabled:
            slot.enabled = False
            if hasattr(slot.plugin, "on_disable"):
                slot.plugin.on_disable()
