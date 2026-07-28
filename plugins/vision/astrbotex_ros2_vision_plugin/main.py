from __future__ import annotations

import json
import math
import os
import socket
import time
from dataclasses import dataclass
from typing import Any

try:
    import rclpy
    from rclpy.node import Node
except ImportError:  # pragma: no cover
    rclpy = None
    Node = object

try:
    from std_msgs.msg import String as RosString
except ImportError:  # pragma: no cover
    RosString = None

try:
    from vision_msgs.msg import Detection2DArray
except ImportError:  # pragma: no cover
    Detection2DArray = None

from astrbot_ex.core.models import Entity, VisionResult, Zone


@dataclass(slots=True)
class ParsedPacket:
    stamp: float
    frame_name: str
    source: str
    raw: dict[str, Any]
    objects: list[dict[str, Any]]
    zones: list[dict[str, Any]]
    metadata: dict[str, Any]


class Plugin:
    id = "astrbotex_ros2_vision_plugin"
    name = "AstrBotEX ROS2 Vision Plugin"

    def __init__(self, context) -> None:
        self.context = context
        self.config = dict(context.config or {})
        self._node: Node | None = None
        self._subscription: Any = None
        self._input_mode = ""
        self._ipc_server: socket.socket | None = None
        self._ipc_connection: socket.socket | None = None
        self._ipc_buffer = ""
        self._ipc_path = ""
        self._frame_seq = 0
        self._last_result = VisionResult(frame_id=0, timestamp=time.time(), metadata={"source": "empty"})
        self._last_raw: dict[str, Any] | None = None

    def on_runtime_start(self) -> None:
        self._open_input()

    def on_runtime_stop(self, reason: str) -> None:
        self._close_input(reason)

    def on_disable(self) -> None:
        self._close_input("plugin disabled")

    def on_unload(self) -> None:
        self._close_input("plugin unloaded")

    def get_result(self) -> VisionResult:
        stale_after = max(1, int(self.config.get("stale_after_ms", 500))) / 1000.0
        now = time.time()
        age = now - self._last_result.timestamp
        if age > stale_after:
            return VisionResult(
                frame_id=self._last_result.frame_id,
                timestamp=now,
                metadata={
                    "source": self.config.get("source_name", "yolo_ros2"),
                    "stale": True,
                    "age_sec": round(age, 3),
                },
            )
        return self._last_result

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
        input_mode = str(self.config.get("input_mode", "ipc_unix_socket")).strip().lower()
        if input_mode in {"ros2", "ros"}:
            if rclpy is None:
                raise RuntimeError("rclpy is not installed; install ROS2 Python packages before enabling ROS2 mode")
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
        self._subscription = None
        if node is not None:
            try:
                node.destroy_node()
            except Exception:
                pass
        if self._input_mode:
            self._emit("vision link closed", reason=reason, input_mode=self._input_mode)
        self._input_mode = ""

    def _open_ros2(self) -> None:
        node_name = str(self.config.get("ros_node_name", self.id)).strip() or self.id
        topic = str(self.config.get("topic", "/astrbotex/vision_target")).strip()
        message_type = str(self.config.get("message_type", "std_msgs/msg/String")).strip()
        if not rclpy.ok():
            rclpy.init(args=None)
        self._node = rclpy.create_node(node_name)
        msg_cls = self._message_class(message_type)
        self._subscription = self._node.create_subscription(msg_cls, topic, self._on_ros_message, 10)
        self._emit("ros2 vision subscription started", topic=topic, message_type=self._message_type_name(msg_cls))

    def _open_ipc(self) -> None:
        path = str(self.config.get("ipc_socket_path", "/app/data/ipc/astrbotex_vision.sock")).strip()
        backlog = int(self.config.get("ipc_socket_backlog", 1))
        if not path:
            raise ValueError("ipc_socket_path is required in ipc_unix_socket mode")
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
        self._emit("ipc vision socket started", path=path)

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
                raise ValueError("IPC payload must be a JSON object")
            packet = self._parse_json_packet(payload)
            result = self._to_vision_result(packet)
            self._last_result = result
            self._last_raw = packet.raw
            self._publish_topics(result, packet)
            self._emit("ipc vision packet received", frame_id=result.frame_id, entities=len(result.entities))
        except Exception as exc:
            self._emit("ipc vision packet parse failed", severity="error", error=str(exc))

    def _message_class(self, message_type: str) -> Any:
        value = message_type.lower()
        if value in {"auto", "std_msgs/string", "std_msgs/msg/string"}:
            if RosString is None:
                raise RuntimeError("std_msgs.msg.String is not available")
            return RosString
        if value in {"vision_msgs/detection2darray", "vision_msgs/msg/detection2darray"}:
            if Detection2DArray is None:
                raise RuntimeError("vision_msgs.msg.Detection2DArray is not available")
            return Detection2DArray
        raise ValueError(f"unsupported ROS2 message_type: {message_type}")

    def _message_type_name(self, msg_cls: Any) -> str:
        return f"{getattr(msg_cls, '__module__', '')}.{getattr(msg_cls, '__name__', msg_cls)}"

    def _on_ros_message(self, msg: Any) -> None:
        try:
            packet = self._parse_message(msg)
            result = self._to_vision_result(packet)
            self._last_result = result
            self._last_raw = packet.raw
            self._publish_topics(result, packet)
            self._emit("ros2 vision packet received", frame_id=result.frame_id, entities=len(result.entities))
        except Exception as exc:
            self._emit("ros2 vision packet parse failed", severity="error", error=str(exc))

    def _parse_message(self, msg: Any) -> ParsedPacket:
        if hasattr(msg, "data") and isinstance(msg.data, str):
            payload = json.loads(msg.data)
            if not isinstance(payload, dict):
                raise ValueError("std_msgs/String payload must be a JSON object")
            return self._parse_json_packet(payload)
        if Detection2DArray is not None and isinstance(msg, Detection2DArray):
            return self._parse_detection2d_array(msg)
        raise ValueError(f"unsupported ROS2 message object: {type(msg).__name__}")

    def _parse_json_packet(self, payload: dict[str, Any]) -> ParsedPacket:
        source_name = str(self.config.get("source_name", "yolo_ros2"))
        objects = self._objects_from_json(payload)
        zones = list(payload.get("zones", [])) if isinstance(payload.get("zones", []), list) else []
        metadata = dict(payload.get("metadata", {})) if isinstance(payload.get("metadata", {}), dict) else {}
        metadata.setdefault("image_width", int(payload.get("image_width", self.config.get("image_width", 640))))
        metadata.setdefault("image_height", int(payload.get("image_height", self.config.get("image_height", 480))))
        return ParsedPacket(
            stamp=float(payload.get("timestamp", time.time())),
            frame_name=str(payload.get("frame_id", self.config.get("default_frame_id", "camera_front"))),
            source=str(payload.get("source", source_name)),
            raw=payload,
            objects=objects,
            zones=zones,
            metadata=metadata,
        )

    def _objects_from_json(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if isinstance(payload.get("objects"), list):
            return [item for item in payload["objects"] if isinstance(item, dict)]
        if isinstance(payload.get("detections"), list):
            return [item for item in payload["detections"] if isinstance(item, dict)]

        color = payload.get("color", payload.get("target_color"))
        target_x = payload.get("target_x", payload.get("x"))
        distance = payload.get("target_distance", payload.get("distance"))
        x_is_empty = isinstance(target_x, str) and target_x.strip().lower() == "empty"
        target_valid = bool(payload.get("target_valid", (color is not None or target_x is not None) and not x_is_empty))
        if not target_valid and color is None:
            color = "empty"

        obj = {
            "track_id": payload.get("track_id", "target-0"),
            "class": payload.get("class", payload.get("class_name", "target")),
            "semantic": payload.get("semantic"),
            "color": color,
            "target_x": target_x,
            "score": payload.get("score", payload.get("confidence", 1.0)),
            "distance": distance,
            "target_valid": target_valid,
            "bbox_xyxy": payload.get("bbox_xyxy", payload.get("bbox")),
            "center_px": payload.get("center_px"),
            "position_xy": payload.get("position_xy"),
            "attributes": {
                key: value
                for key, value in payload.items()
                if key not in {"objects", "detections", "zones", "metadata"}
            },
        }
        return [obj]

    def _parse_detection2d_array(self, msg: Any) -> ParsedPacket:
        stamp = time.time()
        frame_name = str(self.config.get("default_frame_id", "camera_front"))
        header = getattr(msg, "header", None)
        if header is not None:
            frame_name = str(getattr(header, "frame_id", frame_name) or frame_name)
            stamp_msg = getattr(header, "stamp", None)
            if stamp_msg is not None:
                stamp = float(getattr(stamp_msg, "sec", 0)) + float(getattr(stamp_msg, "nanosec", 0)) / 1_000_000_000.0

        objects: list[dict[str, Any]] = []
        image_width = int(self.config.get("image_width", 640))
        detections = list(getattr(msg, "detections", []) or [])
        for idx, detection in enumerate(detections):
            bbox = getattr(detection, "bbox", None)
            center = getattr(bbox, "center", None)
            center_x = float(getattr(getattr(center, "position", center), "x", 0.0) or 0.0) if center is not None else 0.0
            center_y = float(getattr(getattr(center, "position", center), "y", 0.0) or 0.0) if center is not None else 0.0
            size_x = float(getattr(bbox, "size_x", 0.0) or 0.0) if bbox is not None else 0.0
            size_y = float(getattr(bbox, "size_y", 0.0) or 0.0) if bbox is not None else 0.0
            best = self._best_detection_result(getattr(detection, "results", []) or [])
            target_x = (center_x - image_width / 2.0) / max(image_width / 2.0, 1.0)
            objects.append(
                {
                    "track_id": f"det-{idx}",
                    "class": best.get("class_name", "target"),
                    "semantic": best.get("class_name"),
                    "color": best.get("color"),
                    "target_x": target_x,
                    "score": best.get("score", 1.0),
                    "bbox_xyxy": [
                        int(center_x - size_x / 2.0),
                        int(center_y - size_y / 2.0),
                        int(center_x + size_x / 2.0),
                        int(center_y + size_y / 2.0),
                    ],
                    "center_px": [center_x, center_y],
                    "attributes": {"raw_result": best},
                }
            )
        return ParsedPacket(
            stamp=stamp,
            frame_name=frame_name,
            source=str(self.config.get("source_name", "yolo_ros2")),
            raw={"message_type": "vision_msgs/msg/Detection2DArray", "detections": len(objects)},
            objects=objects,
            zones=[],
            metadata={
                "message_type": "vision_msgs/msg/Detection2DArray",
                "image_width": int(self.config.get("image_width", 640)),
                "image_height": int(self.config.get("image_height", 480)),
            },
        )

    def _best_detection_result(self, results: list[Any]) -> dict[str, Any]:
        best: dict[str, Any] = {"score": 0.0}
        for result in results:
            hypothesis = getattr(result, "hypothesis", result)
            score = float(getattr(hypothesis, "score", getattr(result, "score", 0.0)) or 0.0)
            if score < float(best.get("score", 0.0)):
                continue
            class_id = str(getattr(hypothesis, "class_id", getattr(result, "id", "target")) or "target")
            best = {
                "score": score,
                "class_name": class_id,
                "color": self._color_from_text(class_id),
            }
        return best

    def _to_vision_result(self, packet: ParsedPacket) -> VisionResult:
        min_conf = float(self.config.get("min_confidence", 0.4))
        filtered_objects = [item for item in packet.objects if self._should_forward_object(item)]
        source_objects = filtered_objects if bool(self.config.get("publish_filtered_only", True)) else packet.objects

        entities: list[Entity] = []
        for idx, item in enumerate(source_objects):
            raw_x = item.get("target_x", item.get("x"))
            x_is_empty = isinstance(raw_x, str) and raw_x.strip().lower() == "empty"
            if bool(item.get("target_valid", True)) is False and x_is_empty:
                continue
            score = float(item.get("score", item.get("confidence", 1.0)) or 0.0)
            if score < min_conf:
                continue
            bbox = self._parse_bbox(item.get("bbox_xyxy", item.get("bbox")))
            position = self._parse_position(item)
            metadata = dict(item.get("attributes", {})) if isinstance(item.get("attributes", {}), dict) else {}
            color = self._normalize_color(item.get("color", metadata.get("color")))
            target_x = self._parse_float(item.get("target_x", item.get("x")))
            distance = self._parse_float(item.get("target_distance", item.get("distance")))
            if color:
                metadata["color"] = color
            if target_x is not None:
                metadata["target_x"] = target_x
            if distance is not None:
                metadata["distance"] = distance
            if item.get("center_px") is not None:
                metadata["center_px"] = item["center_px"]
            if item.get("target_valid") is not None:
                metadata["target_valid"] = bool(item["target_valid"])

            entities.append(
                Entity(
                    id=str(item.get("track_id", f"obj-{idx}")),
                    type=str(item.get("class", item.get("class_name", "target"))),
                    confidence=score,
                    semantic=str(item["semantic"]) if item.get("semantic") else None,
                    position=position,
                    bbox_px=bbox,
                    metadata=metadata,
                )
            )

        zones = [self._zone_from_item(idx, item) for idx, item in enumerate(packet.zones)]
        self._frame_seq += 1
        return VisionResult(
            frame_id=self._frame_seq,
            timestamp=packet.stamp,
            entities=entities,
            zones=zones,
            metadata={
                "source": packet.source,
                "frame_name": packet.frame_name,
                "filtered_count": len(filtered_objects),
                "raw_count": len(packet.objects),
                **packet.metadata,
            },
        )

    def _should_forward_object(self, item: dict[str, Any]) -> bool:
        color = self._normalize_color(item.get("color"))
        if color is None:
            color = self._color_from_text(str(item.get("class", "")) + " " + str(item.get("semantic", "")))
        if color is None and bool(item.get("target_valid", True)) is False:
            color = "empty"
        focus = self._normalize_color(self.config.get("focus_color", "red"))
        always = {self._normalize_color(value) for value in self.config.get("always_forward_colors", ["yellow", "black", "empty"])}
        always.discard(None)
        return color == focus or color in always

    def _publish_topics(self, result: VisionResult, packet: ParsedPacket) -> None:
        pubsub = self._read_pubsub()
        if not pubsub["publish_enabled"]:
            return
        enabled_topics = set(pubsub["enabled_topics"])

        if "astrbotex_ros2_vision_plugin.raw_packet" in enabled_topics:
            self.context.topic_bus.publish_payload(
                "astrbotex_ros2_vision_plugin.raw_packet",
                timestamp=result.timestamp,
                source=self.id,
                frame=packet.frame_name,
                payload=packet.raw,
            )

        if "astrbotex_ros2_vision_plugin.detections" in enabled_topics:
            self.context.topic_bus.publish_payload(
                "astrbotex_ros2_vision_plugin.detections",
                timestamp=result.timestamp,
                source=self.id,
                frame=packet.frame_name,
                payload={
                    "frame_id": result.frame_id,
                    "frame_name": packet.frame_name,
                    "source": packet.source,
                    "entities": [self._entity_payload(entity) for entity in result.entities],
                    "zones": [self._zone_payload(zone) for zone in result.zones],
                    "metadata": result.metadata,
                },
            )

        current_target = self._pick_current_target(result) or self._empty_current_target(packet)
        if current_target and "astrbotex_ros2_vision_plugin.current_target" in enabled_topics:
            target_x = self._parse_float(current_target.get("target_x", current_target.get("x")))
            deadband = float(self.config.get("target_x_deadband", 0.1))
            self.context.topic_bus.publish_payload(
                "astrbotex_ros2_vision_plugin.current_target",
                timestamp=result.timestamp,
                source=self.id,
                frame=packet.frame_name,
                payload={
                    **current_target,
                    "is_aligned": target_x is not None and abs(target_x) <= deadband,
                },
            )

    def _empty_current_target(self, packet: ParsedPacket) -> dict[str, Any] | None:
        raw_x = packet.raw.get("x", packet.raw.get("target_x"))
        raw_color = packet.raw.get("color", "empty")
        is_empty = isinstance(raw_x, str) and raw_x.strip().lower() == "empty"
        target_valid = bool(packet.raw.get("target_valid", not is_empty))
        if not is_empty and target_valid:
            return None
        return {
            "track_id": packet.raw.get("track_id"),
            "class_name": packet.raw.get("class", packet.raw.get("class_name", "ball")),
            "semantic": packet.raw.get("semantic"),
            "confidence": packet.raw.get("confidence", packet.raw.get("score")),
            "color": raw_color,
            "x": "empty",
            "target_x": "empty",
            "distance": packet.raw.get("target_distance", packet.raw.get("distance")),
            "position": None,
            "bbox_xyxy": None,
            "target_valid": False,
            "metadata": {
                "source": packet.source,
                "raw": packet.raw,
            },
        }

    def _pick_current_target(self, result: VisionResult) -> dict[str, Any] | None:
        if not result.entities:
            return None
        ranked: list[tuple[float, Entity]] = []
        for entity in result.entities:
            target_x = self._parse_float(entity.metadata.get("target_x"))
            center_penalty = abs(target_x) if target_x is not None else 0.0
            distance = self._parse_float(entity.metadata.get("distance"))
            distance_score = 0.0 if distance is None or distance <= 0 else min(1.0, 1000.0 / distance)
            score = entity.confidence * 0.55 + (1.0 - min(1.0, center_penalty)) * 0.30 + distance_score * 0.15
            ranked.append((score, entity))
        ranked.sort(key=lambda item: item[0], reverse=True)
        entity = ranked[0][1]
        return {
            "track_id": entity.id,
            "class_name": entity.type,
            "semantic": entity.semantic,
            "confidence": entity.confidence,
            "color": entity.metadata.get("color"),
            "target_x": entity.metadata.get("target_x"),
            "distance": entity.metadata.get("distance"),
            "position": list(entity.position) if entity.position is not None else None,
            "bbox_xyxy": list(entity.bbox_px) if entity.bbox_px is not None else None,
            "metadata": entity.metadata,
        }

    def _read_pubsub(self) -> dict[str, Any]:
        pubsub = dict(self.config.get("pubsub", {}) or {})
        enabled_topics = [str(item) for item in pubsub.get("enabled_topics", [])]
        return {
            "publish_enabled": bool(pubsub.get("publish_enabled", True)),
            "enabled_topics": enabled_topics,
        }

    def _entity_payload(self, entity: Entity) -> dict[str, Any]:
        return {
            "id": entity.id,
            "type": entity.type,
            "semantic": entity.semantic,
            "confidence": entity.confidence,
            "position": list(entity.position) if entity.position is not None else None,
            "bbox_xyxy": list(entity.bbox_px) if entity.bbox_px is not None else None,
            "metadata": entity.metadata,
        }

    def _zone_payload(self, zone: Zone) -> dict[str, Any]:
        return {
            "id": zone.id,
            "role": zone.role,
            "polygon": [list(point) for point in zone.polygon],
            "metadata": zone.metadata,
        }

    def _zone_from_item(self, idx: int, item: dict[str, Any]) -> Zone:
        polygon = []
        raw = item.get("polygon")
        if isinstance(raw, list):
            for point in raw:
                if isinstance(point, list) and len(point) >= 2:
                    polygon.append((float(point[0]), float(point[1])))
        return Zone(
            id=str(item.get("id", f"zone-{idx}")),
            role=str(item.get("role", "zone")),
            polygon=polygon,
            metadata=dict(item.get("attributes", {})) if isinstance(item.get("attributes", {}), dict) else {},
        )

    def _parse_bbox(self, raw: Any) -> tuple[int, int, int, int] | None:
        if not isinstance(raw, list) or len(raw) < 4:
            return None
        return (int(raw[0]), int(raw[1]), int(raw[2]), int(raw[3]))

    def _parse_position(self, item: dict[str, Any]) -> tuple[float, float] | None:
        raw_xy = item.get("position_xy")
        if isinstance(raw_xy, list) and len(raw_xy) >= 2:
            return (float(raw_xy[0]), float(raw_xy[1]))
        raw_xyz = item.get("position_xyz")
        if isinstance(raw_xyz, list) and len(raw_xyz) >= 2:
            return (float(raw_xyz[0]), float(raw_xyz[1]))
        ball_x = item.get("ball_x")
        ball_y = item.get("ball_y")
        if ball_x is not None and ball_y is not None:
            return (float(ball_x), float(ball_y))
        return None

    def _parse_float(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if math.isnan(number) or math.isinf(number):
            return None
        return number

    def _normalize_color(self, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip().lower()
        aliases = {
            "r": "red",
            "red": "red",
            "红": "red",
            "红色": "red",
            "b": "blue",
            "blue": "blue",
            "蓝": "blue",
            "蓝色": "blue",
            "y": "yellow",
            "yellow": "yellow",
            "黄": "yellow",
            "黄色": "yellow",
            "black": "black",
            "k": "black",
            "黑": "black",
            "黑色": "black",
            "empty": "empty",
            "none": "empty",
            "null": "empty",
            "": "empty",
        }
        return aliases.get(text, text)

    def _color_from_text(self, text: str) -> str | None:
        lower = text.lower()
        for color in ("red", "blue", "yellow", "black", "empty"):
            if color in lower:
                return color
        for token, color in (("红", "red"), ("蓝", "blue"), ("黄", "yellow"), ("黑", "black")):
            if token in text:
                return color
        return None

    def _emit(self, message: str, **data: Any) -> None:
        self.context.event_bus.emit("vision_plugin", message, plugin=self.id, **data)
