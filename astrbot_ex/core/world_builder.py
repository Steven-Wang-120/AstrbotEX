from __future__ import annotations

from astrbot_ex.core.models import RobotState, ScanResult, VisionResult, WorldState
from astrbot_ex.core.scene_fusion import SceneFusion


class WorldBuilder:
    def __init__(self, fusion: SceneFusion | None = None) -> None:
        self.fusion = fusion

    def update(self, vision: VisionResult, scan: ScanResult | None, robot: RobotState) -> WorldState:
        scan_frame_id = scan.frame_id if scan is not None else None
        task_state = {
            "frame_id": vision.frame_id,
            "scan_frame_id": scan_frame_id,
            "perception_notes": [],
        }
        if self.fusion is None:
            return WorldState(
                timestamp=vision.timestamp,
                entities=vision.entities,
                zones=vision.zones,
                robot=robot,
                task_state=task_state,
                obstacles=[],
                perception_degraded=False,
            )

        fused = self.fusion.fuse(vision, scan)
        task_state["perception_notes"] = fused.notes
        return WorldState(
            timestamp=fused.timestamp,
            entities=fused.entities,
            zones=vision.zones,
            robot=robot,
            task_state=task_state,
            obstacles=fused.obstacles,
            perception_degraded=fused.degraded,
        )
