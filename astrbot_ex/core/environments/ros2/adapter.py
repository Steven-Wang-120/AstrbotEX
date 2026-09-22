from __future__ import annotations

import importlib.util
import threading
import time
from typing import Any


class Ros2UnavailableError(RuntimeError):
    """ROS 2 runtime or its Python bindings are not available."""


def _enum_name(value: Any) -> str | None:
    if value is None:
        return None
    name = getattr(value, "name", None)
    if name:
        return str(name)
    return str(value)


def _qos_payload(profile: Any) -> dict[str, Any]:
    if profile is None:
        return {}
    result: dict[str, Any] = {}
    for key in (
        "history",
        "depth",
        "reliability",
        "durability",
        "lifespan",
        "deadline",
        "liveliness",
    ):
        value = getattr(profile, key, None)
        if key == "depth":
            result[key] = int(value) if isinstance(value, int) else value
        elif value is not None:
            result[key] = _enum_name(value)
    return result


class Ros2EnvironmentAdapter:
    mode = "ros2"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        self._rclpy: Any = None
        self._context: Any = None
        self._node: Any = None
        self._executor: Any = None
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._last_error: str | None = None
        self._last_graph: dict[str, Any] = {
            "nodes": [],
            "topics": [],
            "refreshed_at": None,
            "source": "ros2",
        }

    @staticmethod
    def probe() -> dict[str, Any]:
        rclpy_spec = importlib.util.find_spec("rclpy")
        runtime_spec = importlib.util.find_spec("rosidl_runtime_py")
        if rclpy_spec is None:
            return {
                "available": False,
                "reason": "rclpy is not installed in the EX runtime environment",
                "rclpy": False,
                "rosidl_runtime_py": runtime_spec is not None,
            }
        return {
            "available": True,
            "reason": None,
            "rclpy": True,
            "rosidl_runtime_py": runtime_spec is not None,
        }

    def start(self) -> None:
        if self._node is not None:
            return
        probe = self.probe()
        if not probe["available"]:
            raise Ros2UnavailableError(str(probe["reason"]))

        context = None
        rclpy = None
        try:
            import rclpy
            from rclpy.context import Context
            from rclpy.executors import SingleThreadedExecutor

            context = Context()
            domain_id = self.config.get("domain_id")
            init_args = None
            if domain_id is not None:
                init_args = ["--ros-args", "--domain-id", str(int(domain_id))]
            rclpy.init(args=init_args, context=context)
            namespace = str(self.config.get("namespace", "/astrbotex")).strip() or "/astrbotex"
            node_name = str(self.config.get("node_name", "environment")).strip() or "environment"
            node = rclpy.create_node(
                "astrbotex_" + node_name,
                namespace=namespace,
                context=context,
            )
            try:
                executor = SingleThreadedExecutor(context=context)
            except TypeError:
                executor = SingleThreadedExecutor()
            executor.add_node(node)
        except Exception:
            try:
                if context is not None and rclpy is not None:
                    rclpy.shutdown(context=context)
            except Exception:
                pass
            raise

        self._rclpy = rclpy
        self._context = context
        self._node = node
        self._executor = executor
        self._started_at = time.time()
        self._last_error = None
        self._thread = threading.Thread(
            target=self._spin,
            name="astrbotex-ros2-executor",
            daemon=True,
        )
        self._thread.start()

    def _spin(self) -> None:
        try:
            self._executor.spin()
        except Exception as exc:
            self._last_error = str(exc)

    def close(self, reason: str = "environment closed") -> None:
        del reason
        executor = self._executor
        node = self._node
        context = self._context
        rclpy = self._rclpy
        thread = self._thread
        self._executor = None
        self._node = None
        self._context = None
        self._rclpy = None
        self._thread = None
        if executor is not None:
            try:
                executor.shutdown(timeout_sec=1.0)
            except TypeError:
                try:
                    executor.shutdown()
                except Exception:
                    pass
            except Exception:
                pass
        if node is not None:
            try:
                node.destroy_node()
            except Exception:
                pass
        if rclpy is not None and context is not None:
            try:
                rclpy.shutdown(context=context)
            except Exception:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)

    def status(self) -> dict[str, Any]:
        probe = self.probe()
        active = self._node is not None
        return {
            "mode": self.mode,
            "available": bool(probe["available"]),
            "active": active,
            "health": "ok" if active else ("unavailable" if not probe["available"] else "unknown"),
            "label": "ROS 2",
            "reason": self._last_error or probe.get("reason"),
            "probe": probe,
            "node_name": self._node.get_name() if self._node is not None else None,
            "started_at": self._started_at,
            "config": {
                "domain_id": self.config.get("domain_id"),
                "namespace": self.config.get("namespace", "/astrbotex"),
                "node_name": self.config.get("node_name", "environment"),
            },
        }

    def graph(self) -> dict[str, Any]:
        node = self._node
        if node is None:
            probe = self.probe()
            return {
                **self._last_graph,
                "available": bool(probe["available"]),
                "active": False,
                "reason": probe.get("reason"),
            }
        try:
            nodes = []
            get_nodes = getattr(node, "get_node_names_and_namespaces", None)
            if callable(get_nodes):
                nodes = [
                    {"name": str(name), "namespace": str(namespace)}
                    for name, namespace in get_nodes()
                ]
            topics = []
            get_topics = getattr(node, "get_topic_names_and_types", None)
            if callable(get_topics):
                try:
                    topic_pairs = get_topics(no_demangle=False)
                except TypeError:
                    topic_pairs = get_topics()
                for topic, types in topic_pairs:
                    publishers = self._endpoint_info(node, topic, "publishers")
                    subscriptions = self._endpoint_info(node, topic, "subscriptions")
                    topics.append(
                        {
                            "name": str(topic),
                            "types": [str(item) for item in types],
                            "publishers": publishers,
                            "subscriptions": subscriptions,
                        }
                    )
            self._last_graph = {
                "nodes": nodes,
                "topics": topics,
                "refreshed_at": time.time(),
                "source": "ros2",
                "active": True,
                "available": True,
                "reason": None,
            }
        except Exception as exc:
            self._last_error = str(exc)
            return {
                **self._last_graph,
                "active": True,
                "available": True,
                "reason": self._last_error,
            }
        return dict(self._last_graph)

    @staticmethod
    def _endpoint_info(node: Any, topic: str, direction: str) -> list[dict[str, Any]]:
        method_name = (
            "get_publishers_info_by_topic"
            if direction == "publishers"
            else "get_subscriptions_info_by_topic"
        )
        method = getattr(node, method_name, None)
        if not callable(method):
            return []
        try:
            infos = method(topic, no_mangle=False)
        except TypeError:
            infos = method(topic)
        payload: list[dict[str, Any]] = []
        for info in infos:
            payload.append(
                {
                    "node_name": str(getattr(info, "node_name", "")),
                    "node_namespace": str(getattr(info, "node_namespace", "")),
                    "topic_type": str(getattr(info, "topic_type", "")),
                    "endpoint_type": _enum_name(getattr(info, "endpoint_type", None)),
                    "qos": _qos_payload(getattr(info, "qos_profile", None)),
                    "endpoint_gid": list(getattr(info, "endpoint_gid", []) or []),
                }
            )
        return payload

    def endpoints(self) -> dict[str, Any]:
        return {
            "received": [],
            "sent": [],
            "refreshed_at": time.time() if self._node is not None else None,
            "source": "ros2",
        }
