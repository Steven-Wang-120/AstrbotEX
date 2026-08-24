from __future__ import annotations

import math
import unittest

from astrbot_ex.core.models import Intent, MotionIntent, WorldState
from astrbot_ex.core.safety import SafetyGuard


class SafetyGuardTest(unittest.TestCase):
    def test_nonfinite_motion_returns_zero_motion(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                result = SafetyGuard().filter_intent(
                    WorldState(),
                    Intent(motion=MotionIntent(vx=value, wz=value, duration_ms=1_000_000_000)),
                )

                self.assertEqual(result.motion.vx, 0.0)
                self.assertEqual(result.motion.vy, 0.0)
                self.assertEqual(result.motion.wz, 0.0)
                self.assertGreaterEqual(result.motion.duration_ms, 1)
                self.assertLessEqual(result.motion.duration_ms, 1000)
                self.assertIn("non-finite", result.note)

    def test_one_nonfinite_axis_invalidates_all_motion_axes(self) -> None:
        result = SafetyGuard().filter_intent(
            WorldState(),
            Intent(motion=MotionIntent(vx=0.2, vy=-0.1, wz=float("nan"))),
        )

        self.assertEqual(result.motion.vx, 0.0)
        self.assertEqual(result.motion.vy, 0.0)
        self.assertEqual(result.motion.wz, 0.0)

    def test_finite_motion_values_are_clamped(self) -> None:
        result = SafetyGuard().filter_intent(
            WorldState(),
            Intent(motion=MotionIntent(vx=10.0, vy=-10.0, wz=5.0)),
        )
        self.assertEqual(result.motion.vx, 0.35)
        self.assertEqual(result.motion.vy, -0.35)
        self.assertEqual(result.motion.wz, 1.2)

        negative = SafetyGuard().filter_intent(
            WorldState(),
            Intent(motion=MotionIntent(vx=-10.0)),
        )
        self.assertEqual(negative.motion.vx, -0.35)

        unchanged = SafetyGuard().filter_intent(
            WorldState(),
            Intent(motion=MotionIntent(vx=0.1)),
        )
        self.assertEqual(unchanged.motion.vx, 0.1)

    def test_duration_is_bounded_and_integer(self) -> None:
        guard = SafetyGuard()
        for value, expected in ((1_000_000_000, 1000), (0, 1), (-50, 1), (500, 500)):
            with self.subTest(value=value):
                result = guard.filter_intent(
                    WorldState(),
                    Intent(motion=MotionIntent(duration_ms=value)),
                )
                self.assertEqual(result.motion.duration_ms, expected)
                self.assertIsInstance(result.motion.duration_ms, int)

        configured = SafetyGuard(max_duration_ms=250).filter_intent(
            WorldState(),
            Intent(motion=MotionIntent(duration_ms=500)),
        )
        self.assertEqual(configured.motion.duration_ms, 250)

        for value, expected in ((float("inf"), 1000), (float("-inf"), 1), (float("nan"), 1)):
            with self.subTest(value=value):
                result = guard.filter_intent(
                    WorldState(),
                    Intent(motion=MotionIntent(duration_ms=value)),
                )
                self.assertEqual(result.motion.duration_ms, expected)
                self.assertIsInstance(result.motion.duration_ms, int)

    def test_estop_returns_blocked_zero_motion(self) -> None:
        world = WorldState()
        world.robot.estop = True
        result = SafetyGuard().filter_intent(world, Intent(motion=MotionIntent(vx=0.2)))

        self.assertEqual(result.note, "blocked by estop")
        self.assertEqual(result.motion, MotionIntent())

    def test_none_motion_returns_same_intent(self) -> None:
        intent = Intent()

        self.assertIs(SafetyGuard().filter_intent(WorldState(), intent), intent)

    def test_clamp_rejects_nonfinite_values(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertEqual(SafetyGuard._clamp(value, 0.35), 0.0)
                self.assertTrue(math.isfinite(SafetyGuard._clamp(value, 0.35)))


if __name__ == "__main__":
    unittest.main()
