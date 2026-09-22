from __future__ import annotations

import time
from typing import Any


def normalized_detection(
    *, frame_id: int, class_name: str, confidence: float, bbox_xyxy: list[int], image_width: int
) -> dict[str, Any]:
    if len(bbox_xyxy) != 4 or any(not isinstance(v, int) for v in bbox_xyxy):
        raise ValueError("bbox_xyxy must contain four integers")
    center_x = (bbox_xyxy[0] + bbox_xyxy[2]) / 2.0
    target_x = (center_x - image_width / 2.0) / max(image_width / 2.0, 1.0)
    return {
        "track_id": f"{frame_id}-{0}",
        "class_name": class_name,
        "class": class_name,
        "confidence": float(confidence),
        "score": float(confidence),
        "bbox_xyxy": bbox_xyxy,
        "target_x": target_x,
        "target_valid": True,
    }


def build_packet(
    *, frame_id: int, source: str, image_width: int, image_height: int,
    objects: list[dict[str, Any]], model: str, model_version: str, timestamp: float | None = None,
    jpeg_b64: str | None = None,
) -> dict[str, Any]:
    packet: dict[str, Any] = {
        "frame_id": frame_id,
        "timestamp": time.time() if timestamp is None else float(timestamp),
        "source": source,
        "image_width": int(image_width),
        "image_height": int(image_height),
        "metadata": {"model": model, "model_version": model_version, "detection_count": len(objects)},
        "objects": objects,
    }
    if jpeg_b64 is not None:
        packet["jpeg_base64"] = jpeg_b64
    return packet
