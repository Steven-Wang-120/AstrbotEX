from __future__ import annotations

from astrbot_ex.core.models import Intent, MotionIntent, WorldState


class SafetyGuard:
    def __init__(self, max_vx: float = 0.35, max_vy: float = 0.35, max_wz: float = 1.2) -> None:
        self.max_vx = max_vx
        self.max_vy = max_vy
        self.max_wz = max_wz

    def filter_intent(self, world: WorldState, intent: Intent) -> Intent:
        if world.robot.estop:
            return Intent(motion=MotionIntent(), note="blocked by estop")
        if intent.motion is None:
            return intent
        motion = intent.motion
        return Intent(
            motion=MotionIntent(
                vx=self._clamp(motion.vx, self.max_vx),
                vy=self._clamp(motion.vy, self.max_vy),
                wz=self._clamp(motion.wz, self.max_wz),
                duration_ms=motion.duration_ms,
                metadata=motion.metadata,
            ),
            actuators=intent.actuators,
            note=intent.note,
        )

    @staticmethod
    def _clamp(value: float, limit: float) -> float:
        return max(-limit, min(limit, value))
