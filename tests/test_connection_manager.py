from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import uuid
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_ex.core.connection_manager import _ZmqAdapter, ConnectionManager, websocket_connect, websocket_serve, zmq
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
    def test_running_waits_for_socket_initialization(self) -> None:
        for delayed_id in ("router", "dealer"):
            with self.subTest(delayed_id=delayed_id), tempfile.TemporaryDirectory() as temp_dir:
                manager = ConnectionManager(Path(temp_dir) / "connections.json", event_bus=EventBus(), topic_bus=TopicBus())
                port = free_tcp_port()
                manager.create({"id": "router", "type": "zmq_server", "enabled": False,
                                "config": {"host": "127.0.0.1", "port": port}})
                manager.create({"id": "dealer", "type": "zmq_client", "enabled": False,
                                "config": {"endpoint": f"tcp://127.0.0.1:{port}"}})
                entered = threading.Event()
                release = threading.Event()
                original_endpoint = _ZmqAdapter._endpoint

                def delayed_endpoint(adapter):
                    if adapter.record.id == delayed_id:
                        entered.set()
                        if not release.wait(3):
                            raise RuntimeError("test initialization gate timed out")
                    return original_endpoint(adapter)

                try:
                    with patch.object(_ZmqAdapter, "_endpoint", delayed_endpoint):
                        manager.start("router")
                        manager.start("dealer")
                        self.assertTrue(entered.wait(2), "initialization did not enter gate")
                        runtime = manager.get(delayed_id)["runtime"]
                        self.assertEqual(runtime["state"], "starting")
                        self.assertIsNone(runtime["started_at"])
                        release.set()
                        for connection_id in ("router", "dealer"):
                            wait_until(lambda: manager.get(connection_id)["runtime"]["state"] == "running")
                        manager.send("dealer", {"kind": "ping"})
                        wait_until(lambda: manager.get("router")["runtime"]["received"] == 1)
                        manager.send("router", {"kind": "pong"})
                        wait_until(lambda: manager.get("dealer")["runtime"]["received"] == 1)
                finally:
                    release.set()
                    manager.close()

    def test_initialization_failure_does_not_publish_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ConnectionManager(Path(temp_dir) / "connections.json", event_bus=EventBus(), topic_bus=TopicBus())
            manager.create({"id": "dealer", "type": "zmq_client", "enabled": False,
                            "config": {"endpoint": "tcp://127.0.0.1:65534"}})
            try:
                with patch.object(_ZmqAdapter, "_endpoint", side_effect=ValueError("initialization failed")):
                    manager.start("dealer")
                    wait_until(lambda: manager.get("dealer")["runtime"]["state"] == "error"
                               and manager._adapters["dealer"]._socket is None)
                runtime = manager.get("dealer")["runtime"]
                self.assertIsNone(runtime["started_at"])
                self.assertEqual(runtime["error"], "initialization failed")
            finally:
                manager.close()

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


class ControlChannelTest(unittest.TestCase):
    """B09 regressions: the control channel and ``upper.*`` dispatch."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.manager = ConnectionManager(
            Path(self._temp.name) / "connections.json",
            event_bus=EventBus(),
            topic_bus=TopicBus(),
        )
        self.addCleanup(self.manager.close)
        self.business: list[tuple] = []
        self.manager.set_business_handler(
            lambda *args: (self.business.append(args), {"ok": True, "source": "interaction"})[1])

    def _control(self, *, enabled: bool = True, **config):
        payload = {
            "id": "control", "name": "Upper control", "type": "zmq_client", "enabled": enabled,
            "config": {
                "endpoint": "tcp://127.0.0.1:8766", "identity": "ex-control",
                "protocol_profile": "astrbotex", "channel": "control",
                "upper_robot_id": "robot-a", "upper_route_refs": "route-1,route-2",
                "upper_ex_session": "pinned-session",
            },
        }
        payload["config"].update(config)
        return self.manager.create(payload)

    def test_control_is_an_allowed_channel(self) -> None:
        record = self._control()
        self.assertEqual(record["config"]["channel"], "control")
        self.assertEqual(record["config"]["upper_robot_id"], "robot-a")

    def test_existing_channels_still_work_and_default_to_text(self) -> None:
        record = self.manager.create({
            "id": "text", "name": "Text", "type": "zmq_client", "enabled": False,
            "config": {"endpoint": "tcp://127.0.0.1:8766"},
        })
        self.assertEqual(record["config"]["channel"], "text")
        # Old records are never migrated to control.
        self.assertEqual(record["config"]["upper_robot_id"], "")

    def test_invalid_upper_identity_fields_are_rejected(self) -> None:
        for key, value in (("upper_robot_id", "has space"),
                           ("upper_robot_id", "r" * 129),
                           ("upper_ex_session", "tab\there"),
                           ("upper_route_refs", 42)):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    self._control(**{key: value})

    def test_control_is_unique_per_channel(self) -> None:
        self._control()
        with self.assertRaises(ValueError):
            self.manager.create({
                "id": "control-2", "name": "Second", "type": "zmq_client", "enabled": True,
                "config": {"endpoint": "tcp://127.0.0.1:8766", "protocol_profile": "astrbotex",
                           "channel": "control", "upper_robot_id": "robot-b"},
            })

    def test_decision_route_stays_text_only(self) -> None:
        self._control()
        with self.assertRaises(RuntimeError):
            self.manager.decision_connection_id()
        self.manager.create({
            "id": "text", "name": "Text", "type": "zmq_client", "enabled": True,
            "config": {"endpoint": "tcp://127.0.0.1:8766", "protocol_profile": "astrbotex", "channel": "text"},
        })
        self.assertEqual(self.manager.decision_connection_id(), "text")
        self.assertEqual(self.manager.control_connection_ids(), ["control"])

    def test_upper_methods_need_the_control_channel_and_never_fall_back(self) -> None:
        self._control()
        self.manager.create({
            "id": "text", "name": "Text", "type": "zmq_client", "enabled": False,
            "config": {"endpoint": "tcp://127.0.0.1:8766", "protocol_profile": "astrbotex", "channel": "text"},
        })
        self.manager.set_step_input_handler(
            lambda connection_id, method, payload, binary: ({"ok": True, "handled": method}, None))

        result, _ = self.manager._handle_business_request(
            "text", "upper.steps.inject", {}, None, connection_id="text")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "route_mismatch")
        self.assertEqual(self.business, [])

        result, _ = self.manager._handle_business_request(
            "control", "upper.steps.inject", {}, None, connection_id="control")
        self.assertTrue(result["ok"])
        self.assertEqual(result["handled"], "upper.steps.inject")
        self.assertEqual(self.business, [])

    def test_unknown_method_on_control_is_rejected(self) -> None:
        self._control()
        self.manager.set_step_input_handler(
            lambda connection_id, method, payload, binary: ({"ok": True}, None))
        for method in ("interaction.message", "decision.status", "runtime.status"):
            with self.subTest(method=method):
                result, _ = self.manager._handle_business_request(
                    "control", method, {}, None, connection_id="control")
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "unsupported_method")
        self.assertEqual(self.business, [])

    def test_upper_methods_report_unavailable_handler(self) -> None:
        self._control()
        self.manager.set_step_input_handler(None)
        result, _ = self.manager._handle_business_request(
            "control", "upper.capabilities.get", {}, None, connection_id="control")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "backend_unsupported")

    def test_trusted_context_comes_from_the_record_not_the_wire(self) -> None:
        self._control()
        self.manager.create({
            "id": "text", "name": "Text", "type": "zmq_client", "enabled": True,
            "config": {"endpoint": "tcp://127.0.0.1:8766", "protocol_profile": "astrbotex", "channel": "text"},
        })
        context = self.manager.control_trusted_context("control")
        self.assertEqual(context.robot_id, "robot-a")
        self.assertEqual(context.route_refs, ("route-1", "route-2"))
        self.assertEqual(context.ex_session, "pinned-session")
        self.assertTrue(context.ready)
        self.assertIsNone(self.manager.control_trusted_context("text"))
        self.assertIsNone(self.manager.control_trusted_context("missing"))

    def test_runtime_session_provider_overrides_the_recorded_value(self) -> None:
        self._control()
        self.manager.set_control_session_provider(lambda: "live-session")
        self.assertEqual(self.manager.control_trusted_context("control").ex_session, "live-session")
        self.manager.set_control_session_provider(lambda: "")
        self.assertEqual(self.manager.control_trusted_context("control").ex_session, "pinned-session")
        self.manager.set_control_session_provider(None)
        self.assertEqual(self.manager.control_trusted_context("control").ex_session, "pinned-session")


if __name__ == "__main__":
    unittest.main()
