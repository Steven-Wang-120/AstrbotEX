from __future__ import annotations

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.models import RobotState, ScanResult, VisionResult, WorldState
from astrbot_ex.core.plugin_registry import PluginRegistry, PluginSlot
from astrbot_ex.core.world_builder import WorldBuilder
from astrbot_ex.interfaces.fusion import FusionProvider


class PerceptionCore:
    def __init__(
        self,
        *,
        registry: PluginRegistry,
        event_bus: EventBus,
        fusion: FusionProvider | None = None,
    ) -> None:
        self.registry = registry
        self.event_bus = event_bus
        self.world_builder = WorldBuilder(fusion=fusion)

    def update(self, robot: RobotState, previous_world: WorldState) -> WorldState:
        vision_provider = self._vision_provider()
        scan_provider = self._scan_provider()

        vision = self._read_vision(vision_provider, previous_world)
        scan = self._read_scan(scan_provider)
        world = self.world_builder.update(vision, scan, robot)
        self.event_bus.emit_throttled(
            "vision",
            "vision frame received",
            interval_sec=0.5,
            key="runtime:vision_frame",
            frame_id=vision.frame_id,
            entities=len(vision.entities),
        )
        if scan is not None:
            self.event_bus.emit_throttled(
                "scan",
                "scan frame received",
                interval_sec=0.5,
                key="runtime:scan_frame",
                frame_id=scan.frame_id,
                points=len(scan.ranges),
            )
        return world

    def _vision_provider(self) -> PluginSlot | None:
        return self.registry.get_slot("vision")

    def _scan_provider(self) -> PluginSlot | None:
        return self.registry.get_slot("scan")

    def _read_vision(
        self,
        provider: PluginSlot | None,
        previous_world: WorldState,
    ) -> VisionResult:
        if provider is None:
            self.event_bus.emit_throttled(
                "vision",
                "vision provider unavailable",
                interval_sec=1.0,
            )
            return VisionResult(
                frame_id=0,
                timestamp=previous_world.timestamp,
                metadata={"source": "missing_vision"},
            )
        return provider.call("get_result")

    def _read_scan(self, provider: PluginSlot | None) -> ScanResult | None:
        if provider is None:
            self.event_bus.emit_throttled(
                "scan",
                "scan provider unavailable",
                interval_sec=1.0,
                severity="warning",
            )
            return None
        try:
            return provider.call("get_scan")
        except Exception as exc:
            self.event_bus.emit(
                "plugin_fault",
                "scan read failed",
                plugin=provider.id,
                error=str(exc),
            )
            return None
