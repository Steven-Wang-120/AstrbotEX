from __future__ import annotations

import json
import time
import uuid
from typing import Any

PROTOCOL = "astrbotex-zmq"
PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


class AEBForwarder:
    def __init__(self, config: dict[str, Any], emit) -> None:
        self.config = config
        self.emit = emit
        self._context: Any | None = None
        self._sockets: dict[str, Any] = {}
        self._pollers: dict[str, Any] = {}

    def _ensure(self) -> None:
        if self._sockets:
            return
        try:
            import zmq
        except ImportError as exc:
            raise RuntimeError("pyzmq is required for A.E.B forwarding") from exc
        self._context = zmq.Context.instance()
        for route, endpoint, channel in (("json", self.config.get("json_zmq_endpoint", "tcp://astrbot:8766"), "text"), ("jpeg", self.config.get("jpeg_zmq_endpoint", "tcp://astrbot:8768"), "vision")):
            socket = self._context.socket(zmq.DEALER)
            socket.setsockopt(zmq.LINGER, 0)
            socket.setsockopt(zmq.IDENTITY, f"astrbotex-embedded-yolo-{route}".encode())
            socket.connect(str(endpoint))
            poller = zmq.Poller()
            poller.register(socket, zmq.POLLIN)
            self._sockets[route] = socket
            self._pollers[route] = poller
            self._request(route, channel, "system.hello", {"client": "AstrBotEX", "instance_id": "astrbotex_embedded_yolo_vision_plugin"})

    def _request(self, route: str, channel: str, method: str, payload: dict[str, Any], binary: bytes | None = None) -> dict[str, Any]:
        socket = self._sockets[route]
        request_id = uuid.uuid4().hex
        envelope = {"protocol": PROTOCOL, "version": PROTOCOL_VERSION, "channel": channel, "kind": "request", "id": request_id, "method": method, "timestamp": time.time(), "payload": payload}
        frames = [json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")]
        if binary is not None:
            frames.append(binary)
        socket.send_multipart(frames)
        timeout_ms = max(1, int(float(self.config.get("zmq_timeout_sec", 2.0)) * 1000))
        ready = dict(self._pollers[route].poll(timeout_ms))
        if socket not in ready:
            raise TimeoutError(f"{channel}/{method} timed out")
        response = json.loads(socket.recv_multipart()[0].decode("utf-8"))
        if response.get("kind") != "response" or response.get("reply_to") != request_id:
            raise RuntimeError("invalid A.E.B response correlation")
        result = response.get("payload", {})
        if not isinstance(result, dict) or result.get("ok", True) is False:
            raise RuntimeError(str(result.get("error", "A.E.B rejected request")))
        return result

    def publish(self, packet: dict[str, Any]) -> dict[str, Any]:
        self._ensure()
        payload = dict(packet)
        payload["stream_id"] = str(self.config.get("stream_id", "front-camera-yolo"))
        result = {"json": False, "jpeg": False}
        try:
            self._request("json", "text", str(self.config.get("json_method", "vision.json.publish")), payload)
            result["json"] = True
        except Exception as exc:
            self.emit("A.E.B JSON publish failed", severity="warning", error=str(exc))
            self._close()
        jpeg_value = packet.get("jpeg_base64")
        if isinstance(jpeg_value, str):
            import base64
            try:
                binary = base64.b64decode(jpeg_value, validate=True)
                jpeg_payload = {key: packet[key] for key in ("frame_id", "timestamp", "source", "image_width", "image_height") if key in packet}
                jpeg_payload["stream_id"] = payload["stream_id"]
                self._ensure()
                self._request("jpeg", "vision", str(self.config.get("jpeg_method", "vision.jpeg.publish")), jpeg_payload, binary)
                result["jpeg"] = True
            except Exception as exc:
                self.emit("A.E.B JPEG publish failed", severity="warning", error=str(exc))
                self._close()
        return result

    def _close(self) -> None:
        for socket in self._sockets.values():
            socket.close(linger=0)
        self._sockets.clear()
        self._pollers.clear()

    def close(self) -> None:
        self._close()
