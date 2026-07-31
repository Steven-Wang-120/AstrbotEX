from __future__ import annotations

from dataclasses import dataclass

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.models import Goal, RobotState, RuntimeState, ScanResult, VisionResult, WorldState
from astrbot_ex.core.plugin_registry import PluginRegistry, PluginSlot
from astrbot_ex.core.safety import SafetyGuard
from astrbot_ex.core.scene_fusion import SceneFusion
from astrbot_ex.core.topic_bus import TopicBus
from astrbot_ex.core.world_builder import WorldBuilder


@dataclass(slots=True)
class ActiveSkill:
    slot: PluginSlot
    goal: Goal

    @property
    def plugin(self):
        return self.slot.plugin


class AstrBotEXRuntime:
    def __init__(
        self,
        registry: PluginRegistry,
        event_bus: EventBus | None = None,
        safety: SafetyGuard | None = None,
        topic_bus: TopicBus | None = None,
        fusion: SceneFusion | None = None,
    ) -> None:
        self.registry = registry
        self.event_bus = event_bus or EventBus()
        self.safety = safety or SafetyGuard()
        self.topic_bus = topic_bus or TopicBus()
        self.world_builder = WorldBuilder(fusion=fusion)
        self.state = RuntimeState.IDLE
        self.world = WorldState()
        self.active_skill: ActiveSkill | None = None

    def start(self) -> None:
        if self.state in {RuntimeState.RUNNING, RuntimeState.FAULT}:
            return
        self.registry.start_runtime()
        self.state = RuntimeState.RUNNING
        self.event_bus.emit("runtime_state", "runtime started", state=self.state.value)

    def pause(self) -> None:
        if self.state == RuntimeState.RUNNING:
            self.state = RuntimeState.PAUSED
            self.event_bus.emit("runtime_state", "runtime paused", state=self.state.value)

    def stop(self, reason: str = "stopped") -> None:
        bridge = self._motion_bridge()
        if bridge:
            try:
                bridge.call("stop", reason)
            except Exception as exc:
                self.event_bus.emit("plugin_fault", "motion stop failed", plugin=bridge.id, error=str(exc))
        if self.active_skill:
            try:
                self.active_skill.slot.call("cancel", reason)
            except Exception as exc:
                self.event_bus.emit(
                    "plugin_fault",
                    "skill cancel failed",
                    plugin=self.active_skill.slot.id,
                    error=str(exc),
                )
            self.active_skill = None
        try:
            self.registry.stop_runtime(reason)
        except Exception as exc:
            self.event_bus.emit("plugin_fault", "runtime plugin stop failed", error=str(exc))
        self.state = RuntimeState.IDLE
        self.event_bus.emit("runtime_state", reason, state=self.state.value)

    def tick(self) -> None:
        if self.state != RuntimeState.RUNNING:
            return
        for slot in self.registry.list():
            if slot.enabled and slot.has_method("on_tick"):
                slot.cast("on_tick", self.world, coalesce_key="runtime_tick")

        vision_provider = self._vision_provider()
        scan_provider = self._scan_provider()
        motion_bridge = self._motion_bridge()

        vision = self._read_vision(vision_provider)
        scan = self._read_scan(scan_provider)
        robot = self._read_robot_state(motion_bridge)
        self.world = self.world_builder.update(vision, scan, robot)
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

        for rule in self._rules():
            for decision in rule.call("evaluate_world", self.world):
                if not decision.allowed:
                    self._fault(decision.reason or "world rule rejected")
                    return

        goal = self._select_goal()
        if goal is None:
            self.event_bus.emit_throttled("policy", "no goal selected", interval_sec=1.0)
            return

        skill = self._select_or_continue_skill(goal)
        if skill is None:
            self.event_bus.emit_throttled(
                "skill",
                "no skill can run",
                interval_sec=1.0,
                key=f"skill:no_skill:{goal.type}",
                goal=goal.type,
            )
            return

        result = skill.call("tick", self.world)
        intent = self.safety.filter_intent(self.world, result.intent)
        for rule in self._rules():
            decision = rule.call("evaluate_intent", self.world, intent)
            if not decision.allowed:
                if motion_bridge:
                    motion_bridge.call("stop", decision.reason)
                self.event_bus.emit("rule_rejected", decision.reason, severity=decision.severity)
                return

        if motion_bridge is None:
            self.event_bus.emit_throttled(
                "motion",
                "motion bridge unavailable, intent dropped",
                interval_sec=1.0,
                note=intent.note,
                status=result.status,
            )
        else:
            motion_bridge.call("send", intent)
            self.event_bus.emit_throttled(
                "motion",
                "intent sent",
                interval_sec=0.5,
                key="motion:intent_sent",
                note=intent.note,
                status=result.status,
            )

        if result.status in {"done", "failed"}:
            self.event_bus.emit("skill", f"skill {result.status}", reason=result.reason)
            self.active_skill = None

    def _select_goal(self) -> Goal | None:
        policy_slot = self.registry.get_slot("policy")
        return policy_slot.call("select_goal", self.world) if policy_slot else None

    def _select_or_continue_skill(self, goal: Goal) -> PluginSlot | None:
        if self.active_skill and self.active_skill.goal == goal:
            return self.active_skill.slot

        if self.active_skill:
            self.active_skill.slot.call("cancel", "replaced by new goal")
            self.active_skill = None

        for slot in self.registry.list():
            if slot.kind != "skill" or not slot.enabled:
                continue
            if slot.call("can_run", self.world, goal):
                slot.call("start", self.world, goal)
                self.active_skill = ActiveSkill(slot=slot, goal=goal)
                self.event_bus.emit("skill", "skill started", skill=slot.id, goal=goal.type)
                return slot
        return None

    def _rules(self) -> list[PluginSlot]:
        return [slot for slot in self.registry.list() if slot.kind == "rule" and slot.enabled]

    def _vision_provider(self) -> PluginSlot | None:
        return self.registry.get_slot("vision")

    def _scan_provider(self) -> PluginSlot | None:
        return self.registry.get_slot("scan")

    def _motion_bridge(self) -> PluginSlot | None:
        return self.registry.get_slot("motion")

    def _read_vision(self, provider: PluginSlot | None) -> VisionResult:
        if provider is None:
            self.event_bus.emit_throttled("vision", "vision provider unavailable", interval_sec=1.0)
            return VisionResult(frame_id=0, timestamp=self.world.timestamp, metadata={"source": "missing_vision"})
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
            self.event_bus.emit("plugin_fault", "scan read failed", plugin=provider.id, error=str(exc))
            return None

    def _read_robot_state(self, bridge: PluginSlot | None) -> RobotState:
        if bridge is None:
            self.event_bus.emit_throttled("motion", "motion bridge unavailable", interval_sec=1.0)
            return RobotState(link_ok=False, metadata={"source": "missing_motion"})
        return bridge.call("read_state")

    def _fault(self, reason: str) -> None:
        bridge = self._motion_bridge()
        if bridge:
            try:
                bridge.call("stop", reason)
            except Exception as exc:
                self.event_bus.emit("plugin_fault", "fault stop failed", plugin=bridge.id, error=str(exc))
        self.state = RuntimeState.FAULT
        self.event_bus.emit("fault", reason, state=self.state.value)

    def fail(self, reason: str) -> None:
        self._fault(reason)
