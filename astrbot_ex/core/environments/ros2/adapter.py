"""Native ROS lifecycle. Only this module's start/attach paths import rclpy."""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import os
import sys
import threading
import time
import json
from contextlib import contextmanager
from typing import Any
from .interfaces import load_message, check_message, installed_interfaces
from ..contracts import environment_config

class Ros2UnavailableError(RuntimeError): pass


@contextmanager
def _lane_lock(lane):
    if not lane.lock.acquire(timeout=2.0):
        raise Ros2UnavailableError('ROS execution lane is busy beyond deadline')
    try:
        yield
    finally:
        lane.lock.release()


def qos_diagnostics(port, peers, local_qos=None):
    """Use the selected ROS distribution's native compatibility rules."""
    from rclpy.qos import qos_check_compatible, QoSCompatibility
    local = local_qos if local_qos is not None else _qos_profile(port.binding['qos'])
    results = []
    for peer in peers:
        if peer.topic_type != port.binding['message_type']:
            results.append({'node': peer.node_name, 'state': 'incompatible_type', 'reason': peer.topic_type})
            continue
        publisher, subscriber = (peer.qos_profile, local) if port.declaration['direction'] == 'subscribe' else (local, peer.qos_profile)
        try:
            compatibility, reason = qos_check_compatible(publisher, subscriber)
            state = 'compatible' if compatibility == QoSCompatibility.OK else ('incompatible' if compatibility == QoSCompatibility.ERROR else 'unknown')
        except Exception as exc:
            state, reason = 'unknown', str(exc)
        results.append({'node': peer.node_name, 'state': state, 'reason': reason})
    return results

def _enum_name(value):
    return getattr(value, "name", str(value)) if value is not None else None

def _qos_payload(profile):
    if profile is None: return {}
    return {name: (getattr(profile, name, None) if name == "depth" else _enum_name(getattr(profile, name, None)))
            for name in ("depth", "history", "reliability", "durability")}

def _qos_profile(config):
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
    return QoSProfile(depth=config["depth"], history=HistoryPolicy.KEEP_LAST,
                      reliability=getattr(ReliabilityPolicy, config["reliability"].upper()),
                      durability=getattr(DurabilityPolicy, config["durability"].upper()))

class _Lane:
    def __init__(self, context, name, failure):
        from rclpy.executors import SingleThreadedExecutor
        self.executor = SingleThreadedExecutor(context=context)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.ports = {}
        self.failure = failure
        self.thread = threading.Thread(target=self.run, name="ex-ros-" + name, daemon=True)
        self.thread.start()

    def run(self):
        try:
            while not self.stop.is_set():
                with self.lock:
                    self.executor.spin_once(timeout_sec=0.02)
                    for port in list(self.ports.values()):
                        if port.declaration["direction"] == "publish": port.flush_one()
                # Let endpoint lifecycle work acquire the lane between spins.
                self.stop.wait(0.001)
        except Exception as exc:
            if not self.stop.is_set(): self.failure(str(exc))

class Ros2EnvironmentAdapter:
    mode = "ros2"
    def __init__(self, config=None):
        self.config = environment_config(config or {})
        self._context = self._node = self._rclpy = None
        self._lanes = {}
        self._nodes = {}
        self._ports = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._refresh = threading.Event()
        self._discovery = None
        self._started_at = None
        self._last_error = None
        self._lane_error = None
        self._on_change = None
        self._graph_signature = None
        self._last_statistics_at = 0.0
        self._checks = {}
        self._graph = {"nodes": [], "topics": [], "refreshed_at": None, "source": "ros2", "active": False}

    @staticmethod
    def probe():
        def exists(name):
            try: return importlib.util.find_spec(name) is not None
            except (ValueError, ImportError): return False
        libs = {key: exists(key) for key in ("rclpy", "rosidl_runtime_py")}
        available = all(libs.values())
        return {"available": available, **libs,
                "reason": None if available else "运行环境缺少 ROS 2 Python 库；需使用预装 ROS 的镜像并重启容器",
                "ros_distro": os.getenv("ROS_DISTRO"), "rmw": os.getenv("RMW_IMPLEMENTATION"),
                "python": sys.version.split()[0], "overlay": os.getenv("ASTRBOTEX_ROS_OVERLAY")}

    def _failed(self, reason):
        self._lane_error = reason
        self._notify('environment_changed')

    def set_change_callback(self, callback):
        self._on_change = callback

    def _notify(self, kind):
        if self._on_change:
            try:
                self._on_change(kind)
            except Exception:
                pass

    def start(self):
        if self._context is not None: return
        if not self.probe()["available"]: raise Ros2UnavailableError(self.probe()["reason"])
        try:
            import rclpy
            from rclpy.context import Context
            self._rclpy = rclpy
            self._context = Context()
            rclpy.init(args=[], context=self._context, domain_id=self.config["domain_id"])
            self._node = rclpy.create_node(self.config["node_name"], namespace=self.config["namespace"],
                                           context=self._context, enable_rosout=False)
            lane = self._lane("sensor")
            with _lane_lock(lane): lane.executor.add_node(self._node)
            self._started_at = time.time()
            self._stop.clear()
            self._discovery = threading.Thread(target=self._discover, name="ex-ros-discovery", daemon=True)
            self._discovery.start()
        except Exception:
            self.close("failed start")
            raise

    def _lane(self, name):
        if name not in self._lanes: self._lanes[name] = _Lane(self._context, name, self._failed)
        return self._lanes[name]

    def attach(self, port, generation):
        with self._lock:
            if self._context is None or self._stop.is_set() or port.closed: return
            if not port.binding["enabled"]:
                port.unavailable()
                return
            binding = port.binding
            try:
                cls = load_message(binding["message_type"])
                qos = _qos_profile(binding["qos"])
                from rclpy.validate_topic_name import validate_topic_name
                validate_topic_name(binding["topic"])
                lane_name = port.declaration["execution_lane"]
                lane = self._lane(lane_name)
                key = (port.facade.owner_id, lane_name)
                with _lane_lock(lane):
                    node = self._nodes.get(key)
                    if node is None:
                        suffix = hashlib.sha256((port.facade.plugin_id + port.facade.owner_id + lane_name).encode()).hexdigest()[:16]
                        node = self._rclpy.create_node("ex_" + suffix, namespace=self.config["namespace"],
                                                       context=self._context, enable_rosout=False)
                        lane.executor.add_node(node)
                        self._nodes[key] = node
                    with port.lock:
                        port.generation = generation
                        port.binding_generation += 1
                        epoch = port.binding_generation
                        if port.declaration["direction"] == "subscribe":
                            native = node.create_subscription(cls, binding["topic"],
                                lambda message, p=port, g=generation, e=epoch: p.receive(message, g, e), qos)
                        else:
                            native = node.create_publisher(cls, binding["topic"], qos)
                        port.native, port.message_type = native, cls
                        port.resolved_topic = native.topic_name
                        port.state, port.reason = "waiting_peer", None
                    self._ports[port.endpoint_id] = (port, node, lane)
                    lane.ports[port.endpoint_id] = port
                self._refresh.set()
            except Exception as exc:
                with port.lock:
                    port.state = "missing_interface" if isinstance(exc, (ImportError, AttributeError)) else "error"
                    port.reason = str(exc)
                    port.reason_code = port.state
                    port.stats["error_count"] += 1

    def detach(self, port):
        with self._lock:
            entry = self._ports.get(port.endpoint_id)
            if entry:
                _, node, lane = entry
                with _lane_lock(lane):
                    lane.ports.pop(port.endpoint_id, None)
                    native = port.native
                    port.unavailable()
                    if native is not None:
                        if port.declaration["direction"] == "subscribe": node.destroy_subscription(native)
                        else: node.destroy_publisher(native)
                    self._ports.pop(port.endpoint_id, None)
                    key = (port.facade.owner_id, port.declaration["execution_lane"])
                    if not any(v[1] is node for v in self._ports.values()):
                        lane.executor.remove_node(node)
                        node.destroy_node()
                        self._nodes.pop(key, None)
            else:
                port.unavailable()
            self._refresh.set()

    def close(self, reason="environment closed"):
        self._stop.set()
        self._refresh.set()
        for lane in self._lanes.values():
            lane.stop.set()
            lane.executor.wake()
        if self._discovery and self._discovery is not threading.current_thread():
            self._discovery.join(timeout=2.0)
            if self._discovery.is_alive(): raise Ros2UnavailableError("discovery did not stop before deadline")
        for lane in self._lanes.values():
            lane.thread.join(timeout=2.0)
            if lane.thread.is_alive(): raise Ros2UnavailableError("ROS execution lane did not stop before deadline")
        errors = []
        for port, _, _ in list(self._ports.values()):
            try: self.detach(port)
            except Exception as exc: errors.append(str(exc))
        for node in list(self._nodes.values()):
            try: node.destroy_node()
            except Exception as exc: errors.append(str(exc))
        self._nodes.clear()
        if self._node is not None:
            try: self._node.destroy_node()
            except Exception as exc: errors.append(str(exc))
        for lane in self._lanes.values():
            try: lane.executor.shutdown(timeout_sec=1.0)
            except Exception as exc: errors.append(str(exc))
        if self._context is not None:
            try:
                if self._context.ok(): self._rclpy.shutdown(context=self._context)
            except Exception as exc: errors.append(str(exc))
        self._lanes.clear()
        self._context = self._node = None
        self._graph = {"nodes": [], "topics": [], "refreshed_at": None, "source": "ros2", "active": False}
        if errors: raise Ros2UnavailableError("; ".join(errors))

    def status(self):
        active = self._context is not None and not self._stop.is_set()
        endpoint_error = next((p.reason for p, _, _ in list(self._ports.values()) if p.state == 'error'), None)
        reason = self._lane_error or self._last_error or endpoint_error
        return {"mode": "ros2", "active": active, "available": self.probe()["available"],
                "health": "degraded" if reason else ("ok" if active else "unknown"),
                "reason": reason, "node_name": self._node.get_name() if self._node else None,
                "started_at": self._started_at, "config": copy.deepcopy(self.config), "probe": self.probe()}

    def _endpoint_info(self, topic, publishers):
        method = self._node.get_publishers_info_by_topic if publishers else self._node.get_subscriptions_info_by_topic
        return [{"node_name": i.node_name, "node_namespace": i.node_namespace, "topic_type": i.topic_type,
                 "endpoint_gid": list(i.endpoint_gid), "qos": _qos_payload(i.qos_profile)}
                for i in method(topic)]

    def _discover(self):
        while not self._stop.is_set():
            try:
                with self._lock:
                    if self._stop.is_set(): return
                    nodes = [{"name": name, "namespace": ns} for name, ns in self._node.get_node_names_and_namespaces()]
                    topics = []
                    for name, types in self._node.get_topic_names_and_types():
                        if not self.config["include_hidden_topics"] and (name in ("/rosout", "/parameter_events") or any(p.startswith("_") for p in name.split("/") if p)): continue
                        topics.append({"name": name, "types": list(types), "multiple_types": len(types) > 1,
                                       "publishers": self._endpoint_info(name, True), "subscriptions": self._endpoint_info(name, False)})
                    nodes.sort(key=lambda item: (item['namespace'], item['name']))
                    topics.sort(key=lambda item: item['name'])
                    signature = json.dumps([nodes, topics], sort_keys=True)
                    graph_changed = signature != self._graph_signature
                    self._graph_signature = signature
                    self._graph = {"nodes": nodes, "topics": topics, "refreshed_at": time.time(), "source": "ros2", "active": True}
                    entries = list(self._ports.values())
                    for port, node, lane in entries:
                        try:
                            native = port.native
                            if native is None: continue
                            subscribe = port.declaration['direction'] == 'subscribe'
                            method = getattr(native, 'get_publisher_count' if subscribe else 'get_subscription_count', None)
                            try:
                                count = method() if method else None
                            except (AttributeError, NotImplementedError):
                                count = None
                            peer_method = self._node.get_publishers_info_by_topic if subscribe else self._node.get_subscriptions_info_by_topic
                            peers = peer_method(native.topic_name)
                            # Humble has no native get_actual_qos(). Read our own
                            # graph endpoint to resolve system-default durations.
                            local_method = self._node.get_subscriptions_info_by_topic if subscribe else self._node.get_publishers_info_by_topic
                            requested = _qos_profile(port.binding['qos'])
                            local_info = next((info for info in local_method(native.topic_name)
                                if info.node_name == node.get_name() and info.node_namespace == node.get_namespace()
                                and info.topic_type == port.binding['message_type']
                                and all(getattr(info.qos_profile, key) == getattr(requested, key)
                                        for key in ('reliability', 'durability', 'history', 'depth'))), None)
                            diagnostics = qos_diagnostics(port, peers, local_info.qos_profile if local_info else None)
                            with port.lock:
                                port.matched = count
                                port.graph_peers = len(peers)
                                port.qos_compatibility = diagnostics
                                if port.state != 'error':
                                    observed_receive = count is None and port.binding_received > 0 and bool(peers)
                                    communicating = bool(count) or observed_receive
                                    incompatible = bool(diagnostics) and all(d['state'] in ('incompatible', 'incompatible_type') for d in diagnostics)
                                    # Some RMWs encode infinite durations differently.
                                    # Native matching / actual delivery is stronger evidence.
                                    if communicating and incompatible:
                                        for diagnostic in diagnostics:
                                            diagnostic['state'] = 'unknown'
                                            diagnostic['reason'] = 'Native match or delivery confirmed; static QoS comparison differs: ' + diagnostic['reason']
                                    incompatible = incompatible and not communicating
                                    port.state = 'ready' if communicating else ('qos_incompatible' if incompatible else 'waiting_peer')
                                    port.reason_code = port.state if incompatible else None
                                    port.reason = '; '.join(d['reason'] for d in diagnostics if d['reason']) if incompatible else None
                        except Exception:
                            port.matched = None
                self._last_error = None
                if graph_changed:
                    self._notify('ros_graph_changed')
                now = time.monotonic()
                if now - self._last_statistics_at >= self.config['statistics_interval_sec']:
                    self._last_statistics_at = now
                    self._notify('ros_endpoints_changed')
            except Exception as exc:
                self._last_error = str(exc)
            self._refresh.wait(self.config["discovery_interval_sec"])
            self._refresh.clear()

    def graph(self):
        with self._lock: return copy.deepcopy({**self._graph, "reason": self._lane_error or self._last_error})
    def refresh(self):
        self._refresh.set()
        return {"ok": True, "accepted": True}
    def check_interface(self, message_type):
        result = check_message(message_type)
        with self._lock:
            self._checks[message_type] = result
            while len(self._checks) > 128: self._checks.pop(next(iter(self._checks)))
        return result
    def interfaces(self):
        types = sorted({kind for topic in self.graph()['topics'] for kind in topic['types']})
        discovered = [check_message(kind) for kind in types]
        with self._lock:
            checks = list(self._checks.values())
        return {"packages": installed_interfaces(), "checks": checks, 'discovered_types': discovered,
                "restart_required_for_new_packages": True}
