from __future__ import annotations

import json
import socket
import tempfile
import time
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
