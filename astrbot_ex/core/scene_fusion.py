from __future__ import annotations

from dataclasses import dataclass, replace
from math import atan2, cos, degrees, isfinite, pi, radians, sin, tan
from statistics import median

from astrbot_ex.core.models import Entity, FusedScene, ScanCluster, ScanResult, VisionResult
from astrbot_ex.core.perception_config import PerceptionConfig

_FULL_TURN_RAD = 2.0 * pi


@dataclass(slots=True)
class _ScanPoint:
    index: int
    angle_rad: float
    range_m: float


class SceneFusion:
    def __init__(self, config: PerceptionConfig) -> None:
        self.config = config
        self._cx = config.camera.image_width_px / 2.0
        self._fx = self._cx / tan(radians(config.camera.hfov_deg / 2.0))
        self._range_window_rad = radians(config.fusion.range_window_deg)
        self._cluster_tolerance_rad = radians(config.fusion.bearing_tolerance_deg)

    def fuse(self, vision: VisionResult, scan: ScanResult | None) -> FusedScene:
        if scan is None:
            entities = [
                self._entity_with_bearing(entity, self._entity_bearing_rad(entity), None)
                for entity in vision.entities
            ]
            return FusedScene(
                timestamp=vision.timestamp,
                entities=entities,
                obstacles=[],
                degraded=True,
                notes=["scan unavailable"],
            )

        degraded = False
        notes: list[str] = []
        timestamp_delta_ms = abs(vision.timestamp - scan.timestamp) * 1000.0
        if timestamp_delta_ms > self.config.fusion.time_window_ms:
            degraded = True
            notes.append(f"timestamps misaligned: {timestamp_delta_ms:.1f}ms")

        valid_points = self._valid_scan_points(scan)
        valid_by_index = {point.index: point for point in valid_points}
        claimed_indices: set[int] = set()
        claimed_points: list[tuple[str, list[_ScanPoint]]] = []
        fused_entities: list[Entity] = []
        for entity in vision.entities:
            bearing_rad = self._entity_bearing_rad(entity)
            if bearing_rad is None:
                fused_entities.append(self._entity_with_bearing(entity, None, None))
                continue

            window_indices = self._window_indices(scan, bearing_rad)
            window_points = [
                valid_by_index[index] for index in window_indices if index in valid_by_index
            ]
            if not window_points:
                fused_entities.append(self._entity_with_bearing(entity, bearing_rad, None))
                continue

            selected_range = self._select_range(window_points)
            quality = len(window_points) / len(window_indices)
            unclaimed_points = [point for point in window_points if point.index not in claimed_indices]
            claimed_indices.update(point.index for point in unclaimed_points)
            if unclaimed_points:
                claimed_points.append((entity.id, unclaimed_points))
            fused_entities.append(self._entity_with_bearing(entity, bearing_rad, selected_range, quality))

        obstacles = self._cluster_points(
            [point for point in valid_points if point.index not in claimed_indices],
            attributed_to=None,
        )
        for entity_id, points in claimed_points:
            obstacles.extend(
                self._cluster_points(points, attributed_to=entity_id, start_index=len(obstacles))
            )
        return FusedScene(
            timestamp=vision.timestamp,
            entities=fused_entities,
            obstacles=obstacles,
            degraded=degraded,
            notes=notes,
        )

    def _entity_with_bearing(
        self,
        entity: Entity,
        bearing_rad: float | None,
        range_m: float | None,
        range_quality: float | None = None,
    ) -> Entity:
        bearing_deg = None if bearing_rad is None else degrees(_normalize_rad(bearing_rad))
        return replace(
            entity,
            bearing_deg=bearing_deg,
            range_m=range_m,
            range_quality=range_quality,
        )

    def _entity_bearing_rad(self, entity: Entity) -> float | None:
        if entity.bbox_px is None:
            return None

        x, _y, width, _height = entity.bbox_px
        u = x + width / 2.0
        camera_bearing_rad = atan2(self._cx - u, self._fx)
        bearing_rad = (
            self.config.camera.x_to_lidar_angle_sign * camera_bearing_rad
            + radians(self.config.camera.to_lidar_yaw_offset_deg)
        )
        return _normalize_rad(bearing_rad)

    def _valid_scan_points(self, scan: ScanResult) -> list[_ScanPoint]:
        points: list[_ScanPoint] = []
        for index, range_m in enumerate(scan.ranges):
            if not self._is_valid_range(scan, range_m):
                continue
            points.append(
                _ScanPoint(
                    index=index,
                    angle_rad=_normalize_rad(self._scan_angle_rad(scan, index)),
                    range_m=float(range_m),
                )
            )
        return points

    def _is_valid_range(self, scan: ScanResult, range_m: float) -> bool:
        if not isfinite(range_m):
            return False
        if range_m < scan.range_min_m:
            return False
        if scan.range_max_m > 0.0 and range_m > scan.range_max_m:
            return False
        if range_m < self.config.fusion.range_min_m:
            return False
        if self.config.fusion.range_max_m > 0.0 and range_m > self.config.fusion.range_max_m:
            return False
        return True

    def _window_indices(self, scan: ScanResult, bearing_rad: float) -> list[int]:
        if not scan.ranges:
            return []
        if self._range_window_rad == 0.0:
            closest_index = min(
                range(len(scan.ranges)),
                key=lambda index: _angular_distance_rad(
                    self._scan_angle_rad(scan, index), bearing_rad
                ),
            )
            return [closest_index]

        return [
            index
            for index in range(len(scan.ranges))
            if _angular_distance_rad(self._scan_angle_rad(scan, index), bearing_rad)
            <= self._range_window_rad
        ]

    def _scan_angle_rad(self, scan: ScanResult, index: int) -> float:
        return scan.angle_min_rad + index * scan.angle_increment_rad

    def _select_range(self, points: list[_ScanPoint]) -> float:
        values = [point.range_m for point in points]
        if self.config.fusion.range_select_method == "median":
            return float(median(values))
        return min(values)

    def _cluster_points(
        self,
        points: list[_ScanPoint],
        *,
        attributed_to: str | None,
        start_index: int = 0,
    ) -> list[ScanCluster]:
        if not points:
            return []

        ordered = sorted(points, key=lambda point: _positive_rad(point.angle_rad))
        groups: list[list[_ScanPoint]] = []
        current = [ordered[0]]
        previous = ordered[0]
        for point in ordered[1:]:
            if _angular_distance_rad(point.angle_rad, previous.angle_rad) < self._cluster_tolerance_rad:
                current.append(point)
            else:
                groups.append(current)
                current = [point]
            previous = point
        groups.append(current)

        if len(groups) > 1:
            first = groups[0][0]
            last = groups[-1][-1]
            if _angular_distance_rad(first.angle_rad, last.angle_rad) < self._cluster_tolerance_rad:
                groups[0] = groups[-1] + groups[0]
                groups.pop()

        return [
            self._scan_cluster(start_index + index, group, attributed_to=attributed_to)
            for index, group in enumerate(groups)
        ]

    def _scan_cluster(
        self,
        index: int,
        points: list[_ScanPoint],
        *,
        attributed_to: str | None,
    ) -> ScanCluster:
        angles = [point.angle_rad for point in points]
        return ScanCluster(
            id=f"obstacle_{index}",
            bearing_deg=degrees(_circular_mean_rad(angles)),
            range_m=min(point.range_m for point in points),
            width_deg=degrees(_circular_width_rad(angles)),
            point_count=len(points),
            attributed_to=attributed_to,
        )


def _normalize_rad(angle_rad: float) -> float:
    return (angle_rad + pi) % _FULL_TURN_RAD - pi


def _positive_rad(angle_rad: float) -> float:
    return _normalize_rad(angle_rad) % _FULL_TURN_RAD


def _angular_distance_rad(a: float, b: float) -> float:
    return abs(_normalize_rad(a - b))


def _circular_mean_rad(angles: list[float]) -> float:
    return _normalize_rad(atan2(sum(sin(angle) for angle in angles), sum(cos(angle) for angle in angles)))


def _circular_width_rad(angles: list[float]) -> float:
    if len(angles) <= 1:
        return 0.0

    positive_angles = sorted(_positive_rad(angle) for angle in angles)
    gaps = [
        positive_angles[index + 1] - positive_angles[index]
        for index in range(len(positive_angles) - 1)
    ]
    gaps.append(positive_angles[0] + _FULL_TURN_RAD - positive_angles[-1])
    return _FULL_TURN_RAD - max(gaps)
