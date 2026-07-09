from __future__ import annotations

import argparse
import json
import socket
import time
from typing import Any

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_msgs.msg import String as RosString


class Ros2ToIpcBridge(Node):
    def __init__(self, topic: str, socket_path: str, reconnect_sec: float) -> None:
        super().__init__("astrbotex_ros2_to_ipc_bridge")
        self._socket_path = socket_path
        self._reconnect_sec = reconnect_sec
        self._sock: socket.socket | None = None
        self.create_subscription(RosString, topic, self._on_string, 10)
        self.get_logger().info(f"subscribed {topic}, forwarding to unix socket {socket_path}")

    def destroy_node(self) -> bool:
        self._close_socket()
        return super().destroy_node()

    def _on_string(self, msg: RosString) -> None:
        try:
            payload = json.loads(msg.data)
            if not isinstance(payload, dict):
                raise ValueError("std_msgs/String payload must be a JSON object")
            self._send_payload(payload)
        except Exception as exc:
            self.get_logger().error(f"drop invalid vision payload: {exc}")

    def _send_payload(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")
        for attempt in range(2):
            try:
                sock = self._connect()
                sock.sendall(data)
                return
            except OSError as exc:
                self._close_socket()
                if attempt == 0:
                    time.sleep(self._reconnect_sec)
                    continue
                self.get_logger().error(f"failed to forward vision payload: {exc}")

    def _connect(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self._socket_path)
        self._sock = sock
        self.get_logger().info("connected to AstrBotEX vision IPC socket")
        return sock

    def _close_socket(self) -> None:
        if self._sock is None:
            return
        try:
            self._sock.close()
        finally:
            self._sock = None


def main() -> None:
    parser = argparse.ArgumentParser(description="Forward ROS2 std_msgs/String JSON vision topic to AstrBotEX Unix Socket IPC.")
    parser.add_argument("--topic", default="/astrbotex/vision_target", help="ROS2 std_msgs/String topic to subscribe.")
    parser.add_argument(
        "--socket-path",
        default="/home/orangepi/astrbotex_deploy/deploy/astrbotex/data/ipc/astrbotex_vision.sock",
        help="Host-visible Unix socket path created by the AstrBotEX plugin.",
    )
    parser.add_argument("--reconnect-sec", type=float, default=0.2, help="Delay before one reconnect retry.")
    args = parser.parse_args()

    rclpy.init()
    node = Ros2ToIpcBridge(args.topic, args.socket_path, args.reconnect_sec)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
