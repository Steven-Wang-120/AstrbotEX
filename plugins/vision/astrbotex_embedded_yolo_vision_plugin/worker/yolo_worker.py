from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from camera_sources import FrameSource
from packet_builder import build_packet, normalized_detection


EXIT_CONFIG = 10
EXIT_MODEL = 11
EXIT_CAMERA = 12
EXIT_DEPENDENCY = 13
EXIT_RUNTIME = 20


def emit(kind: str, payload: dict[str, Any] | None = None, **fields: Any) -> None:
    value: dict[str, Any] = {"kind": kind}
    if payload is not None:
        value["payload"] = payload
    value.update(fields)
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)


def run(config: dict[str, Any]) -> int:
    weights = Path(str(config.get("weights", ""))).expanduser()
    if not str(weights):
        emit("worker_error", error="weights is required", exit_code=EXIT_CONFIG)
        return EXIT_CONFIG
    if not weights.is_file():
        emit("worker_error", error=f"model file not found: {weights}", exit_code=EXIT_MODEL)
        return EXIT_MODEL
    try:
        import cv2
        from ultralytics import YOLO
    except ImportError as exc:
        emit("worker_error", error=f"YOLO dependency missing: {exc}", exit_code=EXIT_DEPENDENCY)
        return EXIT_DEPENDENCY
    source_mode = str(config.get("source_mode", "mjpeg_http"))
    source = str(config.get("source", ""))
    if not source:
        emit("worker_error", error="source is required", exit_code=EXIT_CONFIG)
        return EXIT_CONFIG
    frame_interval = max(1, int(config.get("frame_interval", 5)))
    max_frame_bytes = max(1024, int(config.get("max_frame_bytes", 8 * 1024 * 1024)))
    model_version = f"{weights.name}:{weights.stat().st_size}"
    try:
        model = YOLO(str(weights))
        source_reader = FrameSource(source_mode, source, timeout_sec=float(config.get("source_timeout_sec", 5.0)), max_frame_bytes=max_frame_bytes, reconnect_sec=float(config.get("source_reconnect_sec", 2.0)))
        warmup = source_reader.read()
        model.predict(warmup, device=config.get("device", "cpu"), conf=float(config.get("confidence", 0.25)), imgsz=int(config.get("imgsz", 640)), verbose=False)
        emit("worker_ready", weights=str(weights), weight_bytes=weights.stat().st_size, model_version=model_version, dependencies={"opencv": cv2.__version__, "python": sys.version.split()[0]})
    except RuntimeError as exc:
        source_reader.close() if "source_reader" in locals() else None
        emit("worker_error", error=str(exc), exit_code=EXIT_CAMERA)
        return EXIT_CAMERA
    except Exception as exc:
        source_reader.close() if "source_reader" in locals() else None
        emit("worker_error", error=str(exc), exit_code=EXIT_RUNTIME)
        return EXIT_RUNTIME

    frame_id = 0
    try:
        while True:
            frame_started = time.monotonic()
            frame = source_reader.read()
            frame_id += 1
            height, width = frame.shape[:2]
            result = model.predict(frame, device=config.get("device", "cpu"), conf=float(config.get("confidence", 0.25)), imgsz=int(config.get("imgsz", 640)), verbose=False)[0]
            objects: list[dict[str, Any]] = []
            boxes = getattr(result, "boxes", None)
            names = getattr(result, "names", {}) or {}
            if boxes is not None:
                xyxy = boxes.xyxy.cpu().tolist() if hasattr(boxes.xyxy, "cpu") else boxes.xyxy.tolist()
                confs = boxes.conf.cpu().tolist() if hasattr(boxes.conf, "cpu") else boxes.conf.tolist()
                classes = boxes.cls.cpu().tolist() if hasattr(boxes.cls, "cpu") else boxes.cls.tolist()
                for idx, bbox in enumerate(xyxy):
                    class_id = int(classes[idx])
                    class_name = str(names.get(class_id, class_id))
                    objects.append(normalized_detection(frame_id=frame_id, class_name=class_name, confidence=float(confs[idx]), bbox_xyxy=[int(v) for v in bbox], image_width=width))
            jpeg_b64 = None
            if frame_id % frame_interval == 0:
                encoded, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(config.get("jpeg_quality", 85))])
                if encoded and len(buffer) <= max_frame_bytes:
                    import base64
                    jpeg_b64 = base64.b64encode(buffer.tobytes()).decode("ascii")
            packet = build_packet(frame_id=frame_id, source=str(config.get("stream_id", "front-camera-yolo")), image_width=width, image_height=height, objects=objects, model=weights.name, model_version=model_version, jpeg_b64=jpeg_b64)
            emit("packet", packet=packet, stats={"fps": round(1.0 / max(time.monotonic() - frame_started, 1e-6), 2), "detection_count": len(objects)})
    except KeyboardInterrupt:
        emit("worker_stopped", reason="signal")
        return 0
    except Exception as exc:
        emit("worker_error", error=str(exc), exit_code=EXIT_RUNTIME, frame_id=frame_id)
        return EXIT_RUNTIME
    finally:
        source_reader.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("worker config must be an object")
    except Exception as exc:
        emit("worker_error", error=f"invalid config: {exc}", exit_code=EXIT_CONFIG)
        return EXIT_CONFIG
    return run(config)


if __name__ == "__main__":
    raise SystemExit(main())
