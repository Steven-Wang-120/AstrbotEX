from __future__ import annotations

import json
import math
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any

try:
    import rclpy
    from rclpy.node import Node
except ImportError:  # pragma: no cover
    rclpy = None
    Node = object

try:
    from sensor_msgs.msg import LaserScan
except ImportError:  # pragma: no cover
    LaserScan = None

try:
    from geometry_msgs.msg import Pose2D, PoseStamped
except ImportError:  # pragma: no cover
    Pose2D = None
    PoseStamped = None

try:
    from nav_msgs.msg import Odometry
except ImportError:  # pragma: no cover
    Odometry = None

try:
    from std_msgs.msg import String as RosString
except ImportError:  # pragma: no cover
    RosString = None


@dataclass(slots=True)
class ScanPacket:
    timestamp: float
    frame_id: str
    raw: dict[str, Any]
    pose: dict[str, float] = field(default_factory=dict)
    ranges_m: list[float] = field(default_factory=list)
    angle_min_rad: float | None = None
    angle_increment_rad: float | None = None
    range_min_m: float = 0.02
    range_max_m: float = 12.0
    front_distance_mm: float | None = None
    selected_target_distance_mm: float | None = None


class Plugin:
    id = "lidar_pose_plugin"
    name = "Lidar Pose Perception Plugin"

    def __init__(self, context) -> None:
        self.context = context
        self.config = dict(context.config or {})
        self.enabled = False
        self._lock = threading.RLock()
        self._node: Node | None = None
        self._subscriptions: list[Any] = []
        self._input_mode = ""
        self._ipc_server: socket.socket | None = None
        self._ipc_connection: socket.socket | None = None
        self._ipc_buffer = ""
        self._ipc_path = ""
        self._last_packet: ScanPacket | None = None
        self._last_pose: dict[str, float] = {}
        self._last_publish_at = 0.0
        self._seq = 0

    def on_load(self) -> None:
        self.context.event_bus.emit("plugin", "lidar pose plugin loaded", plugin=self.id)

    def on_enable(self) -> None:
        self.enabled = True

    def on_disable(self) -> None:
        self.enabled = False
        self._close_input("plugin disabled")

    def on_unload(self) -> None:
        self.enabled = False
        self._close_input("plugin unloaded")

    def on_runtime_start(self) -> None:
        self._open_input()

    def on_runtime_stop(self, reason: str) -> None:
        self._close_input(reason)

    def on_tick(self, world) -> None:
        if not self.enabled:
            return
        publish_hz = max(1.0, float(self.config.get("publish_hz", 10)))
        now = time.time()
        if now - self._last_publish_at < 1.0 / publish_hz:
            return
        self._last_publish_at = now
        perception = self._build_perception(now)
        self._publish_topics(perception)

    def on_worker_step(self) -> None:
        if self._input_mode == "ros2":
            if self._node is not None:
                timeout = min(0.05, max(0.001, float(self.config.get("spin_timeout_sec", 0.05))))
                rclpy.spin_once(self._node, timeout_sec=timeout)
            return
        if self._input_mode == "ipc_unix_socket":
            self._ipc_step()

    def _open_input(self) -> None:
        self._close_input("reopen")
        input_mode = str(self.config.get("input_mode", "ros2")).strip().lower()
        if input_mode in {"ros2", "ros"}:
            if rclpy is None:
                raise RuntimeError("rclpy is not installed; use ipc_unix_socket mode or install ROS2 Python packages")
            self._input_mode = "ros2"
            self._open_ros2()
        elif input_mode in {"ipc_unix_socket", "unix_socket", "ipc"}:
            self._input_mode = "ipc_unix_socket"
            self._open_ipc()
        else:
            raise ValueError(f"unsupported input_mode: {input_mode}")

    def _close_input(self, reason: str) -> None:
        self._close_ipc_connection()
        server = self._ipc_server
        self._ipc_server = None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        if self._ipc_path:
            try:
                if os.path.exists(self._ipc_path):
                    os.unlink(self._ipc_path)
            except OSError:
                pass
        self._ipc_path = ""
        node = self._node
        self._node = None
        self._subscriptions = []
        if node is not None:
            try:
                node.destroy_node()
            except Exception:
                pass
        if self._input_mode:
            self.context.event_bus.emit_throttled(
                "perception_plugin",
                "lidar link closed",
                interval_sec=1.0,
                key=f"{self.id}:closed",
                plugin=self.id,
                reason=reason,
                input_mode=self._input_mode,
            )
        self._input_mode = ""

    def _open_ros2(self) -> None:
        node_name = str(self.config.get("ros_node_name", self.id)).strip() or self.id
        scan_topic = str(self.config.get("scan_topic", "/scan")).strip()
        pose_topic = str(self.config.get("pose_topic", "/astrbotex/robot_pose")).strip()
        pose_type = str(self.config.get("pose_message_type", "geometry_msgs/msg/Pose2D")).strip()
        if not rclpy.ok():
            rclpy.init(args=None)
        self._node = rclpy.create_node(node_name)
        if LaserScan is None:
            raise RuntimeError("sensor_msgs.msg.LaserScan is not available")
        self._subscriptions.append(self._node.create_subscription(LaserScan, scan_topic, self._on_scan_message, 10))
        if pose_topic:
            pose_cls = self._pose_message_class(pose_type)
            self._subscriptions.append(self._node.create_subscription(pose_cls, pose_topic, self._on_pose_message, 10))
        self.context.event_bus.emit(
            "perception_plugin",
            "lidar ros2 subscription started",
            plugin=self.id,
            scan_topic=scan_topic,
            pose_topic=pose_topic,
            pose_message_type=pose_type,
        )

    def _pose_message_class(self, message_type: str) -> Any:
        value = message_type.lower()
        if value in {"geometry_msgs/msg/pose2d", "geometry_msgs/pose2d"}:
            if Pose2D is None:
                raise RuntimeError("geometry_msgs.msg.Pose2D is not available")
            return Pose2D
        if value in {"geometry_msgs/msg/posestamped", "geometry_msgs/posestamped"}:
            if PoseStamped is None:
                raise RuntimeError("geometry_msgs.msg.PoseStamped is not available")
            return PoseStamped
        if value in {"nav_msgs/msg/odometry", "nav_msgs/odometry"}:
            if Odometry is None:
                raise RuntimeError("nav_msgs.msg.Odometry is not available")
            return Odometry
        if value in {"std_msgs/msg/string", "std_msgs/string"}:
            if RosString is None:
                raise RuntimeError("std_msgs.msg.String is not available")
            return RosString
        raise ValueError(f"unsupported pose_message_type: {message_type}")

    def _on_scan_message(self, msg: Any) -> None:
        try:
            timestamp = self._stamp_to_sec(getattr(msg, "header", None))
            frame_id = str(getattr(getattr(msg, "header", None), "frame_id", "") or self.config.get("default_frame_id", "lidar_front"))
            ranges = [float(item) for item in list(getattr(msg, "ranges", []) or [])]
            with self._lock:
                pose = dict(self._last_pose)
            packet = ScanPacket(
                timestamp=timestamp,
                frame_id=frame_id,
                raw={
                    "message_type": "sensor_msgs/msg/LaserScan",
                    "ranges_count": len(ranges),
                    "angle_min_rad": float(getattr(msg, "angle_min", 0.0) or 0.0),
                    "angle_increment_rad": float(getattr(msg, "angle_increment", 0.0) or 0.0),
                    "range_min_m": float(getattr(msg, "range_min", 0.0) or 0.0),
                    "range_max_m": float(getattr(msg, "range_max", 0.0) or 0.0),
                    **pose,
                },
                pose=pose,
                ranges_m=ranges,
                angle_min_rad=float(getattr(msg, "angle_min", 0.0) or 0.0),
                angle_increment_rad=float(getattr(msg, "angle_increment", 0.0) or 0.0),
                range_min_m=float(getattr(msg, "range_min", 0.02) or 0.02),
                range_max_m=float(getattr(msg, "range_max", 12.0) or 12.0),
            )
            with self._lock:
                self._last_packet = packet
            self.context.event_bus.emit_throttled(
                "perception_plugin",
                "lidar scan received",
                interval_sec=1.0,
                key=f"{self.id}:scan",
                plugin=self.id,
                ranges=len(ranges),
                pose_valid=bool(pose),
            )
        except Exception as exc:
            self.context.event_bus.emit("perception_plugin", "lidar scan parse failed", plugin=self.id, severity="error", error=str(exc))

    def _on_pose_message(self, msg: Any) -> None:
        try:
            pose = self._pose_from_message(msg)
            with self._lock:
                self._last_pose = pose
                if self._last_packet is not None:
                    self._last_packet.pose = pose
            self.context.event_bus.emit_throttled(
                "perception_plugin",
                "lidar pose received",
                interval_sec=1.0,
                key=f"{self.id}:pose",
                plugin=self.id,
                pose_valid=bool(pose),
            )
        except Exception as exc:
            self.context.event_bus.emit("perception_plugin", "lidar pose parse failed", plugin=self.id, severity="error", error=str(exc))

    def _pose_from_message(self, msg: Any) -> dict[str, float]:
        if RosString is not None and isinstance(msg, RosString):
            payload = json.loads(msg.data)
            if not isinstance(payload, dict):
                raise ValueError("pose String payload must be JSON object")
            return self._parse_pose(payload)
        if Pose2D is not None and isinstance(msg, Pose2D):
            return {
                "robot_x_mm": float(msg.x) * 1000.0,
                "robot_y_mm": float(msg.y) * 1000.0,
                "robot_yaw_rad": self._normalize_angle(float(msg.theta)),
            }
        if PoseStamped is not None and isinstance(msg, PoseStamped):
            position = msg.pose.position
            orientation = msg.pose.orientation
            return {
                "robot_x_mm": float(position.x) * 1000.0,
                "robot_y_mm": float(position.y) * 1000.0,
                "robot_yaw_rad": self._yaw_from_quaternion(orientation),
            }
        if Odometry is not None and isinstance(msg, Odometry):
            position = msg.pose.pose.position
            orientation = msg.pose.pose.orientation
            return {
                "robot_x_mm": float(position.x) * 1000.0,
                "robot_y_mm": float(position.y) * 1000.0,
                "robot_yaw_rad": self._yaw_from_quaternion(orientation),
            }
        raise ValueError(f"unsupported pose message object: {type(msg).__name__}")

    def _open_ipc(self) -> None:
        path = str(self.config.get("ipc_socket_path", "/app/data/ipc/astrbotex_lidar.sock")).strip()
        backlog = int(self.config.get("ipc_socket_backlog", 1))
        if not path:
            raise ValueError("ipc_socket_path is required")
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("Unix domain sockets are not supported on this platform")
        timeout = min(0.05, max(0.001, float(self.config.get("ipc_read_timeout_sec", 0.2))))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path):
            os.unlink(path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.settimeout(timeout)
        server.bind(path)
        server.listen(backlog)
        try:
            os.chmod(path, 0o666)
        except OSError:
            pass
        self._ipc_server = server
        self._ipc_path = path
        self.context.event_bus.emit("perception_plugin", "lidar ipc socket started", plugin=self.id, path=path)

    def _ipc_step(self) -> None:
        if self._ipc_connection is None:
            if self._ipc_server is None:
                return
            try:
                connection, _ = self._ipc_server.accept()
            except socket.timeout:
                return
            connection.settimeout(self._ipc_server.gettimeout())
            self._ipc_connection = connection
            self._ipc_buffer = ""
            return
        try:
            chunk = self._ipc_connection.recv(65536)
        except socket.timeout:
            return
        except OSError:
            self._close_ipc_connection()
            return
        if not chunk:
            self._close_ipc_connection()
            return
        self._ipc_buffer += chunk.decode("utf-8", errors="replace")
        while "\n" in self._ipc_buffer:
            line, self._ipc_buffer = self._ipc_buffer.split("\n", 1)
            self._on_ipc_line(line)

    def _close_ipc_connection(self) -> None:
        connection = self._ipc_connection
        self._ipc_connection = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        if self._ipc_buffer.strip():
            self._on_ipc_line(self._ipc_buffer)
        self._ipc_buffer = ""

    def _on_ipc_line(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        try:
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("lidar IPC payload must be a JSON object")
            packet = self._parse_packet(payload)
            with self._lock:
                self._last_pose = dict(packet.pose)
                self._last_packet = packet
            self.context.event_bus.emit_throttled(
                "perception_plugin",
                "lidar packet received",
                interval_sec=1.0,
                key=f"{self.id}:packet",
                plugin=self.id,
                ranges=len(packet.ranges_m),
                pose_valid=bool(packet.pose),
            )
        except Exception as exc:
            self.context.event_bus.emit("perception_plugin", "lidar packet parse failed", plugin=self.id, severity="error", error=str(exc))

    def _parse_packet(self, payload: dict[str, Any]) -> ScanPacket:
        timestamp = self._parse_float(payload.get("timestamp")) or time.time()
        frame_id = str(payload.get("frame_id", self.config.get("default_frame_id", "lidar_front")))
        ranges_raw = payload.get("ranges_m", payload.get("ranges", payload.get("scan_ranges_m", [])))
        ranges_m = [float(item) for item in ranges_raw if self._parse_float(item) is not None] if isinstance(ranges_raw, list) else []
        return ScanPacket(
            timestamp=timestamp,
            frame_id=frame_id,
            raw=payload,
            pose=self._parse_pose(payload),
            ranges_m=ranges_m,
            angle_min_rad=self._parse_angle(payload, "angle_min"),
            angle_increment_rad=self._parse_angle(payload, "angle_increment"),
            range_min_m=self._parse_float(payload.get("range_min_m", payload.get("range_min"))) or 0.02,
            range_max_m=self._parse_float(payload.get("range_max_m", payload.get("range_max"))) or 12.0,
            front_distance_mm=self._parse_distance_mm(payload, "front_distance"),
            selected_target_distance_mm=self._parse_distance_mm(payload, "selected_target_distance"),
        )

    def _parse_pose(self, payload: dict[str, Any]) -> dict[str, float]:
        x = self._first_float(payload, ("robot_x_mm", "x_mm", "pose_x_mm"))
        y = self._first_float(payload, ("robot_y_mm", "y_mm", "pose_y_mm"))
        if x is None or y is None:
            generic_x = self._first_float(payload, ("robot_x", "x", "pose_x"))
            generic_y = self._first_float(payload, ("robot_y", "y", "pose_y"))
            if generic_x is not None and generic_y is not None:
                scale = 1000.0 if str(self.config.get("pose_unit", "mm")).lower() == "m" else 1.0
                x = generic_x * scale
                y = generic_y * scale

        yaw = self._first_float(payload, ("robot_yaw_rad", "yaw_rad", "pose_yaw_rad"))
        if yaw is None:
            yaw_deg = self._first_float(payload, ("robot_yaw_deg", "yaw_deg", "pose_yaw_deg"))
            if yaw_deg is not None:
                yaw = math.radians(yaw_deg)
        if yaw is None:
            generic_yaw = self._first_float(payload, ("robot_yaw", "yaw", "pose_yaw"))
            if generic_yaw is not None:
                yaw = math.radians(generic_yaw) if str(self.config.get("pose_yaw_unit", "rad")).lower() == "deg" else generic_yaw

        pose: dict[str, float] = {}
        if x is not None and y is not None:
            pose["robot_x_mm"] = x
            pose["robot_y_mm"] = y
        if yaw is not None:
            pose["robot_yaw_rad"] = self._normalize_angle(yaw)
        return pose

    def _build_perception(self, now: float) -> dict[str, Any]:
        with self._lock:
            packet = self._last_packet
        stale_after = max(1, int(self.config.get("stale_after_ms", 800))) / 1000.0
        packet_stale = packet is None or now - packet.timestamp > stale_after
        target_payload, target_stale = self._read_current_target(now)

        pose = {} if packet is None or packet_stale else dict(packet.pose)
        home = self._home_fields(pose)
        front_range = None if packet is None or packet_stale else packet.front_distance_mm
        if front_range is None and packet is not None and not packet_stale:
            sample = self._sample_range(packet, 0.0, math.radians(float(self.config.get("front_range_window_deg", 5.0))))
            front_range = sample["distance_mm"]

        target_x = self._parse_float(target_payload.get("target_x", target_payload.get("x"))) if target_payload else None
        target_valid = self._target_valid(target_payload) and not target_stale
        target_angle_rad = None
        target_range = None
        target_quality = 0.0
        if target_valid and target_x is not None:
            target_angle_rad = self._target_angle_from_x(target_x)
            if packet is not None and not packet_stale:
                if packet.selected_target_distance_mm is not None:
                    target_range = packet.selected_target_distance_mm
                    target_quality = 1.0
                else:
                    sample = self._sample_range(
                        packet,
                        target_angle_rad,
                        math.radians(float(self.config.get("target_range_window_deg", 3.0))),
                    )
                    target_range = sample["distance_mm"]
                    target_quality = sample["quality"]

        target_world = self._target_world_fields(pose, target_angle_rad, target_range)
        target_detected = bool(target_valid and target_range is not None)
        target_ignored = bool(target_detected and target_world.get("selected_target_in_own_safe_zone"))
        boundary = self._boundary_fields(pose)
        self._seq = (self._seq + 1) % 1_000_000
        return {
            "seq": self._seq,
            "timestamp": now,
            "source": self.id,
            "frame_id": packet.frame_id if packet else str(self.config.get("default_frame_id", "lidar_front")),
            "stale": packet_stale,
            "pose_valid": "robot_x_mm" in pose and "robot_y_mm" in pose and "robot_yaw_rad" in pose,
            **pose,
            **home,
            "front_distance_mm": front_range,
            **boundary,
            "selected_target_detected": target_detected,
            "selected_target_valid": bool(target_detected and not target_ignored),
            "selected_target_track_id": target_payload.get("track_id") if target_payload else None,
            "selected_target_color": target_payload.get("color") if target_payload else None,
            "selected_target_x": target_x,
            "selected_target_angle_rad": target_angle_rad,
            "selected_target_distance_mm": target_range,
            "selected_target_range_quality": round(float(target_quality), 3),
            **target_world,
            "selected_target_ignore": target_ignored,
            "selected_target_ignore_reason": "own_safe_zone" if target_ignored else "",
            "vision_target_stale": target_stale,
            "raw_target": target_payload,
        }

    def _read_current_target(self, now: float) -> tuple[dict[str, Any] | None, bool]:
        topic = str(self.config.get("vision_target_topic", "astrbotex_ros2_vision_plugin.current_target")).strip()
        message = self.context.topic_bus.get_latest(topic) if topic else None
        if message is None:
            return None, True
        stale_after = max(1, int(self.config.get("vision_target_stale_ms", 700))) / 1000.0
        return dict(message.payload), now - message.timestamp > stale_after

    def _target_valid(self, payload: dict[str, Any] | None) -> bool:
        if not payload:
            return False
        raw_x = payload.get("target_x", payload.get("x"))
        if isinstance(raw_x, str) and raw_x.strip().lower() == "empty":
            return False
        color = str(payload.get("color", "")).strip().lower()
        if color in {"empty", "none", "null"}:
            return False
        if payload.get("target_valid") is False:
            return False
        return self._parse_float(raw_x) is not None

    def _target_angle_from_x(self, target_x: float) -> float:
        hfov_rad = math.radians(float(self.config.get("camera_hfov_deg", 60.0)))
        offset_rad = math.radians(float(self.config.get("camera_to_lidar_yaw_offset_deg", 0.0)))
        sign = float(self.config.get("camera_x_to_lidar_angle_sign", -1.0))
        return self._normalize_angle(sign * target_x * hfov_rad / 2.0 + offset_rad)

    def _home_fields(self, pose: dict[str, float]) -> dict[str, Any]:
        x = pose.get("robot_x_mm")
        y = pose.get("robot_y_mm")
        yaw = pose.get("robot_yaw_rad")
        home_x = float(self.config.get("home_x_mm", 0.0))
        home_y = float(self.config.get("home_y_mm", 0.0))
        if x is None or y is None:
            return {
                "home_x_mm": home_x,
                "home_y_mm": home_y,
                "home_distance_mm": None,
                "home_bearing_rad": None,
                "heading_error_to_home_rad": None,
            }
        dx = home_x - x
        dy = home_y - y
        bearing = math.atan2(dy, dx)
        return {
            "home_x_mm": home_x,
            "home_y_mm": home_y,
            "home_distance_mm": math.hypot(dx, dy),
            "home_bearing_rad": self._normalize_angle(bearing),
            "heading_error_to_home_rad": self._normalize_angle(bearing - yaw) if yaw is not None else None,
        }

    def _target_world_fields(
        self,
        pose: dict[str, float],
        target_angle_rad: float | None,
        target_distance_mm: float | None,
    ) -> dict[str, Any]:
        x = pose.get("robot_x_mm")
        y = pose.get("robot_y_mm")
        yaw = pose.get("robot_yaw_rad")
        fields: dict[str, Any] = {
            "selected_target_world_x_mm": None,
            "selected_target_world_y_mm": None,
            "selected_target_world_bearing_rad": None,
            "selected_target_in_own_safe_zone": False,
            "own_safe_zone": self._own_safe_zone_payload(),
        }
        if x is None or y is None or yaw is None or target_angle_rad is None or target_distance_mm is None:
            return fields

        mode = str(self.config.get("target_projection_angle_mode", "robot_yaw_minus_lidar_angle")).strip().lower()
        if mode == "robot_yaw_plus_lidar_angle":
            bearing = yaw + target_angle_rad
        else:
            bearing = yaw - target_angle_rad
        bearing = self._normalize_angle(bearing)
        target_x = x + math.cos(bearing) * target_distance_mm
        target_y = y + math.sin(bearing) * target_distance_mm
        fields.update(
            {
                "selected_target_world_x_mm": target_x,
                "selected_target_world_y_mm": target_y,
                "selected_target_world_bearing_rad": bearing,
                "selected_target_in_own_safe_zone": self._point_in_own_safe_zone(target_x, target_y),
            }
        )
        return fields

    def _own_safe_zone_payload(self) -> dict[str, Any]:
        margin = float(self.config.get("own_safe_zone_margin_mm", 20.0))
        return {
            "enabled": bool(self.config.get("ignore_own_safe_zone_targets", True)),
            "min_x_mm": float(self.config.get("own_safe_zone_min_x_mm", 0.0)),
            "max_x_mm": float(self.config.get("own_safe_zone_max_x_mm", 300.0)),
            "min_y_mm": float(self.config.get("own_safe_zone_min_y_mm", -300.0)),
            "max_y_mm": float(self.config.get("own_safe_zone_max_y_mm", 300.0)),
            "margin_mm": margin,
        }

    def _point_in_own_safe_zone(self, x_mm: float, y_mm: float) -> bool:
        if not bool(self.config.get("ignore_own_safe_zone_targets", True)):
            return False
        margin = float(self.config.get("own_safe_zone_margin_mm", 20.0))
        min_x = float(self.config.get("own_safe_zone_min_x_mm", 0.0)) - margin
        max_x = float(self.config.get("own_safe_zone_max_x_mm", 300.0)) + margin
        min_y = float(self.config.get("own_safe_zone_min_y_mm", -300.0)) - margin
        max_y = float(self.config.get("own_safe_zone_max_y_mm", 300.0)) + margin
        return min_x <= x_mm <= max_x and min_y <= y_mm <= max_y

    def _boundary_fields(self, pose: dict[str, float]) -> dict[str, Any]:
        if not bool(self.config.get("boundary_check_enabled", True)):
            return {"boundary_risk": False, "boundary_reason": ""}
        x = pose.get("robot_x_mm")
        y = pose.get("robot_y_mm")
        if x is None or y is None:
            return {"boundary_risk": False, "boundary_reason": "pose_unavailable"}
        margin = float(self.config.get("boundary_margin_mm", 120.0))
        min_x = float(self.config.get("field_min_x_mm", 0.0))
        max_x = float(self.config.get("field_max_x_mm", 2400.0))
        min_y = float(self.config.get("field_min_y_mm", -1200.0))
        max_y = float(self.config.get("field_max_y_mm", 1200.0))
        reasons = []
        if x <= min_x + margin:
            reasons.append("near_min_x")
        if x >= max_x - margin:
            reasons.append("near_max_x")
        if y <= min_y + margin:
            reasons.append("near_min_y")
        if y >= max_y - margin:
            reasons.append("near_max_y")
        return {"boundary_risk": bool(reasons), "boundary_reason": ",".join(reasons)}

    def _sample_range(self, packet: ScanPacket, angle_rad: float, window_rad: float) -> dict[str, Any]:
        if not packet.ranges_m or packet.angle_min_rad is None or packet.angle_increment_rad in {None, 0}:
            return {"distance_mm": None, "quality": 0.0, "valid_count": 0}
        idx = self._angle_to_index(packet, angle_rad)
        if idx is None:
            return {"distance_mm": None, "quality": 0.0, "valid_count": 0}
        half_count = max(0, int(math.ceil(abs(window_rad / float(packet.angle_increment_rad)))))
        start = max(0, idx - half_count)
        end = min(len(packet.ranges_m), idx + half_count + 1)
        values = []
        for value in packet.ranges_m[start:end]:
            if not math.isfinite(value):
                continue
            if value < packet.range_min_m or value > packet.range_max_m:
                continue
            values.append(float(value))
        if not values:
            return {"distance_mm": None, "quality": 0.0, "valid_count": 0}
        values.sort()
        method = str(self.config.get("range_select_method", "min")).lower()
        selected = values[len(values) // 2] if method == "median" else values[0]
        total = max(1, end - start)
        return {"distance_mm": selected * 1000.0, "quality": min(1.0, len(values) / total), "valid_count": len(values)}

    def _angle_to_index(self, packet: ScanPacket, angle_rad: float) -> int | None:
        assert packet.angle_min_rad is not None
        assert packet.angle_increment_rad is not None
        angle_max = packet.angle_min_rad + packet.angle_increment_rad * (len(packet.ranges_m) - 1)
        candidates = [angle_rad, angle_rad + 2.0 * math.pi, angle_rad - 2.0 * math.pi]
        for candidate in candidates:
            if min(packet.angle_min_rad, angle_max) <= candidate <= max(packet.angle_min_rad, angle_max):
                idx = int(round((candidate - packet.angle_min_rad) / packet.angle_increment_rad))
                if 0 <= idx < len(packet.ranges_m):
                    return idx
        return None

    def _publish_topics(self, perception: dict[str, Any]) -> None:
        pubsub = dict(self.config.get("pubsub", {}) or {})
        if not bool(pubsub.get("publish_enabled", True)):
            return
        enabled_topics = set(str(item) for item in pubsub.get("enabled_topics", []))
        ts = float(perception["timestamp"])

        def publish(topic: str, payload: dict[str, Any], frame: str | None = None) -> None:
            if topic not in enabled_topics:
                return
            self.context.topic_bus.publish_payload(
                topic,
                timestamp=ts,
                source=self.id,
                frame=frame,
                seq=int(perception["seq"]),
                payload=payload,
            )

        publish(
            "lidar_pose_plugin.pose",
            {
                key: perception.get(key)
                for key in (
                    "pose_valid",
                    "robot_x_mm",
                    "robot_y_mm",
                    "robot_yaw_rad",
                    "home_x_mm",
                    "home_y_mm",
                    "home_distance_mm",
                    "home_bearing_rad",
                    "heading_error_to_home_rad",
                    "stale",
                )
            },
            frame=str(perception.get("frame_id")),
        )
        publish(
            "lidar_pose_plugin.selected_target_range",
            {
                key: perception.get(key)
                for key in (
                    "selected_target_valid",
                    "selected_target_track_id",
                    "selected_target_color",
                    "selected_target_x",
                    "selected_target_angle_rad",
                    "selected_target_distance_mm",
                    "selected_target_range_quality",
                    "selected_target_world_x_mm",
                    "selected_target_world_y_mm",
                    "selected_target_in_own_safe_zone",
                    "selected_target_ignore",
                    "selected_target_ignore_reason",
                    "vision_target_stale",
                )
            },
            frame=str(perception.get("frame_id")),
        )
        publish(
            "lidar_pose_plugin.boundary",
            {
                "boundary_risk": perception.get("boundary_risk"),
                "boundary_reason": perception.get("boundary_reason"),
                "front_distance_mm": perception.get("front_distance_mm"),
            },
            frame=str(perception.get("frame_id")),
        )
        publish("lidar_pose_plugin.rescue_perception", perception, frame=str(perception.get("frame_id")))
        with self._lock:
            packet = self._last_packet
        if packet is not None:
            publish("lidar_pose_plugin.raw_packet", packet.raw, frame=packet.frame_id)

    def _parse_angle(self, payload: dict[str, Any], base: str) -> float | None:
        rad = self._first_float(payload, (f"{base}_rad", base))
        if rad is not None:
            return rad
        deg = self._first_float(payload, (f"{base}_deg",))
        return math.radians(deg) if deg is not None else None

    def _parse_distance_mm(self, payload: dict[str, Any], base: str) -> float | None:
        mm = self._first_float(payload, (f"{base}_mm", base))
        if mm is not None:
            return mm
        meters = self._first_float(payload, (f"{base}_m",))
        return meters * 1000.0 if meters is not None else None

    def _first_float(self, payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
        for key in keys:
            value = self._parse_float(payload.get(key))
            if value is not None:
                return value
        return None

    def _stamp_to_sec(self, header: Any) -> float:
        stamp = getattr(header, "stamp", None)
        if stamp is None:
            return time.time()
        value = float(getattr(stamp, "sec", 0)) + float(getattr(stamp, "nanosec", 0)) / 1_000_000_000.0
        return value if value > 0 else time.time()

    def _yaw_from_quaternion(self, q: Any) -> float:
        x = float(getattr(q, "x", 0.0) or 0.0)
        y = float(getattr(q, "y", 0.0) or 0.0)
        z = float(getattr(q, "z", 0.0) or 0.0)
        w = float(getattr(q, "w", 1.0) or 1.0)
        siny_cosp = 2.0 * (w * z + x * y)
        cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
        return self._normalize_angle(math.atan2(siny_cosp, cosy_cosp))

    @staticmethod
    def _parse_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if math.isnan(number) or math.isinf(number):
            return None
        return number

    @staticmethod
    def _normalize_angle(angle: float) -> float:
        while angle > math.pi:
            angle -= 2.0 * math.pi
        while angle < -math.pi:
            angle += 2.0 * math.pi
        return angle
