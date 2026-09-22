from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from forwarder import AEBForwarder
from protocol import validate_packet
from topic_adapter import TopicAdapter
from worker_launcher import launch
from worker_protocol import WorkerMessage, parse_line


class LatestPacket:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._packet: dict[str, Any] | None = None
        self.dropped = 0

    def put(self, packet: dict[str, Any]) -> None:
        with self._lock:
            if self._packet is not None:
                self.dropped += 1
            self._packet = packet

    def take(self) -> dict[str, Any] | None:
        with self._lock:
            packet, self._packet = self._packet, None
            return packet


class Plugin:
    id = "astrbotex_embedded_yolo_vision_plugin"
    name = "AstrBotEX Embedded YOLO Vision"

    def __init__(self, context) -> None:
        self.context = context
        self.config = dict(context.config or {})
        self.root = Path(__file__).resolve().parent
        self._worker = None
        self._reader: threading.Thread | None = None
        self._stop_reader = threading.Event()
        self._latest = LatestPacket()
        self._last_packet: dict[str, Any] | None = None
        self._forwarder = AEBForwarder(self.config, self._emit)
        self._topics = TopicAdapter(context, self.config, self._emit)
        self._status: dict[str, Any] = {"worker_state": "stopped", "restart_count": 0, "last_error": None, "packets_forwarded": 0, "frames_dropped": 0, "last_frame_id": None}
        self._next_restart_at = 0.0

    def on_runtime_start(self) -> None:
        if self._worker is not None and self._worker.poll() is None:
            return
        self._start_worker()

    def on_runtime_stop(self, reason: str) -> None:
        self._stop_worker(reason)

    def on_disable(self) -> None:
        self._stop_worker("plugin disabled")

    def on_unload(self) -> None:
        self._stop_worker("plugin unloaded")
        self._forwarder.close()

    def on_worker_step(self) -> None:
        packet = self._latest.take()
        self._status["frames_dropped"] = self._latest.dropped
        if packet is not None:
            try:
                self._forwarder.publish(packet)
                self._topics.publish(packet)
                self._status["packets_forwarded"] += 1
                self._status["last_frame_id"] = packet.get("frame_id")
            except Exception as exc:
                self._status["last_error"] = str(exc)
                self._emit("packet forwarding failed", severity="warning", error=str(exc))
        if self._worker is not None and self._worker.poll() is not None:
            code = self._worker.returncode
            self._status["worker_exit_code"] = code
            if code == 20 and bool(self.config.get("worker_restart", True)) and time.monotonic() >= self._next_restart_at:
                self._status["restart_count"] += 1
                self._next_restart_at = time.monotonic() + max(0.1, float(self.config.get("worker_restart_sec", 2.0)))
                self._start_worker()
            elif code not in (0, None):
                self._status["worker_state"] = "failed"

    def get_status(self) -> dict[str, Any]:
        status = dict(self._status)
        status["worker_running"] = self._worker is not None and self._worker.poll() is None
        status["worker_state"] = "running" if status["worker_running"] else status.get("worker_state", "stopped")
        return status

    def get_result(self):
        """Expose the latest packet through AstrBotEX's vision_provider contract."""
        try:
            from astrbot_ex.core.models import Entity, VisionResult
        except ImportError:
            return {"frame_id": 0, "timestamp": time.time(), "entities": [], "metadata": {"source": self.id, "stale": True}}
        packet = self._latest_snapshot()
        if packet is None:
            return VisionResult(frame_id=0, timestamp=time.time(), metadata={"source": self.id, "stale": True})
        entities = []
        for index, obj in enumerate(packet.get("objects", [])):
            bbox = obj.get("bbox_xyxy")
            bbox_value = tuple(int(value) for value in bbox) if isinstance(bbox, list) and len(bbox) == 4 else None
            metadata = {"target_x": obj.get("target_x"), "target_valid": obj.get("target_valid", True)}
            entities.append(Entity(id=str(obj.get("track_id", f"obj-{index}")), type=str(obj.get("class_name", obj.get("class", "target"))), confidence=float(obj.get("confidence", obj.get("score", 0.0)) or 0.0), bbox_px=bbox_value, metadata=metadata))
        return VisionResult(frame_id=int(packet.get("frame_id", 0)), timestamp=float(packet.get("timestamp", time.time())), entities=entities, metadata={"source": packet.get("source", self.id), **dict(packet.get("metadata", {}))})

    def _latest_snapshot(self) -> dict[str, Any] | None:
        with self._latest._lock:
            if self._latest._packet is not None:
                return dict(self._latest._packet)
            return dict(self._last_packet) if self._last_packet is not None else None
    def _start_worker(self) -> None:
        self._stop_worker("restart")
        self._stop_reader.clear()
        try:
            self._worker = launch(self.root, self.config)
        except Exception as exc:
            self._status.update(worker_state="failed", last_error=str(exc))
            self._emit("worker launch failed", severity="error", error=str(exc))
            return
        self._status.update(worker_state="starting", last_error=None)
        self._reader = threading.Thread(target=self._read_worker, name=f"{self.id}-reader", daemon=True)
        self._reader.start()

    def _read_worker(self) -> None:
        worker = self._worker
        if worker is None or worker.stdout is None:
            return
        for line in worker.stdout:
            if self._stop_reader.is_set():
                break
            try:
                message = parse_line(line)
                self._handle_worker_message(message)
            except Exception as exc:
                self._status["last_error"] = str(exc)
                self._emit("worker output rejected", severity="warning", error=str(exc))

    def _handle_worker_message(self, message: WorkerMessage) -> None:
        if message.kind == "packet":
            packet = validate_packet(message.payload)
            self._latest.put(packet)
        elif message.kind == "worker_ready":
            self._status.update(worker_state="ready", model_version=message.payload.get("model_version"), dependencies=message.payload.get("dependencies", {}))
        elif message.kind == "worker_error":
            self._status.update(worker_state="failed", last_error=message.payload.get("error"), worker_error_code=message.payload.get("exit_code"))
        elif message.kind == "worker_stats":
            self._status.update(worker_stats=message.payload)

    def _stop_worker(self, reason: str) -> None:
        self._stop_reader.set()
        worker = self._worker
        self._worker = None
        if worker is not None:
            try:
                worker.terminate()
                worker.wait(timeout=max(0.2, float(self.config.get("worker_stop_timeout_sec", 3.0))))
            except Exception:
                try:
                    worker.kill()
                except Exception:
                    pass
        if self._reader is not None and self._reader.is_alive():
            self._reader.join(timeout=0.5)
        self._reader = None
        self._status["worker_state"] = "stopped"
        self._emit("worker stopped", reason=reason)

    def _emit(self, message: str, severity: str = "info", **details: Any) -> None:
        event_bus = getattr(self.context, "event_bus", None)
        if event_bus is not None:
            event_bus.emit("plugin", message, severity=severity, plugin=self.id, **details)


def create_plugin(context) -> Plugin:
    return Plugin(context)
