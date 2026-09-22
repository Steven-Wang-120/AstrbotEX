from __future__ import annotations

import base64
import binascii
import json
from typing import Any


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    return float(value)


def validate_packet(packet: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(packet, dict):
        raise ValueError("packet must be an object")
    if isinstance(packet.get("frame_id"), bool) or not isinstance(packet.get("frame_id"), int):
        raise ValueError("frame_id must be an integer")
    _number(packet.get("timestamp"), "timestamp")
    for key in ("image_width", "image_height"):
        if key in packet and (isinstance(packet[key], bool) or not isinstance(packet[key], int) or packet[key] < 0):
            raise ValueError(f"{key} must be a non-negative integer")
    objects = packet.get("objects", packet.get("detections"))
    if not isinstance(objects, list):
        raise ValueError("objects or detections must be an array")
    for index, obj in enumerate(objects):
        if not isinstance(obj, dict):
            raise ValueError(f"object {index} must be an object")
        bbox = obj.get("bbox_xyxy")
        if not isinstance(bbox, list) or len(bbox) != 4 or any(isinstance(v, bool) or not isinstance(v, int) for v in bbox):
            raise ValueError(f"object {index} bbox_xyxy must contain four integers")
        if not isinstance(obj.get("class_name", obj.get("class")), str):
            raise ValueError(f"object {index} class_name must be a string")
        _number(obj.get("confidence", obj.get("score", 0.0)), f"object {index} confidence")
    return packet


def decode_worker_line(line: str) -> tuple[str, dict[str, Any]]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid worker JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("worker output must be a JSON object")
    kind = str(value.get("kind", "packet"))
    if kind == "packet":
        return kind, validate_packet(value.get("packet", value))
    if kind in {"worker_ready", "worker_error", "worker_stats", "worker_stopped"}:
        payload = value.get("payload", value)
        if not isinstance(payload, dict):
            raise ValueError("worker event payload must be an object")
        return kind, payload
    raise ValueError(f"unsupported worker event kind: {kind}")


def encode_jpeg_base64(data: bytes, max_bytes: int) -> str | None:
    if len(data) > max_bytes:
        return None
    return base64.b64encode(data).decode("ascii")


def decode_jpeg_base64(value: str) -> bytes:
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid base64 JPEG") from exc
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise ValueError("JPEG payload markers are invalid")
    return data
