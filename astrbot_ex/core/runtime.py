from __future__ import annotations

from dataclasses import dataclass
from threading import Event, RLock

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.interaction_core import InteractionCore
from astrbot_ex.core.models import Goal, RobotState, RuntimeState, WorldState
from astrbot_ex.core.perception_core import PerceptionCore
from astrbot_ex.core.plugin_registry import PluginRegistry, PluginSlot
from astrbot_ex.core.safety import SafetyGuard
from astrbot_ex.core.scene_fusion import SceneFusion
from astrbot_ex.core.topic_bus import TopicBus


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
        interaction_core: InteractionCore | None = None,
        perception: PerceptionCore | None = None,
        action_service=None,
        decision_service=None,
    ) -> None:
        self.registry = registry
        self.event_bus = event_bus or EventBus()
        self.safety = safety or SafetyGuard()
        self.topic_bus = topic_bus or TopicBus()
        self.perception_core = perception or PerceptionCore(
            registry=self.registry,
            event_bus=self.event_bus,
            fusion=fusion,
        )
        self.interaction_core = interaction_core
        self.action_service = action_service
        self.decision_service = decision_service
        self._stopping = Event()
        self._lifecycle_lock = RLock()
        self._lifecycle_epoch = 0
        self.state = RuntimeState.IDLE
        self.world = WorldState()
        self.active_skill: ActiveSkill | None = None

    def request_stop(self) -> None:
        with self._lifecycle_lock:
            self._lifecycle_epoch += 1
            self._stopping.set()
        if self.decision_service is not None:
            self.decision_service.request_stop("runtime_stop_requested")
        if self.action_service is not None:
            self.action_service.revoke()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.state in {RuntimeState.RUNNING, RuntimeState.FAULT}:
                return
            self._lifecycle_epoch += 1
            start_epoch = self._lifecycle_epoch
            self._stopping.clear()
        self.registry.set_runtime_mode(
            self.action_service.control_mode if self.action_service is not None else "legacy"
        )
        self.registry.start_runtime()
        with self._lifecycle_lock:
            if start_epoch != self._lifecycle_epoch or self._stopping.is_set():
                return
            self._stopping.clear()
        if self.interaction_core is not None:
            self.interaction_core.on_runtime_start()
        with self._lifecycle_lock:
            if start_epoch != self._lifecycle_epoch or self._stopping.is_set():
                return
            self.state = RuntimeState.RUNNING
        if self.action_service is not None:
            self.action_service.update_versions(runtime_state=self.state.value)
        self.event_bus.emit("runtime_state", "runtime started", state=self.state.value)

    def pause(self) -> None:
        self.request_stop()
        if self.state == RuntimeState.RUNNING:
            self.state = RuntimeState.PAUSED
            if self.action_service is not None:
                self.action_service.update_versions(runtime_state=self.state.value)
            self.event_bus.emit("runtime_state", "runtime paused", state=self.state.value)

    def stop(self, reason: str = "stopped") -> None:
        self.request_stop()
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
        if self.interaction_core is not None:
            self.interaction_core.on_runtime_stop(reason)
        try:
            self.registry.stop_runtime(reason)
        except Exception as exc:
            self.event_bus.emit("plugin_fault", "runtime plugin stop failed", error=str(exc))
        self.state = RuntimeState.IDLE
        if self.action_service is not None:
            self.action_service.update_versions(runtime_state=self.state.value)
        self.event_bus.emit("runtime_state", reason, state=self.state.value)

    def tick(self) -> None:
        if self.state != RuntimeState.RUNNING or self._stopping.is_set():
            return
        decision_mode = self.action_service is not None and self.action_service.control_mode == "decision"
        for slot in self.registry.list():
            if decision_mode and slot.kind in {"policy", "skill", "motion"}:
                continue
            if slot.enabled and slot.has_method("on_tick"):
                slot.cast("on_tick", self.world, coalesce_key="runtime_tick")

        motion_bridge = None if decision_mode else self._motion_bridge()

        robot = self._read_robot_state(motion_bridge)
        self.world = self.perception_core.update(robot, self.world)
        if self.interaction_core is not None:
            self.interaction_core.tick()

        for rule in self._rules():
            for decision in rule.call("evaluate_world", self.world):
                if not decision.allowed:
                    self._fault(decision.reason or "world rule rejected")
                    return

        if self.action_service is not None and self.action_service.control_mode == "decision":
            if self.decision_service is not None:
                self.decision_service.tick()
            return

        if self._stopping.is_set():
            return
        goal = self._select_goal()
        if goal is None:
            self.event_bus.emit_throttled("policy", "no goal selected", interval_sec=1.0)
            return

        if self._stopping.is_set():
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
        if self._stopping.is_set():
            return
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
            if self._stopping.is_set() or (self.action_service is not None and
                                           self.action_service.control_mode == "decision"):
                return
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

    def _motion_bridge(self) -> PluginSlot | None:
        return self.registry.get_slot("motion")

    def _read_robot_state(self, bridge: PluginSlot | None) -> RobotState:
        if bridge is None:
            self.event_bus.emit_throttled("motion", "motion bridge unavailable", interval_sec=1.0)
            return RobotState(link_ok=False, metadata={"source": "missing_motion"})
        return bridge.call("read_state")

    def _fault(self, reason: str) -> None:
        self.request_stop()
        bridge = self._motion_bridge()
        if bridge:
            try:
                bridge.call("stop", reason)
            except Exception as exc:
                self.event_bus.emit("plugin_fault", "fault stop failed", plugin=bridge.id, error=str(exc))
        self.state = RuntimeState.FAULT
        if self.action_service is not None:
            self.action_service.update_versions(runtime_state=self.state.value)
        self.event_bus.emit("fault", reason, state=self.state.value)

    def fail(self, reason: str) -> None:
        self._fault(reason)
