from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import uuid
import unittest
from pathlib import Path

from astrbot_ex.core.connection_manager import ConnectionManager, websocket_connect, websocket_serve, zmq
from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.topic_bus import TopicBus


def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not met before timeout")


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class ConnectionManagerPersistenceTest(unittest.TestCase):
    def test_crud_and_shutdown_preserve_enabled_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "connections.json"
            manager = ConnectionManager(state_path, event_bus=EventBus(), topic_bus=TopicBus())
            created = manager.create(
                {
                    "id": "a",
                    "name": "Main channel",
                    "type": "zmq_client",
                    "enabled": False,
                    "config": {"endpoint": "tcp://127.0.0.1:65534"},
                }
            )
            self.assertEqual(created["id"], "a")
            updated = manager.update("a", {"name": "Renamed channel"})
            self.assertEqual(updated["name"], "Renamed channel")

            manager.start("a")
            manager.close()

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["connections"][0]["enabled"])
            reloaded = ConnectionManager(state_path, event_bus=EventBus(), topic_bus=TopicBus())
            self.assertTrue(reloaded.get("a")["enabled"])
            reloaded.delete("a")
            self.assertEqual(reloaded.list(), [])

    def test_only_one_enabled_astrbotex_client_per_feature(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ConnectionManager(Path(temp_dir) / "connections.json", event_bus=EventBus(), topic_bus=TopicBus())
            config = {"endpoint": "tcp://127.0.0.1:65534", "protocol_profile": "astrbotex", "channel": "audio"}
            manager.create({"id": "audio-a", "name": "Audio A", "type": "zmq_client", "enabled": True, "config": config})
            try:
                with self.assertRaisesRegex(ValueError, "already assigned"):
                    manager.create({"id": "audio-b", "name": "Audio B", "type": "zmq_client", "enabled": True, "config": config})
            finally:
                manager.close()


@unittest.skipIf(zmq is None, "pyzmq is not installed")
class ConnectionManagerZeroMQTest(unittest.TestCase):
    def test_router_and_dealer_exchange_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            topic_bus = TopicBus()
            manager = ConnectionManager(
                Path(temp_dir) / "connections.json",
                event_bus=EventBus(),
                topic_bus=topic_bus,
            )
            port = free_tcp_port()
            manager.create(
                {
                    "id": "router",
                    "name": "Router",
                    "type": "zmq_server",
                    "enabled": False,
                    "config": {"host": "127.0.0.1", "port": port},
                }
            )
            manager.create(
                {
                    "id": "dealer",
                    "name": "Dealer",
                    "type": "zmq_client",
                    "enabled": False,
                    "config": {"endpoint": f"tcp://127.0.0.1:{port}", "identity": "test-dealer"},
                }
            )
            try:
                manager.start("router")
                manager.start("dealer")
                wait_until(lambda: manager.get("router")["runtime"]["state"] == "running")
                wait_until(lambda: manager.get("dealer")["runtime"]["state"] == "running")

                manager.send("dealer", {"kind": "ping", "value": 1})
                wait_until(lambda: manager.get("router")["runtime"]["received"] == 1)
                received = topic_bus.get_latest("connections.router.message")
                self.assertIsNotNone(received)
                self.assertEqual(received.payload["data"]["kind"], "ping")
                self.assertEqual(received.payload["peer"], "test-dealer")

                manager.send("router", {"kind": "pong", "value": 2})
                wait_until(lambda: manager.get("dealer")["runtime"]["received"] == 1)
                reply = topic_bus.get_latest("connections.dealer.message")
                self.assertIsNotNone(reply)
                self.assertEqual(reply.payload["data"]["kind"], "pong")
            finally:
                manager.close()

    def test_astrbotex_profile_matches_response_and_binary_frames(self) -> None:
        port = free_tcp_port()
        context = zmq.Context.instance()
        router = context.socket(zmq.ROUTER)
        router.setsockopt(zmq.LINGER, 0)
        router.bind(f"tcp://127.0.0.1:{port}")
        stop = threading.Event()

        def server_loop() -> None:
            while not stop.is_set():
                if not router.poll(100):
                    continue
                frames = router.recv_multipart()
                peer, envelope = frames[0], json.loads(frames[1].decode("utf-8"))
                method = envelope["method"]
                payload = {"ok": True, "text": "hello"} if method == "stt.transcribe" else {"ok": True, "channel": "audio"}
                response = {
                    "protocol": "astrbotex-zmq", "version": 1, "channel": "audio",
                    "kind": "response", "id": uuid.uuid4().hex, "reply_to": envelope["id"],
                    "method": method, "timestamp": time.time(), "payload": payload,
                }
                router.send_multipart([peer, json.dumps(response).encode("utf-8")] + ([b"WAVE"] if method == "stt.transcribe" else []))

        thread = threading.Thread(target=server_loop, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ConnectionManager(Path(temp_dir) / "connections.json", event_bus=EventBus(), topic_bus=TopicBus())
            manager.create({
                "id": "audio", "name": "Audio", "type": "zmq_client", "enabled": True,
                "config": {"endpoint": f"tcp://127.0.0.1:{port}", "identity": "test-audio", "protocol_profile": "astrbotex", "channel": "audio"},
            })
            try:
                wait_until(lambda: manager.business_status()["audio"]["ready"])
                payload, binary = manager.request_feature("audio", "stt.transcribe", {"filename": "test.wav"}, binary=b"RIFF")
                self.assertEqual(payload["text"], "hello")
                self.assertEqual(binary, b"WAVE")
            finally:
                manager.close()
                stop.set()
                thread.join(timeout=1)
                router.close(linger=0)


@unittest.skipIf(websocket_connect is None or websocket_serve is None, "websockets is not installed")
class ConnectionManagerWebSocketTest(unittest.TestCase):
    def test_server_and_client_exchange_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            topic_bus = TopicBus()
            manager = ConnectionManager(
                Path(temp_dir) / "connections.json",
                event_bus=EventBus(),
                topic_bus=topic_bus,
            )
            port = free_tcp_port()
            manager.create(
                {
                    "id": "ws-server",
                    "name": "WebSocket server",
                    "type": "websocket_server",
                    "enabled": False,
                    "config": {"host": "127.0.0.1", "port": port, "path": "/bridge", "token": "secret"},
                }
            )
            manager.create(
                {
                    "id": "ws-client",
                    "name": "WebSocket client",
                    "type": "websocket_client",
                    "enabled": False,
                    "config": {"url": f"ws://127.0.0.1:{port}/bridge", "token": "secret", "reconnect_interval_sec": 0.1},
                }
            )
            try:
                manager.start("ws-server")
                wait_until(lambda: manager.get("ws-server")["runtime"]["state"] == "running")
                manager.start("ws-client")
                wait_until(lambda: manager.get("ws-client")["runtime"]["clients"] == 1)
                wait_until(lambda: manager.get("ws-server")["runtime"]["clients"] == 1)

                manager.send("ws-client", {"kind": "ping"})
                wait_until(lambda: manager.get("ws-server")["runtime"]["received"] == 1)
                received = topic_bus.get_latest("connections.ws-server.message")
                self.assertIsNotNone(received)
                self.assertEqual(json.loads(received.payload["data"])["kind"], "ping")

                manager.send("ws-server", {"kind": "pong"})
                wait_until(lambda: manager.get("ws-client")["runtime"]["received"] == 1)
                reply = topic_bus.get_latest("connections.ws-client.message")
                self.assertIsNotNone(reply)
                self.assertEqual(json.loads(reply.payload["data"])["kind"], "pong")
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
