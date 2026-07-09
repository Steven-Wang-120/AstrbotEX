from __future__ import annotations

from dataclasses import dataclass

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.models import Goal, RobotState, RuntimeState, VisionResult, WorldState
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.safety import SafetyGuard
from astrbot_ex.core.topic_bus import TopicBus
from astrbot_ex.core.world_builder import WorldBuilder
from astrbot_ex.interfaces.motion import MotionBridge
from astrbot_ex.interfaces.policy import PolicyPlugin
from astrbot_ex.interfaces.rule import RulePlugin
from astrbot_ex.interfaces.skill import SkillPlugin
from astrbot_ex.interfaces.vision import VisionProvider


@dataclass(slots=True)
class ActiveSkill:
    plugin: SkillPlugin
    goal: Goal


class AstrBotEXRuntime:
    def __init__(
        self,
        registry: PluginRegistry,
        event_bus: EventBus | None = None,
        safety: SafetyGuard | None = None,
        topic_bus: TopicBus | None = None,
    ) -> None:
        self.registry = registry
        self.event_bus = event_bus or EventBus()
        self.safety = safety or SafetyGuard()
        self.topic_bus = topic_bus or TopicBus()
        self.world_builder = WorldBuilder()
        self.state = RuntimeState.IDLE
        self.world = WorldState()
        self.active_skill: ActiveSkill | None = None

    def start(self) -> None:
        if self.state in {RuntimeState.RUNNING, RuntimeState.FAULT}:
            return
        self.state = RuntimeState.RUNNING
        for slot in self.registry.list():
            if slot.enabled and hasattr(slot.plugin, "on_runtime_start"):
                slot.plugin.on_runtime_start()
        self.event_bus.emit("runtime_state", "runtime started", state=self.state.value)

    def pause(self) -> None:
        if self.state == RuntimeState.RUNNING:
            self.state = RuntimeState.PAUSED
            self.event_bus.emit("runtime_state", "runtime paused", state=self.state.value)

    def stop(self, reason: str = "stopped") -> None:
        bridge = self._motion_bridge()
        if bridge:
            bridge.stop(reason)
        if self.active_skill:
            self.active_skill.plugin.cancel(reason)
            self.active_skill = None
        for slot in self.registry.list():
            if slot.enabled and hasattr(slot.plugin, "on_runtime_stop"):
                slot.plugin.on_runtime_stop(reason)
        self.state = RuntimeState.IDLE
        self.event_bus.emit("runtime_state", reason, state=self.state.value)

    def tick(self) -> None:
        if self.state != RuntimeState.RUNNING:
            return
        for slot in self.registry.list():
            if slot.enabled and hasattr(slot.plugin, "on_tick"):
                slot.plugin.on_tick(self.world)

        vision_provider = self._vision_provider()
        motion_bridge = self._motion_bridge()

        vision = self._read_vision(vision_provider)
        robot = self._read_robot_state(motion_bridge)
        self.world = self.world_builder.update(vision, robot)
        self.event_bus.emit_throttled(
            "vision",
            "vision frame received",
            interval_sec=0.5,
            key="runtime:vision_frame",
            frame_id=vision.frame_id,
            entities=len(vision.entities),
        )

        for rule in self._rules():
            for decision in rule.evaluate_world(self.world):
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

        result = skill.tick(self.world)
        intent = self.safety.filter_intent(self.world, result.intent)
        for rule in self._rules():
            decision = rule.evaluate_intent(self.world, intent)
            if not decision.allowed:
                if motion_bridge:
                    motion_bridge.stop(decision.reason)
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
            motion_bridge.send(intent)
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
        policy = self.registry.get_one("policy")
        if policy is None:
            return None
        return policy.select_goal(self.world)

    def _select_or_continue_skill(self, goal: Goal) -> SkillPlugin | None:
        if self.active_skill and self.active_skill.goal == goal:
            return self.active_skill.plugin

        if self.active_skill:
            self.active_skill.plugin.cancel("replaced by new goal")
            self.active_skill = None

        for slot in self.registry.list():
            if slot.kind != "skill" or not slot.enabled:
                continue
            skill = slot.plugin
            if skill.can_run(self.world, goal):
                skill.start(self.world, goal)
                self.active_skill = ActiveSkill(plugin=skill, goal=goal)
                self.event_bus.emit("skill", "skill started", skill=skill.id, goal=goal.type)
                return skill
        return None

    def _rules(self) -> list[RulePlugin]:
        return [slot.plugin for slot in self.registry.list() if slot.kind == "rule" and slot.enabled]

    def _vision_provider(self) -> VisionProvider | None:
        return self.registry.get_one("vision")

    def _motion_bridge(self) -> MotionBridge | None:
        return self.registry.get_one("motion")

    def _read_vision(self, provider: VisionProvider | None) -> VisionResult:
        if provider is None:
            self.event_bus.emit_throttled("vision", "vision provider unavailable", interval_sec=1.0)
            return VisionResult(frame_id=0, timestamp=self.world.timestamp, metadata={"source": "missing_vision"})
        return provider.get_result()

    def _read_robot_state(self, bridge: MotionBridge | None) -> RobotState:
        if bridge is None:
            self.event_bus.emit_throttled("motion", "motion bridge unavailable", interval_sec=1.0)
            return RobotState(link_ok=False, metadata={"source": "missing_motion"})
        return bridge.read_state()

    def _fault(self, reason: str) -> None:
        bridge = self._motion_bridge()
        if bridge:
            bridge.stop(reason)
        self.state = RuntimeState.FAULT
        self.event_bus.emit("fault", reason, state=self.state.value)
