from __future__ import annotations

import argparse
import json
import socket
import time
from typing import Any

import rclpy
from geometry_msgs.msg import Pose2D
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import LaserScan


class Ros2LidarToIpcBridge(Node):
    def __init__(self, scan_topic: str, pose_topic: str, socket_path: str, reconnect_sec: float) -> None:
        super().__init__("astrbotex_ros2_lidar_to_ipc_bridge")
        self._socket_path = socket_path
        self._reconnect_sec = reconnect_sec
        self._sock: socket.socket | None = None
        self._last_pose: dict[str, float] = {}
        self.create_subscription(LaserScan, scan_topic, self._on_scan, 10)
        if pose_topic:
            self.create_subscription(Pose2D, pose_topic, self._on_pose, 10)
        self.get_logger().info(f"subscribed scan={scan_topic}, pose={pose_topic or '<none>'}, socket={socket_path}")

    def destroy_node(self) -> bool:
        self._close_socket()
        return super().destroy_node()

    def _on_pose(self, msg: Pose2D) -> None:
        self._last_pose = {
            "robot_x_mm": float(msg.x) * 1000.0,
            "robot_y_mm": float(msg.y) * 1000.0,
            "robot_yaw_rad": float(msg.theta),
        }

    def _on_scan(self, msg: LaserScan) -> None:
        timestamp = float(msg.header.stamp.sec) + float(msg.header.stamp.nanosec) / 1_000_000_000.0
        if timestamp <= 0:
            timestamp = time.time()
        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "frame_id": msg.header.frame_id or "lidar_front",
            "ranges_m": [float(item) for item in msg.ranges],
            "angle_min_rad": float(msg.angle_min),
            "angle_increment_rad": float(msg.angle_increment),
            "range_min_m": float(msg.range_min),
            "range_max_m": float(msg.range_max),
            **self._last_pose,
        }
        self._send_payload(payload)

    def _send_payload(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")
        for attempt in range(2):
            try:
                sock = self._connect()
                sock.sendall(data)
                return
            except OSError as exc:
                self._close_socket()
                if attempt == 0:
                    time.sleep(self._reconnect_sec)
                    continue
                self.get_logger().error(f"failed to forward lidar payload: {exc}")

    def _connect(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self._socket_path)
        self._sock = sock
        self.get_logger().info("connected to AstrBotEX lidar IPC socket")
        return sock

    def _close_socket(self) -> None:
        if self._sock is None:
            return
        try:
            self._sock.close()
        finally:
            self._sock = None


def main() -> None:
    parser = argparse.ArgumentParser(description="Forward ROS2 LaserScan/Pose2D data to AstrBotEX lidar Unix Socket IPC.")
    parser.add_argument("--scan-topic", default="/scan")
    parser.add_argument("--pose-topic", default="/astrbotex/robot_pose")
    parser.add_argument(
        "--socket-path",
        default="/home/orangepi/astrbotex_deploy/deploy/astrbotex/data/ipc/astrbotex_lidar.sock",
    )
    parser.add_argument("--reconnect-sec", type=float, default=0.2)
    args = parser.parse_args()

    rclpy.init()
    node = Ros2LidarToIpcBridge(args.scan_topic, args.pose_topic, args.socket_path, args.reconnect_sec)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
