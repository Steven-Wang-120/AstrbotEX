from __future__ import annotations

import math

from astrbot_ex.core.models import Intent, MotionIntent, WorldState


class SafetyGuard:
    def __init__(
        self,
        max_vx: float = 0.35,
        max_vy: float = 0.35,
        max_wz: float = 1.2,
        max_duration_ms: int = 1000,
    ) -> None:
        self.max_vx = max_vx
        self.max_vy = max_vy
        self.max_wz = max_wz
        self.max_duration_ms = max_duration_ms

    def filter_intent(self, world: WorldState, intent: Intent) -> Intent:
        if world.robot.estop:
            return Intent(motion=MotionIntent(), note="blocked by estop")
        if intent.motion is None:
            return intent
        motion = intent.motion
        if not all(math.isfinite(value) for value in (motion.vx, motion.vy, motion.wz)):
            return Intent(motion=MotionIntent(), note="blocked by non-finite motion")
        return Intent(
            motion=MotionIntent(
                vx=self._clamp(motion.vx, self.max_vx),
                vy=self._clamp(motion.vy, self.max_vy),
                wz=self._clamp(motion.wz, self.max_wz),
                duration_ms=self._clamp_duration(motion.duration_ms),
                metadata=motion.metadata,
            ),
            actuators=intent.actuators,
            note=intent.note,
        )

    @staticmethod
    def _clamp(value: float, limit: float) -> float:
        if not math.isfinite(value):
            return 0.0
        return max(-limit, min(limit, value))

    def _clamp_duration(self, value: int | float) -> int:
        if isinstance(value, bool):
            numeric_value = int(value)
        elif isinstance(value, int):
            numeric_value = value
        elif isinstance(value, float):
            if not math.isfinite(value):
                return self.max_duration_ms if value > 0.0 else 1
            numeric_value = int(value)
        else:
            return 1
        return max(1, min(self.max_duration_ms, int(numeric_value)))
