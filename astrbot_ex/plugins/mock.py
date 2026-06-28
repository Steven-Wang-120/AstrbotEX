from __future__ import annotations

from time import time

from astrbot_ex.core.models import (
    Entity,
    Goal,
    Intent,
    MotionIntent,
    RobotState,
    RuleDecision,
    SkillResult,
    VisionResult,
    WorldState,
    Zone,
)


class PluginLifecycleMixin:
    enabled = False

    def on_load(self) -> None:
        pass

    def on_enable(self) -> None:
        self.enabled = True

    def on_disable(self) -> None:
        self.enabled = False

    def on_unload(self) -> None:
        self.enabled = False


class MockVisionProvider(PluginLifecycleMixin):
    id = "mock_vision"
    name = "Mock Vision Provider"

    def __init__(self) -> None:
        self.frame_id = 0

    def get_result(self) -> VisionResult:
        self.frame_id += 1
        return VisionResult(
            frame_id=self.frame_id,
            timestamp=time(),
            entities=[
                Entity(
                    id="entity-1",
                    type="rescue_target",
                    semantic="own_normal",
                    confidence=0.92,
                    position=(1.2, 0.4),
                    bbox_px=(316, 210, 24, 24),
                )
            ],
            zones=[
                Zone(
                    id="zone-home",
                    role="home_safe_zone",
                    polygon=[(-0.3, -0.2), (0.3, -0.2), (0.3, 0.2), (-0.3, 0.2)],
                )
            ],
        )


class MockMotionBridge(PluginLifecycleMixin):
    id = "mock_motion"
    name = "Mock Motion Bridge"

    def __init__(self) -> None:
        self.last_intent: Intent | None = None
        self.stopped_reason = ""

    def send(self, intent: Intent) -> None:
        self.last_intent = intent

    def stop(self, reason: str) -> None:
        self.stopped_reason = reason
        self.last_intent = Intent(motion=MotionIntent(), note=reason)

    def read_state(self) -> RobotState:
        return RobotState(metadata={"source": "mock_motion"})


class BasicRulePlugin(PluginLifecycleMixin):
    id = "basic_rules"
    name = "Basic Rule Guard"

    def evaluate_world(self, world: WorldState) -> list[RuleDecision]:
        if world.robot.estop:
            return [RuleDecision(False, "robot estop is active", "error")]
        return [RuleDecision(True, "world accepted")]

    def evaluate_intent(self, world: WorldState, intent: Intent) -> RuleDecision:
        if intent.motion and abs(intent.motion.vx) > 0.5:
            return RuleDecision(False, "vx exceeds hard rule limit", "error")
        return RuleDecision(True, "intent accepted")


class NearestEntityPolicy(PluginLifecycleMixin):
    id = "nearest_entity_policy"
    name = "Nearest Entity Policy"

    def select_goal(self, world: WorldState) -> Goal | None:
        target = next((entity for entity in world.entities if entity.type == "rescue_target"), None)
        if not target:
            return None
        zone = next((zone for zone in world.zones if zone.role == "home_safe_zone"), None)
        return Goal(
            type="relocate_entity",
            target_entity_id=target.id,
            target_zone_id=zone.id if zone else None,
        )


class ApproachEntitySkill(PluginLifecycleMixin):
    id = "approach_entity"
    name = "Approach Entity Skill"

    def __init__(self) -> None:
        self.goal: Goal | None = None
        self.ticks = 0

    def can_run(self, world: WorldState, goal: Goal) -> bool:
        return goal.type == "relocate_entity" and goal.target_entity_id is not None

    def start(self, world: WorldState, goal: Goal) -> None:
        self.goal = goal
        self.ticks = 0

    def tick(self, world: WorldState) -> SkillResult:
        self.ticks += 1
        if self.ticks >= 3:
            return SkillResult(status="done", intent=Intent(note="target reached"))
        return SkillResult(
            status="running",
            intent=Intent(
                motion=MotionIntent(vx=0.18, vy=0.0, wz=0.0, duration_ms=100),
                note="approaching target",
            ),
        )

    def cancel(self, reason: str) -> None:
        self.goal = None
