from __future__ import annotations

from astrbot_ex.core.models import RobotState, VisionResult, WorldState


class WorldBuilder:
    def update(self, vision: VisionResult, robot: RobotState) -> WorldState:
        return WorldState(
            timestamp=vision.timestamp,
            entities=vision.entities,
            zones=vision.zones,
            robot=robot,
            task_state={"frame_id": vision.frame_id},
        )
