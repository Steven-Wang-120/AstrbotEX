from __future__ import annotations

import unittest

from astrbot_ex.core.models import Entity, RobotState, VisionResult, Zone
from astrbot_ex.core.world_builder import WorldBuilder


class WorldBuilderTest(unittest.TestCase):
    def test_without_fusion_preserves_legacy_world_fields(self) -> None:
        entities = [Entity(id="entity-1", type="rescue_target")]
        zones = [
            Zone(
                id="zone-home",
                role="home_safe_zone",
                polygon=[(-0.3, -0.2), (0.3, -0.2), (0.3, 0.2), (-0.3, 0.2)],
            )
        ]
        robot = RobotState(link_ok=True, metadata={"source": "test"})
        vision = VisionResult(frame_id=42, timestamp=123.5, entities=entities, zones=zones)

        world = WorldBuilder(fusion=None).update(vision, None, robot)

        self.assertEqual(world.timestamp, vision.timestamp)
        self.assertIs(world.entities, entities)
        self.assertIs(world.zones, zones)
        self.assertIs(world.robot, robot)
        self.assertEqual(world.obstacles, [])
        self.assertFalse(world.perception_degraded)
        self.assertEqual(
            world.task_state,
            {"frame_id": 42, "scan_frame_id": None, "perception_notes": []},
        )


if __name__ == "__main__":
    unittest.main()
