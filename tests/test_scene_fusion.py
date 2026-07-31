from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from astrbot_ex.core.models import Entity, ScanResult, VisionResult
from astrbot_ex.core.perception_config import (
    CameraConfig,
    FusionConfig,
    PerceptionConfig,
    load_perception_config,
)
from astrbot_ex.core.scene_fusion import SceneFusion


class SceneFusionTest(unittest.TestCase):
    def config(
        self,
        *,
        x_to_lidar_angle_sign: float = -1.0,
        to_lidar_yaw_offset_deg: float = 0.0,
        range_select_method: str = "min",
        range_window_deg: float = 3.0,
    ) -> PerceptionConfig:
        return PerceptionConfig(
            camera=CameraConfig(
                hfov_deg=90.0,
                image_width_px=640,
                image_height_px=480,
                to_lidar_yaw_offset_deg=to_lidar_yaw_offset_deg,
                x_to_lidar_angle_sign=x_to_lidar_angle_sign,
            ),
            fusion=FusionConfig(
                bearing_tolerance_deg=8.0,
                time_window_ms=150.0,
                range_min_m=0.05,
                range_max_m=12.0,
                range_window_deg=range_window_deg,
                range_select_method=range_select_method,
            ),
        )

    def entity(self, bbox_px: tuple[int, int, int, int] | None = (316, 210, 24, 24)) -> Entity:
        return Entity(id="entity-1", type="rescue_target", bbox_px=bbox_px)

    def vision(
        self,
        *,
        timestamp: float = 0.0,
        entities: list[Entity] | None = None,
    ) -> VisionResult:
        return VisionResult(
            frame_id=1,
            timestamp=timestamp,
            entities=entities if entities is not None else [self.entity()],
        )

    def scan_from_degrees(
        self,
        start_deg: float,
        ranges: list[float],
        *,
        step_deg: float = 1.0,
        timestamp: float = 0.0,
    ) -> ScanResult:
        return ScanResult(
            frame_id=1,
            timestamp=timestamp,
            angle_min_rad=math.radians(start_deg),
            angle_max_rad=math.radians(start_deg + step_deg * max(len(ranges) - 1, 0)),
            angle_increment_rad=math.radians(step_deg),
            ranges=ranges,
        )

    def test_empty_vision_and_empty_scan_is_not_degraded(self) -> None:
        fusion = SceneFusion(self.config())
        scan = self.scan_from_degrees(0.0, [])

        fused = fusion.fuse(self.vision(entities=[]), scan)

        self.assertEqual(fused.entities, [])
        self.assertEqual(fused.obstacles, [])
        self.assertFalse(fused.degraded)
        self.assertEqual(fused.notes, [])

    def test_scan_none_degrades_and_clears_ranges(self) -> None:
        fusion = SceneFusion(self.config())

        fused = fusion.fuse(self.vision(), None)

        self.assertTrue(fused.degraded)
        self.assertIn("scan unavailable", fused.notes)
        self.assertEqual(len(fused.entities), 1)
        self.assertAlmostEqual(fused.entities[0].bearing_deg or 0.0, 1.4321, places=3)
        self.assertIsNone(fused.entities[0].range_m)
        self.assertIsNone(fused.entities[0].range_quality)

    def test_bbox_bearing_and_matching_range(self) -> None:
        fusion = SceneFusion(self.config())
        scan = self.scan_from_degrees(
            -5.0,
            [math.inf, math.inf, math.inf, math.inf, math.inf, math.inf, 2.4],
        )

        fused = fusion.fuse(self.vision(), scan)
        entity = fused.entities[0]

        self.assertIsNotNone(entity.bearing_deg)
        self.assertAlmostEqual(entity.bearing_deg or 0.0, 1.4321, places=3)
        self.assertEqual(entity.range_m, 2.4)
        self.assertIsNotNone(entity.range_quality)
        self.assertGreater(entity.range_quality or 0.0, 0.0)
        self.assertLessEqual(entity.range_quality or 0.0, 1.0)

    def test_unmatched_scan_point_becomes_obstacle(self) -> None:
        fusion = SceneFusion(self.config())
        scan = self.scan_from_degrees(20.0, [2.0])

        fused = fusion.fuse(self.vision(), scan)

        self.assertIsNone(fused.entities[0].range_m)
        self.assertIsNone(fused.entities[0].range_quality)
        self.assertEqual(len(fused.obstacles), 1)
        self.assertEqual(fused.obstacles[0].id, "obstacle_0")
        self.assertEqual(fused.obstacles[0].point_count, 1)

    def test_window_selects_min_range(self) -> None:
        fusion = SceneFusion(self.config())
        scan = self.scan_from_degrees(
            -5.0,
            [
                math.inf,
                math.inf,
                math.inf,
                math.inf,
                math.inf,
                2.0,
                1.5,
                3.0,
                math.inf,
                math.inf,
                math.inf,
            ],
        )

        fused = fusion.fuse(self.vision(), scan)

        self.assertEqual(fused.entities[0].range_m, 1.5)

    def test_window_selects_median_range(self) -> None:
        fusion = SceneFusion(self.config(range_select_method="median"))
        scan = self.scan_from_degrees(
            -5.0,
            [
                math.inf,
                math.inf,
                math.inf,
                math.inf,
                math.inf,
                2.0,
                1.5,
                3.0,
                math.inf,
                math.inf,
                math.inf,
            ],
        )

        fused = fusion.fuse(self.vision(), scan)

        self.assertEqual(fused.entities[0].range_m, 2.0)

    def test_angle_sign_flip_changes_bearing_sign(self) -> None:
        fusion = SceneFusion(self.config(x_to_lidar_angle_sign=1.0))

        fused = fusion.fuse(self.vision(), self.scan_from_degrees(0.0, []))

        self.assertAlmostEqual(fused.entities[0].bearing_deg or 0.0, -1.4321, places=3)

    def test_yaw_offset_is_added_to_bearing(self) -> None:
        base = SceneFusion(self.config()).fuse(self.vision(), self.scan_from_degrees(0.0, []))
        offset = SceneFusion(self.config(to_lidar_yaw_offset_deg=10.0)).fuse(
            self.vision(), self.scan_from_degrees(0.0, [])
        )

        self.assertIsNotNone(base.entities[0].bearing_deg)
        self.assertIsNotNone(offset.entities[0].bearing_deg)
        self.assertAlmostEqual(
            (offset.entities[0].bearing_deg or 0.0) - (base.entities[0].bearing_deg or 0.0),
            10.0,
            places=3,
        )

    def test_angle_wrap_matches_across_pi_boundary(self) -> None:
        fusion = SceneFusion(self.config(to_lidar_yaw_offset_deg=179.0))
        ranges = [math.inf] * 360
        ranges[1] = 4.0
        scan = self.scan_from_degrees(-180.0, ranges)
        vision = self.vision(entities=[self.entity(bbox_px=(308, 210, 24, 24))])

        fused = fusion.fuse(vision, scan)

        self.assertAlmostEqual(fused.entities[0].bearing_deg or 0.0, 179.0, places=3)
        self.assertEqual(fused.entities[0].range_m, 4.0)

    def test_timestamp_misalignment_degrades_but_still_matches(self) -> None:
        fusion = SceneFusion(self.config())
        scan = self.scan_from_degrees(
            -5.0,
            [math.inf, math.inf, math.inf, math.inf, math.inf, math.inf, 2.4],
            timestamp=0.5,
        )

        fused = fusion.fuse(self.vision(timestamp=0.0), scan)

        self.assertTrue(fused.degraded)
        self.assertTrue(any("timestamps misaligned" in note for note in fused.notes))
        self.assertEqual(fused.entities[0].range_m, 2.4)

    def test_fuse_does_not_modify_input_entity(self) -> None:
        fusion = SceneFusion(self.config())
        entity = Entity(
            id="entity-1",
            type="rescue_target",
            bbox_px=(316, 210, 24, 24),
            range_m=9.0,
            range_quality=0.25,
        )
        scan = self.scan_from_degrees(-5.0, [math.inf, math.inf, math.inf, math.inf, math.inf, 2.0])

        fused = fusion.fuse(self.vision(entities=[entity]), scan)

        self.assertEqual(fused.entities[0].range_m, 2.0)
        self.assertEqual(entity.range_m, 9.0)
        self.assertEqual(entity.range_quality, 0.25)

    def test_consumed_points_are_not_repeated_as_obstacles(self) -> None:
        fusion = SceneFusion(self.config())
        ranges = [math.inf] * 26
        ranges[5] = 2.0
        ranges[6] = 1.5
        ranges[7] = 3.0
        ranges[25] = 4.5
        scan = self.scan_from_degrees(-5.0, ranges)

        fused = fusion.fuse(self.vision(), scan)

        self.assertEqual(fused.entities[0].range_m, 1.5)
        self.assertEqual(sum(cluster.point_count for cluster in fused.obstacles), 1)
        self.assertEqual(sum(cluster.point_count for cluster in fused.obstacles) + 3, 4)

    def test_load_perception_config_rejects_inverted_range_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "perception.json"
            self.write_config(path, range_min_m=5.0, range_max_m=1.0)

            with self.assertRaisesRegex(
                ValueError,
                r"fusion\.range_min_m=5\.0.*fusion\.range_max_m=1\.0",
            ):
                load_perception_config(path)

    def test_load_perception_config_allows_zero_range_max(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "perception.json"
            self.write_config(path, range_min_m=5.0, range_max_m=0.0)

            config = load_perception_config(path)

            self.assertEqual(config.fusion.range_min_m, 5.0)
            self.assertEqual(config.fusion.range_max_m, 0.0)

    def write_config(self, path: Path, *, range_min_m: float, range_max_m: float) -> None:
        path.write_text(
            json.dumps(
                {
                    "camera": {
                        "hfov_deg": 90.0,
                        "image_width_px": 640,
                        "image_height_px": 480,
                        "to_lidar_yaw_offset_deg": 0.0,
                        "x_to_lidar_angle_sign": -1.0,
                    },
                    "fusion": {
                        "bearing_tolerance_deg": 8.0,
                        "time_window_ms": 150.0,
                        "range_min_m": range_min_m,
                        "range_max_m": range_max_m,
                        "range_window_deg": 3.0,
                        "range_select_method": "min",
                    },
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
