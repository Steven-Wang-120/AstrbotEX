from __future__ import annotations

import argparse
import math
import time
from collections import deque
from typing import Iterable

import rclpy
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import OccupancyGrid, Path
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import LaserScan


class LidarScanVisualizer(Node):
    def __init__(
        self,
        scan_topic: str,
        points_topic: str,
        map_topic: str,
        map_size_m: float,
        resolution_m: float,
        max_points: int,
        publish_map: bool,
        hit_radius_cells: int,
        decay_sec: float,
        ray_step_cells: int,
        map_publish_hz: float,
    ) -> None:
        super().__init__("astrbotex_lidar_scan_visualizer")
        self._points_pub = self.create_publisher(Path, points_topic, 10)
        self._map_pub = self.create_publisher(OccupancyGrid, map_topic, 1)
        self._map_size_m = float(map_size_m)
        self._resolution_m = float(resolution_m)
        self._max_points = int(max_points)
        self._publish_map = bool(publish_map)
        self._hit_radius_cells = max(0, int(hit_radius_cells))
        self._decay_sec = max(0.0, float(decay_sec))
        self._ray_step_m = max(self._resolution_m, float(ray_step_cells) * self._resolution_m)
        self._map_interval_sec = 1.0 / max(0.1, float(map_publish_hz))
        self._last_map_publish_at = 0.0
        self._point_history: deque[tuple[float, list[tuple[float, float]]]] = deque()
        self.create_subscription(LaserScan, scan_topic, self._on_scan, qos_profile_sensor_data)
        self.get_logger().info(
            f"subscribed {scan_topic}; publishing points={points_topic}, map={map_topic}"
        )

    def _on_scan(self, msg: LaserScan) -> None:
        points = list(self._scan_points(msg))
        path = Path()
        path.header = msg.header
        path.poses = [self._pose_from_xy(msg, x, y) for x, y in points]
        self._points_pub.publish(path)
        now = time.monotonic()
        if self._publish_map and now - self._last_map_publish_at >= self._map_interval_sec:
            self._last_map_publish_at = now
            self._map_pub.publish(self._map_from_points(msg, points))

    def _scan_points(self, msg: LaserScan) -> Iterable[tuple[float, float]]:
        ranges = list(msg.ranges)
        if not ranges:
            return []
        stride = max(1, math.ceil(len(ranges) / max(1, self._max_points)))
        points: list[tuple[float, float]] = []
        for index in range(0, len(ranges), stride):
            distance = float(ranges[index])
            if not math.isfinite(distance):
                continue
            if distance < float(msg.range_min) or distance > float(msg.range_max):
                continue
            angle = float(msg.angle_min) + float(msg.angle_increment) * index
            points.append((math.cos(angle) * distance, math.sin(angle) * distance))
        return points

    @staticmethod
    def _pose_from_xy(msg: LaserScan, x: float, y: float) -> PoseStamped:
        pose = PoseStamped()
        pose.header = msg.header
        pose.pose.position.x = float(x)
        pose.pose.position.y = float(y)
        pose.pose.position.z = 0.0
        pose.pose.orientation.w = 1.0
        return pose

    def _map_from_points(self, msg: LaserScan, points: list[tuple[float, float]]) -> OccupancyGrid:
        width = max(1, int(round(self._map_size_m / self._resolution_m)))
        height = width
        origin = -self._map_size_m / 2.0
        data = [-1] * (width * height)

        center = width // 2
        if 0 <= center < width:
            data[center * width + center] = 0

        now = time.monotonic()
        self._point_history.append((now, points))
        while self._point_history and now - self._point_history[0][0] > self._decay_sec:
            self._point_history.popleft()

        for x, y in points:
            self._draw_free_ray(data, width, height, origin, x, y)

        history_points = points
        if self._decay_sec > 0:
            history_points = [point for _, batch in self._point_history for point in batch]
        for x, y in history_points:
            self._draw_hit(data, width, height, origin, x, y)

        grid = OccupancyGrid()
        grid.header = msg.header
        grid.info.resolution = float(self._resolution_m)
        grid.info.width = width
        grid.info.height = height
        grid.info.origin.position.x = origin
        grid.info.origin.position.y = origin
        grid.info.origin.orientation.w = 1.0
        grid.data = data
        return grid

    def _draw_free_ray(self, data: list[int], width: int, height: int, origin: float, x: float, y: float) -> None:
        distance = math.hypot(x, y)
        if distance <= self._resolution_m:
            return
        steps = max(1, int(distance / self._ray_step_m))
        for step in range(steps):
            ratio = step / steps
            self._set_cell(data, width, height, origin, x * ratio, y * ratio, 0, overwrite_hits=False)

    def _draw_hit(self, data: list[int], width: int, height: int, origin: float, x: float, y: float) -> None:
        cx = int((x - origin) / self._resolution_m)
        cy = int((y - origin) / self._resolution_m)
        radius = self._hit_radius_cells
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if dx * dx + dy * dy > radius * radius:
                    continue
                self._set_cell_index(data, width, height, cx + dx, cy + dy, 100)

    def _set_cell(
        self,
        data: list[int],
        width: int,
        height: int,
        origin: float,
        x: float,
        y: float,
        value: int,
        *,
        overwrite_hits: bool,
    ) -> None:
        cell_x = int((x - origin) / self._resolution_m)
        cell_y = int((y - origin) / self._resolution_m)
        if not overwrite_hits and 0 <= cell_x < width and 0 <= cell_y < height:
            if data[cell_y * width + cell_x] == 100:
                return
        self._set_cell_index(data, width, height, cell_x, cell_y, value)

    @staticmethod
    def _set_cell_index(data: list[int], width: int, height: int, cell_x: int, cell_y: int, value: int) -> None:
        if 0 <= cell_x < width and 0 <= cell_y < height:
            data[cell_y * width + cell_x] = value


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish phone-friendly lidar visualization topics from LaserScan.")
    parser.add_argument("--scan-topic", default="/scan")
    parser.add_argument("--points-topic", default="/scan_points")
    parser.add_argument("--map-topic", default="/map")
    parser.add_argument("--map-size-m", type=float, default=8.0)
    parser.add_argument("--resolution-m", type=float, default=0.05)
    parser.add_argument("--max-points", type=int, default=720)
    parser.add_argument("--hit-radius-cells", type=int, default=2)
    parser.add_argument("--decay-sec", type=float, default=0.8)
    parser.add_argument("--ray-step-cells", type=int, default=4)
    parser.add_argument("--map-publish-hz", type=float, default=5.0)
    parser.add_argument("--no-map", action="store_true")
    args = parser.parse_args()

    rclpy.init()
    node = LidarScanVisualizer(
        scan_topic=args.scan_topic,
        points_topic=args.points_topic,
        map_topic=args.map_topic,
        map_size_m=args.map_size_m,
        resolution_m=args.resolution_m,
        max_points=args.max_points,
        publish_map=not args.no_map,
        hit_radius_cells=args.hit_radius_cells,
        decay_sec=args.decay_sec,
        ray_step_cells=args.ray_step_cells,
        map_publish_hz=args.map_publish_hz,
    )
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
