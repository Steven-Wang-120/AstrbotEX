from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.topic_bus import TopicBus

try:
    import zmq
except ImportError:  # pragma: no cover - reported through runtime status
    zmq = None  # type: ignore[assignment]

try:
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect as websocket_connect
    from websockets.sync.server import serve as websocket_serve
except ImportError:  # pragma: no cover - reported through runtime status
    ConnectionClosed = Exception  # type: ignore[misc,assignment]
    websocket_connect = None  # type: ignore[assignment]
    websocket_serve = None  # type: ignore[assignment]


MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_ASTRBOTEX_MESSAGE_BYTES = 64 * 1024 * 1024
MAX_RECENT_MESSAGES = 40
ASTRBOTEX_PROTOCOL = "astrbotex-zmq"
ASTRBOTEX_PROTOCOL_VERSION = 1
ASTRBOTEX_CHANNELS = frozenset({"text", "audio", "vision"})
_VALID_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

CONNECTION_TYPES: dict[str, dict[str, Any]] = {
    "zmq_server": {
        "label": "ZeroMQ 服务器",
        "description": "ROUTER 端，绑定端口并接收多个 DEALER 对端。",
        "protocol": "ZeroMQ",
        "fields": ["host", "port", "identity", "protocol_profile"],
        "defaults": {"host": "0.0.0.0", "port": 8766, "identity": "", "protocol_profile": "raw"},
    },
    "zmq_client": {
        "label": "ZeroMQ 客户端",
        "description": "DEALER 端，连接 ROUTER 服务并支持双向消息。",
        "protocol": "ZeroMQ",
        "fields": ["endpoint", "identity", "protocol_profile", "channel"],
        "defaults": {"endpoint": "tcp://127.0.0.1:8766", "identity": "astrbotex-main", "channel": "text", "protocol_profile": "raw"},
    },
    "websocket_server": {
        "label": "WebSocket 服务器",
        "description": "监听 ws/wss 对端，向已连接客户端广播消息。",
        "protocol": "WebSocket",
        "fields": ["host", "port", "path", "token", "ping_interval_sec"],
        "defaults": {"host": "0.0.0.0", "port": 8780, "path": "/", "token": "", "ping_interval_sec": 20},
    },
    "websocket_client": {
        "label": "WebSocket 客户端",
        "description": "主动连接 ws/wss 服务，断线后按间隔自动重连。",
        "protocol": "WebSocket",
        "fields": ["url", "token", "reconnect_interval_sec", "ping_interval_sec"],
        "defaults": {"url": "ws://127.0.0.1:8780/", "token": "", "reconnect_interval_sec": 3, "ping_interval_sec": 20},
    },
}


def _now() -> float:
    return time.time()


def _normalise_id(value: Any) -> str:
    candidate = str(value or "").strip().lower().replace(" ", "-")
    candidate = re.sub(r"[^a-z0-9_-]", "-", candidate).strip("-_")
    if not candidate:
        candidate = f"connection-{uuid.uuid4().hex[:8]}"
    if not candidate[0].isalpha():
        candidate = f"connection-{candidate}"
    return candidate[:64]


def _safe_preview(value: Any, limit: int = 400) -> str:
    if isinstance(value, bytes):
        return f"<binary {len(value)} B>"
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


@dataclass(slots=True)
class ConnectionRecord:
    id: str
    name: str
    type: str
    enabled: bool
    config: dict[str, Any]
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ConnectionRecord":
        connection_type = str(value.get("type", ""))
        if connection_type not in CONNECTION_TYPES:
            raise ValueError(f"unsupported connection type: {connection_type}")
        connection_id = _normalise_id(value.get("id"))
        if not _VALID_ID.fullmatch(connection_id):
            raise ValueError("connection id must start with a letter and contain only a-z, 0-9, _ or -")
        return cls(
            id=connection_id,
            name=str(value.get("name") or connection_id).strip()[:80] or connection_id,
            type=connection_type,
            enabled=bool(value.get("enabled", False)),
            config=dict(value.get("config") or {}),
            created_at=float(value.get("created_at") or _now()),
            updated_at=float(value.get("updated_at") or _now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "enabled": self.enabled,
            "config": dict(self.config),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class _Adapter:
    def __init__(self, manager: "ConnectionManager", record: ConnectionRecord) -> None:
        self.manager = manager
        self.record = record
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._state = "stopped"
        self._error: str | None = None
        self._started_at: float | None = None
        self._last_message_at: float | None = None
        self._received = 0
        self._sent = 0
        self._bytes_received = 0
        self._bytes_sent = 0
        self._clients = 0
        self._recent: deque[dict[str, Any]] = deque(maxlen=MAX_RECENT_MESSAGES)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self.stop_event.clear()
            self._state = "starting"
            self._error = None
            self._thread = threading.Thread(target=self._run_guarded, name=f"astrbotex-connection-{self.record.id}", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.5)
        with self._lock:
            self._state = "stopped"
            self._clients = 0

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "error": self._error,
                "started_at": self._started_at,
                "last_message_at": self._last_message_at,
                "received": self._received,
                "sent": self._sent,
                "bytes_received": self._bytes_received,
                "bytes_sent": self._bytes_sent,
                "clients": self._clients,
                "recent_messages": list(self._recent),
            }

    def send(self, data: Any, *, peer: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def _run_guarded(self) -> None:
        with self._lock:
            self._state = "running"
            self._started_at = _now()
        self.manager._emit("connection started", connection=self.record.id, transport=self.record.type)
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001
            if not self.stop_event.is_set():
                with self._lock:
                    self._state = "error"
                    self._error = str(exc)
                self.manager._emit("connection failed", severity="error", connection=self.record.id, error=str(exc))
        finally:
            self._close()
            with self._lock:
                if self._state != "error":
                    self._state = "stopped"
                self._clients = 0

    def _received_message(self, data: Any, *, peer: str | None = None, binary_bytes: int = 0) -> None:
        timestamp = _now()
        item = {"direction": "in", "timestamp": timestamp, "peer": peer, "preview": _safe_preview(data), "bytes": binary_bytes or len(str(data).encode("utf-8"))}
        with self._lock:
            self._received += 1
            self._bytes_received += item["bytes"]
            self._last_message_at = timestamp
            self._recent.append(item)
        self.manager._publish_message(self.record.id, data, peer=peer, binary_bytes=binary_bytes)

    def _sent_message(self, data: Any, *, peer: str | None = None, binary_bytes: int = 0) -> None:
        item = {"direction": "out", "timestamp": _now(), "peer": peer, "preview": _safe_preview(data), "bytes": binary_bytes or len(str(data).encode("utf-8"))}
        with self._lock:
            self._sent += 1
            self._bytes_sent += item["bytes"]
            self._recent.append(item)

    def _run(self) -> None:
        raise NotImplementedError

    def _close(self) -> None:
        return


class _ZmqAdapter(_Adapter):
    def __init__(self, manager: "ConnectionManager", record: ConnectionRecord) -> None:
        super().__init__(manager, record)
        self._socket: Any | None = None
        self._socket_lock = threading.RLock()
        self._latest_peer: bytes | None = None
        self._peers: dict[bytes, float] = {}
        self._outbound: queue.Queue[tuple[Any, bytes | None, str | None, threading.Event, dict[str, Any]]] = queue.Queue(maxsize=100)
        self._pending: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self._protocol_ready = False

    def _endpoint(self) -> str:
        config = self.record.config
        if self.record.type == "zmq_server":
            return f"tcp://{str(config.get('host') or '0.0.0.0')}:{self.manager.port(config.get('port'))}"
        endpoint = str(config.get("endpoint") or "").strip()
        if not endpoint.startswith("tcp://"):
            raise ValueError("ZeroMQ client endpoint must start with tcp://")
        return endpoint

    def _encode(self, data: Any) -> bytes:
        if isinstance(data, bytes):
            return data
        if isinstance(data, str):
            return data.encode("utf-8")
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _decode(self, frames: list[bytes]) -> Any:
        if len(frames) == 1:
            try:
                return json.loads(frames[0].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return frames[0]
        return {"frames": frames, "frame_count": len(frames)}

    def _run(self) -> None:
        if zmq is None:
            raise RuntimeError("pyzmq is not installed")
        context = zmq.Context.instance()
        kind = zmq.ROUTER if self.record.type == "zmq_server" else zmq.DEALER
        socket = context.socket(kind)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.RCVHWM, 100)
        socket.setsockopt(zmq.SNDHWM, 100)
        message_limit = MAX_ASTRBOTEX_MESSAGE_BYTES if self.record.config.get("protocol_profile") == "astrbotex" else MAX_MESSAGE_BYTES
        socket.setsockopt(zmq.MAXMSGSIZE, message_limit)
        if kind == zmq.ROUTER:
            socket.setsockopt(zmq.ROUTER_MANDATORY, 1)
            socket.bind(self._endpoint())
        else:
            identity = str(self.record.config.get("identity") or self.record.id).encode("utf-8")
            socket.setsockopt(zmq.IDENTITY, identity)
            socket.connect(self._endpoint())
        with self._socket_lock:
            self._socket = socket
        if kind == zmq.DEALER and self.record.config.get("protocol_profile") == "astrbotex":
            channel = str(self.record.config.get("channel") or "text")
            self._send_now(socket, self._envelope("request", "system.hello", {"client": "AstrBotEX", "instance_id": self.record.id}), peer=None)
        poller = zmq.Poller()
        poller.register(socket, zmq.POLLIN)
        while not self.stop_event.is_set():
            self._flush_outbound(socket)
            events = dict(poller.poll(50))
            if socket not in events:
                continue
            frames = socket.recv_multipart()
            if kind == zmq.ROUTER:
                if len(frames) < 2:
                    continue
                peer, body = frames[0], frames[1:]
                self._latest_peer = peer
                self._peers[peer] = _now()
                with self._lock:
                    self._clients = len(self._peers)
            else:
                peer, body = None, frames
            total = sum(len(frame) for frame in body)
            if total > message_limit:
                continue
            peer_text = peer.decode("utf-8", "replace") if peer else None
            decoded = self._decode_protocol(body) if self.record.config.get("protocol_profile") == "astrbotex" else self._decode(body)
            self._received_message(decoded, peer=peer_text, binary_bytes=total)
            if self.record.config.get("protocol_profile") == "astrbotex" and isinstance(decoded, dict):
                self._handle_protocol(socket, decoded, body[1] if len(body) > 1 else None, peer=peer_text)
        self._fail_outbound("connection stopped")

    def send(self, data: Any, *, peer: str | None = None) -> dict[str, Any]:
        with self._socket_lock:
            if self._socket is None:
                raise RuntimeError("connection is not running")
        completed = threading.Event()
        outcome: dict[str, Any] = {}
        try:
            self._outbound.put_nowait((data, None, peer, completed, outcome))
        except queue.Full as exc:
            raise RuntimeError("ZeroMQ send queue is full") from exc
        if not completed.wait(5.0):
            raise RuntimeError("ZeroMQ send timed out")
        error = outcome.get("error")
        if error is not None:
            raise RuntimeError(str(error))
        return dict(outcome.get("result") or {"ok": True})

    def request(self, method: str, payload: dict[str, Any], *, binary: bytes | None, timeout_sec: float) -> tuple[dict[str, Any], bytes | None]:
        if self.record.type != "zmq_client" or self.record.config.get("protocol_profile") != "astrbotex":
            raise RuntimeError("connection is not an AstrBotEX ZeroMQ client")
        message_id = uuid.uuid4().hex
        completed = threading.Event()
        outcome: dict[str, Any] = {}
        with self._lock:
            self._pending[message_id] = (completed, outcome)
        try:
            self._outbound.put_nowait((self._envelope("request", method, payload, message_id=message_id), binary, None, threading.Event(), {}))
        except queue.Full as exc:
            with self._lock:
                self._pending.pop(message_id, None)
            raise RuntimeError("ZeroMQ send queue is full") from exc
        try:
            if not completed.wait(timeout_sec):
                raise TimeoutError(f"{self.record.config.get('channel')}/{method} timed out after {timeout_sec:.1f}s")
            if outcome.get("error"):
                raise RuntimeError(str(outcome["error"]))
            return dict(outcome.get("payload") or {}), outcome.get("binary")
        finally:
            with self._lock:
                self._pending.pop(message_id, None)

    @staticmethod
    def _decode_protocol(frames: list[bytes]) -> Any:
        try:
            value = json.loads(frames[0].decode("utf-8"))
        except (IndexError, UnicodeDecodeError, json.JSONDecodeError):
            return {"frames": frames, "frame_count": len(frames)}
        return value

    def _send_now(self, socket: Any, data: Any, *, peer: str | None, binary: bytes | None = None) -> dict[str, Any]:
        encoded = self._encode(data)
        limit = MAX_ASTRBOTEX_MESSAGE_BYTES if self.record.config.get("protocol_profile") == "astrbotex" else MAX_MESSAGE_BYTES
        if len(encoded) > limit or (binary is not None and len(binary) > limit):
            raise ValueError("message exceeds configured limit")
        if self.record.type == "zmq_server":
            target = peer.encode("utf-8") if peer else self._latest_peer
            if target is None:
                raise RuntimeError("no ZeroMQ peer is connected")
            socket.send_multipart([target, encoded] + ([binary] if binary is not None else []))
            peer = target.decode("utf-8", "replace")
        else:
            socket.send_multipart([encoded] + ([binary] if binary is not None else []))
        total = len(encoded) + (len(binary) if binary is not None else 0)
        self._sent_message(data, peer=peer, binary_bytes=total)
        return {"ok": True, "bytes": total, "peer": peer}

    def _flush_outbound(self, socket: Any) -> None:
        while True:
            try:
                data, binary, peer, completed, outcome = self._outbound.get_nowait()
            except queue.Empty:
                return
            try:
                outcome["result"] = self._send_now(socket, data, peer=peer, binary=binary)
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = str(exc)
            finally:
                completed.set()

    def _fail_outbound(self, reason: str) -> None:
        while True:
            try:
                _, _, _, completed, outcome = self._outbound.get_nowait()
            except queue.Empty:
                return
            outcome["error"] = reason
            completed.set()

    def _envelope(self, kind: str, method: str, payload: dict[str, Any], *, message_id: str | None = None, reply_to: str | None = None) -> dict[str, Any]:
        envelope = {"protocol": ASTRBOTEX_PROTOCOL, "version": ASTRBOTEX_PROTOCOL_VERSION, "channel": str(self.record.config.get("channel") or "text"), "kind": kind, "id": message_id or uuid.uuid4().hex, "method": method, "timestamp": _now(), "payload": payload}
        if reply_to is not None:
            envelope["reply_to"] = reply_to
        return envelope

    def _handle_protocol(self, socket: Any, envelope: dict[str, Any], binary: bytes | None, *, peer: str | None) -> None:
        if envelope.get("protocol") != ASTRBOTEX_PROTOCOL or envelope.get("version") != ASTRBOTEX_PROTOCOL_VERSION:
            return
        if envelope.get("channel") != self.record.config.get("channel"):
            return
        kind = envelope.get("kind")
        if kind == "response":
            if envelope.get("method") == "system.hello":
                with self._lock:
                    self._protocol_ready = bool(envelope.get("payload", {}).get("ok", True))
                    self._clients = 1 if self._protocol_ready else 0
            reply_to = str(envelope.get("reply_to") or "")
            with self._lock:
                pending = self._pending.get(reply_to)
            if pending is not None:
                completed, outcome = pending
                payload = envelope.get("payload", {})
                outcome["payload"] = payload
                outcome["binary"] = binary
                if not payload.get("ok", True):
                    outcome["error"] = payload.get("error", "remote request failed")
                completed.set()
            return
        if kind not in {"request", "event"}:
            return
        result, response_binary = self.manager._handle_business_request(
            str(self.record.config.get("channel") or "text"),
            str(envelope.get("method") or ""),
            dict(envelope.get("payload") or {}),
            binary,
        )
        if kind == "request":
            response = self._envelope("response", str(envelope.get("method") or ""), result, reply_to=str(envelope.get("id") or ""))
            self._send_now(socket, response, peer=peer, binary=response_binary)

    def status(self) -> dict[str, Any]:
        result = super().status()
        result["business_feature"] = self.record.config.get("channel") if self.record.config.get("protocol_profile") == "astrbotex" else None
        result["business_ready"] = self._protocol_ready
        return result

    def stop(self) -> None:
        self.stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.5)
        self._fail_outbound("connection stopped")
        with self._lock:
            self._state = "stopped"
            self._clients = 0

    def _close(self) -> None:
        with self._socket_lock:
            socket, self._socket = self._socket, None
        if socket is not None:
            socket.close(linger=0)


class _WebSocketServerAdapter(_Adapter):
    def __init__(self, manager: "ConnectionManager", record: ConnectionRecord) -> None:
        super().__init__(manager, record)
        self._server: Any | None = None
        self._connections: set[Any] = set()
        self._connections_lock = threading.RLock()

    def _run(self) -> None:
        if websocket_serve is None:
            raise RuntimeError("websockets is not installed")
        config = self.record.config
        expected_path = str(config.get("path") or "/")
        token = str(config.get("token") or "")

        def handler(websocket: Any) -> None:
            path = str(getattr(getattr(websocket, "request", None), "path", expected_path))
            if path != expected_path:
                websocket.close(code=1008, reason="unexpected path")
                return
            if token:
                auth = str(getattr(getattr(websocket, "request", None), "headers", {}).get("Authorization", ""))
                if auth != f"Bearer {token}":
                    websocket.close(code=1008, reason="unauthorized")
                    return
            with self._connections_lock:
                self._connections.add(websocket)
                with self._lock:
                    self._clients = len(self._connections)
            peer = str(getattr(websocket, "remote_address", ""))
            try:
                for message in websocket:
                    self._received_message(message, peer=peer, binary_bytes=len(message) if isinstance(message, bytes) else 0)
            except ConnectionClosed:
                pass
            finally:
                with self._connections_lock:
                    self._connections.discard(websocket)
                    with self._lock:
                        self._clients = len(self._connections)

        server = websocket_serve(handler, str(config.get("host") or "0.0.0.0"), self.manager.port(config.get("port")), ping_interval=float(config.get("ping_interval_sec") or 20), max_size=MAX_MESSAGE_BYTES)
        self._server = server
        server.serve_forever()

    def send(self, data: Any, *, peer: str | None = None) -> dict[str, Any]:
        payload = data if isinstance(data, (str, bytes)) else json.dumps(data, ensure_ascii=False)
        with self._connections_lock:
            connections = list(self._connections)
        if not connections:
            raise RuntimeError("no WebSocket client is connected")
        sent = 0
        for websocket in connections:
            if peer and peer not in str(getattr(websocket, "remote_address", "")):
                continue
            try:
                websocket.send(payload)
                sent += 1
            except ConnectionClosed:
                continue
        if not sent:
            raise RuntimeError("no matching WebSocket client is connected")
        self._sent_message(payload, peer=peer, binary_bytes=len(payload) if isinstance(payload, bytes) else 0)
        return {"ok": True, "clients": sent}

    def _close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()


class _WebSocketClientAdapter(_Adapter):
    def __init__(self, manager: "ConnectionManager", record: ConnectionRecord) -> None:
        super().__init__(manager, record)
        self._connection: Any | None = None
        self._connection_lock = threading.RLock()

    def _run(self) -> None:
        if websocket_connect is None:
            raise RuntimeError("websockets is not installed")
        config = self.record.config
        url = str(config.get("url") or "")
        if not url.startswith(("ws://", "wss://")):
            raise ValueError("WebSocket client URL must start with ws:// or wss://")
        token = str(config.get("token") or "")
        headers = {"Authorization": f"Bearer {token}"} if token else None
        delay = max(0.2, float(config.get("reconnect_interval_sec") or 3))
        while not self.stop_event.is_set():
            try:
                with websocket_connect(url, additional_headers=headers, ping_interval=float(config.get("ping_interval_sec") or 20), max_size=MAX_MESSAGE_BYTES, open_timeout=5) as websocket:
                    with self._connection_lock:
                        self._connection = websocket
                    with self._lock:
                        self._clients = 1
                        self._error = None
                        self._state = "running"
                    while not self.stop_event.is_set():
                        try:
                            message = websocket.recv(timeout=0.2)
                        except TimeoutError:
                            continue
                        self._received_message(message, peer=url, binary_bytes=len(message) if isinstance(message, bytes) else 0)
            except Exception as exc:  # noqa: BLE001
                if not self.stop_event.is_set():
                    with self._lock:
                        self._error = str(exc)
                        self._state = "reconnecting"
                    self.stop_event.wait(delay)
            finally:
                with self._connection_lock:
                    self._connection = None
                with self._lock:
                    self._clients = 0
                    if self._state != "error" and not self.stop_event.is_set():
                        self._state = "running"

    def send(self, data: Any, *, peer: str | None = None) -> dict[str, Any]:
        del peer
        payload = data if isinstance(data, (str, bytes)) else json.dumps(data, ensure_ascii=False)
        with self._connection_lock:
            websocket = self._connection
            if websocket is None:
                raise RuntimeError("WebSocket client is not connected")
            websocket.send(payload)
        self._sent_message(payload, binary_bytes=len(payload) if isinstance(payload, bytes) else 0)
        return {"ok": True}

    def _close(self) -> None:
        with self._connection_lock:
            websocket, self._connection = self._connection, None
        if websocket is not None:
            try:
                websocket.close()
            except Exception:
                pass


class ConnectionManager:
    """Persists and supervises externally-addressable messaging connections."""

    def __init__(self, state_path: Path, *, event_bus: EventBus, topic_bus: TopicBus) -> None:
        self.state_path = state_path
        self.event_bus = event_bus
        self.topic_bus = topic_bus
        self._lock = threading.RLock()
        self._records: dict[str, ConnectionRecord] = {}
        self._adapters: dict[str, _Adapter] = {}
        self._business_handler: Callable[[str, str, dict[str, Any], bytes | None], tuple[dict[str, Any], bytes | None]] | None = None
        self._load()

    @staticmethod
    def port(value: Any) -> int:
        try:
            port = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("port must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        return port

    def list_types(self) -> dict[str, Any]:
        return {key: {**value, "defaults": dict(value["defaults"])} for key, value in CONNECTION_TYPES.items()}

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._view(record) for record in sorted(self._records.values(), key=lambda item: item.name.lower())]

    def get(self, connection_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._records.get(connection_id)
            if record is None:
                raise KeyError(f"connection not found: {connection_id}")
            return self._view(record)

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = self._record_from_payload(payload)
        with self._lock:
            if record.id in self._records:
                raise ValueError(f"connection already exists: {record.id}")
            self._ensure_feature_available_locked(record)
            self._records[record.id] = record
            self._save_locked()
        if record.enabled:
            self.start(record.id)
        return self.get(record.id)

    def update(self, connection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            old = self._records.get(connection_id)
            if old is None:
                raise KeyError(f"connection not found: {connection_id}")
            merged = old.to_dict()
            merged.update({key: value for key, value in payload.items() if key in {"name", "type", "enabled", "config"}})
            merged["id"] = connection_id
            merged["updated_at"] = _now()
            record = self._record_from_payload(merged)
            self._ensure_feature_available_locked(record, excluding=connection_id)
            restart = old.enabled or record.enabled
        self.stop(connection_id)
        with self._lock:
            self._records[connection_id] = record
            self._save_locked()
        if restart and record.enabled:
            self.start(connection_id)
        return self.get(connection_id)

    def delete(self, connection_id: str) -> None:
        self.stop(connection_id)
        with self._lock:
            if connection_id not in self._records:
                raise KeyError(f"connection not found: {connection_id}")
            self._records.pop(connection_id)
            self._save_locked()

    def start(self, connection_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._records.get(connection_id)
            if record is None:
                raise KeyError(f"connection not found: {connection_id}")
            self._ensure_feature_available_locked(record, excluding=connection_id)
            record.enabled = True
            record.updated_at = _now()
            adapter = self._adapters.get(connection_id)
            if adapter is None:
                adapter = self._adapter_for(record)
                self._adapters[connection_id] = adapter
            self._save_locked()
        adapter.start()
        return self.get(connection_id)

    def stop(self, connection_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._records.get(connection_id)
            if record is None:
                raise KeyError(f"connection not found: {connection_id}")
            adapter = self._adapters.pop(connection_id, None)
            record.enabled = False
            record.updated_at = _now()
            self._save_locked()
        if adapter is not None:
            adapter.stop()
        self._emit("connection stopped", connection=connection_id)
        return self.get(connection_id)

    def send(self, connection_id: str, data: Any, *, peer: str | None = None) -> dict[str, Any]:
        with self._lock:
            adapter = self._adapters.get(connection_id)
        if adapter is None:
            raise RuntimeError("connection is not running")
        return adapter.send(data, peer=peer)

    def set_business_handler(self, handler: Callable[[str, str, dict[str, Any], bytes | None], tuple[dict[str, Any], bytes | None]]) -> None:
        self._business_handler = handler

    def request_feature(self, feature: str, method: str, payload: dict[str, Any] | None = None, *, binary: bytes | None = None, timeout_sec: float = 10.0) -> tuple[dict[str, Any], bytes | None]:
        if feature not in ASTRBOTEX_CHANNELS:
            raise ValueError(f"unsupported AstrBotEX business feature: {feature}")
        with self._lock:
            matches = [
                (record, self._adapters.get(record.id))
                for record in self._records.values()
                if record.enabled
                and record.type == "zmq_client"
                and record.config.get("protocol_profile") == "astrbotex"
                and record.config.get("channel") == feature
            ]
        if not matches:
            raise RuntimeError(f"AstrBotEX {feature} business connection is not configured or enabled")
        adapter = matches[0][1]
        if not isinstance(adapter, _ZmqAdapter):
            raise RuntimeError(f"AstrBotEX {feature} business connection is not running")
        return adapter.request(method, payload or {}, binary=binary, timeout_sec=timeout_sec)

    def business_status(self) -> dict[str, Any]:
        with self._lock:
            result: dict[str, Any] = {}
            for feature in sorted(ASTRBOTEX_CHANNELS):
                record = next((item for item in self._records.values() if item.enabled and item.type == "zmq_client" and item.config.get("protocol_profile") == "astrbotex" and item.config.get("channel") == feature), None)
                adapter = self._adapters.get(record.id) if record is not None else None
                runtime = adapter.status() if adapter is not None else {}
                result[feature] = {"connection_id": record.id if record is not None else None, "configured": record is not None, "ready": bool(runtime.get("business_ready")), "error": runtime.get("error")}
            return result

    def close(self) -> None:
        with self._lock:
            adapters = list(self._adapters.values())
            self._adapters.clear()
        for adapter in adapters:
            adapter.stop()

    def _record_from_payload(self, payload: dict[str, Any]) -> ConnectionRecord:
        record = ConnectionRecord.from_dict(payload)
        defaults = dict(CONNECTION_TYPES[record.type]["defaults"])
        defaults.update(record.config)
        record.config = defaults
        self._validate(record)
        return record

    def _validate(self, record: ConnectionRecord) -> None:
        config = record.config
        if record.type in {"zmq_server", "websocket_server"}:
            self.port(config.get("port"))
        if record.type == "zmq_client" and not str(config.get("endpoint") or "").startswith("tcp://"):
            raise ValueError("ZeroMQ endpoint must start with tcp://")
        if record.type == "websocket_client" and not str(config.get("url") or "").startswith(("ws://", "wss://")):
            raise ValueError("WebSocket URL must start with ws:// or wss://")
        if record.type == "websocket_server":
            path = str(config.get("path") or "/")
            if not path.startswith("/"):
                raise ValueError("WebSocket path must start with /")
        if str(config.get("protocol_profile") or "raw") not in {"raw", "astrbotex"}:
            raise ValueError("protocol_profile must be raw or astrbotex")
        if record.type == "zmq_client" and config.get("protocol_profile") == "astrbotex" and str(config.get("channel") or "") not in ASTRBOTEX_CHANNELS:
            raise ValueError("AstrBotEX business feature must be text, audio, or vision")

    def _ensure_feature_available_locked(self, record: ConnectionRecord, *, excluding: str | None = None) -> None:
        if not record.enabled or record.type != "zmq_client" or record.config.get("protocol_profile") != "astrbotex":
            return
        feature = record.config.get("channel")
        conflict = next((item for item in self._records.values() if item.id != excluding and item.enabled and item.type == "zmq_client" and item.config.get("protocol_profile") == "astrbotex" and item.config.get("channel") == feature), None)
        if conflict is not None:
            raise ValueError(f"AstrBotEX business feature {feature} is already assigned to {conflict.id}")

    def _handle_business_request(self, feature: str, method: str, payload: dict[str, Any], binary: bytes | None) -> tuple[dict[str, Any], bytes | None]:
        if self._business_handler is None:
            return {"ok": False, "error": "AstrBotEX business handler is not ready"}, None
        return self._business_handler(feature, method, payload, binary)

    def _adapter_for(self, record: ConnectionRecord) -> _Adapter:
        if record.type.startswith("zmq_"):
            return _ZmqAdapter(self, record)
        if record.type == "websocket_server":
            return _WebSocketServerAdapter(self, record)
        return _WebSocketClientAdapter(self, record)

    def _view(self, record: ConnectionRecord) -> dict[str, Any]:
        result = record.to_dict()
        adapter = self._adapters.get(record.id)
        result["runtime"] = adapter.status() if adapter is not None else {"state": "stopped", "error": None, "started_at": None, "last_message_at": None, "received": 0, "sent": 0, "bytes_received": 0, "bytes_sent": 0, "clients": 0, "recent_messages": []}
        return result

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            values = raw.get("connections", []) if isinstance(raw, dict) else []
            for value in values:
                record = self._record_from_payload(dict(value))
                self._records[record.id] = record
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001
            self._emit("connection state ignored", severity="error", error=str(exc))

    def start_enabled(self) -> None:
        with self._lock:
            ids = [record.id for record in self._records.values() if record.enabled]
        for connection_id in ids:
            self.start(connection_id)

    def _save_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"connections": [record.to_dict() for record in self._records.values()]}
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.state_path)

    def _emit(self, message: str, severity: str = "info", **details: Any) -> None:
        self.event_bus.emit("connection", message, severity=severity, **details)

    def _publish_message(self, connection_id: str, data: Any, *, peer: str | None, binary_bytes: int) -> None:
        self.topic_bus.publish_payload(
            f"connections.{connection_id}.message",
            timestamp=_now(),
            source=f"connection:{connection_id}",
            payload={"data": data, "peer": peer, "binary_bytes": binary_bytes},
        )
        self._emit("connection message received", connection=connection_id, peer=peer or "", preview=_safe_preview(data, 180))
