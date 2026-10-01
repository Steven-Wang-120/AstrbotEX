"""Declared ROS ports with independent bounded queues. TopicBus is never used here."""
from __future__ import annotations
import copy
import threading
import time
import uuid
import sys
from collections import deque
from dataclasses import dataclass
from typing import Any
from .contracts import parse_ports, normalize_bindings

class RosBindingError(RuntimeError): pass
class RosUnavailableError(RosBindingError): pass

@dataclass(frozen=True, slots=True)
class RosPublishResult:
    status: str
    message: str = ""
    @property
    def accepted(self): return self.status == "queued"

@dataclass(frozen=True, slots=True)
class RosReceivedMessage:
    message: Any
    received_monotonic_ns: int
    generation: int
    binding_generation: int = 0
    size_bytes: int = 0
    stop_token: str | None = None


def message_size(message, limit):
    """Bounded estimate of retained memory without copying ROS byte arrays."""
    pending, seen, size, visited = [message], set(), 0, 0
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        size += sys.getsizeof(value)
        visited += 1
        if size > limit or visited > 4096:
            return limit + 1
        if isinstance(value, (list, tuple)):
            if len(value) + len(pending) > 4096:
                return limit + 1
            pending.extend(value)
        elif isinstance(value, dict):
            if len(value) * 2 + len(pending) > 4096:
                return limit + 1
            pending.extend(value.keys())
            pending.extend(value.values())
        elif not isinstance(value, (str, bytes, bytearray, int, float, bool, type(None))):
            slots = getattr(type(value), '__slots__', ())
            if isinstance(slots, str):
                slots = (slots,)
            pending.extend(getattr(value, name) for name in slots if hasattr(value, name))
            if hasattr(value, '__dict__'):
                pending.append(vars(value))
    return size

class _Port:
    def __init__(self, facade, declaration, binding):
        self.facade = facade
        self.declaration = declaration
        self.binding = copy.deepcopy(binding)
        self.endpoint_id = f"{facade.owner_id}:{declaration['id']}"
        self.lock = threading.RLock()
        self.queue = deque()
        self.native = None
        self.message_type = None
        self.generation = 0
        self.binding_generation = 0
        self.queue_bytes = 0
        self.in_flight = 0
        self.graph_peers = 0
        self.binding_received = 0
        self.qos_compatibility = []
        self.reason_code = None
        self._rate_at = time.monotonic()
        self._rate_count = 0
        self._rate_hz = 0.0
        self.state = "waiting_environment" if binding["enabled"] else "disabled"
        self.reason = None
        self.closed = False
        self.matched = None
        self.resolved_topic = None
        self.stats = dict.fromkeys(("rx_received", "rx_consumed", "tx_queued", "tx_published", "queue_dropped", "expired", "rejected", "error_count", "oversized", "queue_peak_bytes"), 0)
        self.last_message_at = None

    def status(self):
        with self.lock:
            now = time.monotonic()
            count = self.stats['rx_received'] if self.declaration['direction'] == 'subscribe' else self.stats['tx_published']
            if now - self._rate_at >= 0.5:
                self._rate_hz = (count - self._rate_count) / (now - self._rate_at)
                self._rate_at, self._rate_count = now, count
            return {
                "endpoint_id": self.endpoint_id, "owner_generation": self.facade.owner_id,
                "plugin_id": self.facade.plugin_id, "port_id": self.declaration["id"],
                "direction": self.declaration["direction"], "environment_generation": self.generation,
                "binding_generation": self.binding_generation,
                "requested_topic": self.binding["topic"], "resolved_topic": self.resolved_topic,
                "topic": self.resolved_topic or self.binding["topic"], "message_type": self.binding["message_type"],
                "desired_enabled": self.binding["enabled"], "resource_created": self.native is not None,
                "state": self.state, "status": self.state, "message": self.reason,
                "reason_code": self.reason_code,
                "effective_qos": copy.deepcopy(self.binding["qos"]), "local_queue_policy": dict(self.declaration["queue"]),
                "matched_peer_count": self.matched, "queue_depth": len(self.queue),
                "graph_peer_count": self.graph_peers, "qos_compatibility": copy.deepcopy(self.qos_compatibility),
                "queue_bytes": self.queue_bytes, "rate_hz": round(self._rate_hz, 2), "in_flight": self.in_flight,
                "last_message_at": self.last_message_at, **self.stats,
            }

    def unavailable(self, reason="waiting_environment"):
        with self.lock:
            self.native = self.message_type = None
            self.discard_queue()
            self.binding_generation += 1
            self.matched = None
            self.resolved_topic = None
            self.state = "closed" if self.closed else ("disabled" if not self.binding["enabled"] else reason)
            self.reason = None
            self.reason_code = None
            self.graph_peers = 0
            self.binding_received = 0
            self.qos_compatibility = []

    def receive(self, message, generation, binding_generation=None):
        with self.lock:
            if self.closed or self.native is None or self.generation != generation: return
            if binding_generation is not None and binding_generation != self.binding_generation: return
            self.stats["rx_received"] += 1
            self.binding_received += 1
            self.last_message_at = time.time()
            size = message_size(message, self.declaration['queue']['max_message_bytes'])
            self._enqueue(RosReceivedMessage(message, time.monotonic_ns(), generation, self.binding_generation, size))

    def discard_queue(self):
        with self.lock:
            self.queue.clear()
            self.queue_bytes = 0

    def _pop(self):
        item = self.queue.popleft()
        self.queue_bytes -= item.size_bytes
        return item

    def _enqueue(self, item):
        policy = self.declaration["queue"]
        if item.size_bytes > policy['max_message_bytes'] or item.size_bytes > policy['max_bytes']:
            self.stats['oversized'] += 1
            self.stats['queue_dropped'] += 1
            return False
        while len(self.queue) >= policy["capacity"] or self.queue_bytes + item.size_bytes > policy['max_bytes']:
            if policy["overflow"] == "reject_new":
                self.stats["queue_dropped"] += 1
                return False
            self._pop()
            self.stats["queue_dropped"] += 1
        self.queue.append(item)
        self.queue_bytes += item.size_bytes
        self.stats['queue_peak_bytes'] = max(self.stats['queue_peak_bytes'], self.queue_bytes)
        return True

    def expired(self, item):
        age = self.declaration["queue"]["max_age_ms"]
        return item.generation != self.generation or item.binding_generation != self.binding_generation or (age > 0 and time.monotonic_ns() - item.received_monotonic_ns > age * 1000000)

    def get_nowait(self):
        with self.lock:
            while self.queue:
                item = self._pop()
                if self.expired(item):
                    self.stats["expired"] += 1
                    continue
                self.stats["rx_consumed"] += 1
                return item
        return None

    def take_latest(self):
        with self.lock:
            if len(self.queue) > 1:
                self.stats["queue_dropped"] += len(self.queue) - 1
                last = self.queue[-1]
                self.discard_queue()
                self.queue.append(last)
                self.queue_bytes = last.size_bytes
            return self.get_nowait()

    def new_message(self):
        with self.lock:
            if self.native is None or self.closed: raise RosUnavailableError(self.reason or self.state)
            return self.message_type()

    def publish(self, message, *, stop=False):
        with self.lock:
            manager = self.facade.environment_manager
            stop_token = manager.stop_permission(self.facade) if stop and manager else None
            if stop and (not stop_token):
                self.stats['rejected'] += 1
                return RosPublishResult('stop_not_authorized')
            if self.closed or self.native is None or not manager or (not manager.accepting and not stop_token):
                return RosPublishResult("unavailable", self.reason or self.state)
            if not stop_token and self.declaration["requires_runtime_running"] and not manager.runtime_running():
                self.stats["rejected"] += 1
                self.discard_queue()
                return RosPublishResult("runtime_inactive")
            if not isinstance(message, self.message_type):
                self.stats["rejected"] += 1
                return RosPublishResult("invalid_type", self.binding["message_type"])
            size = message_size(message, self.declaration['queue']['max_message_bytes'])
            if not self._enqueue(RosReceivedMessage(message, time.monotonic_ns(), self.generation, self.binding_generation, size, stop_token)):
                self.stats["rejected"] += 1
                return RosPublishResult('rejected_size' if size > self.declaration['queue']['max_message_bytes'] else "rejected_full")
            self.stats["tx_queued"] += 1
            return RosPublishResult("queued")

    def flush_one(self):
        # Called only on this port's ROS execution lane; no TopicBus callbacks.
        with self.lock:
            manager = self.facade.environment_manager
            if self.closed or self.native is None or not manager:
                self.discard_queue()
                return
            if not self.queue: return
            item = self._pop()
            authorized_stop = bool(item.stop_token and manager.stop_token_valid(item.stop_token, self.facade.owner_id))
            if (item.stop_token and not authorized_stop) or (not item.stop_token and not manager.accepting):
                self.stats['rejected'] += 1
                return
            if self.expired(item):
                self.stats["expired"] += 1
                if item.stop_token:
                    manager.stop_failed('stop message expired before publication')
                return
            if not authorized_stop and self.declaration["requires_runtime_running"] and not manager.runtime_running():
                self.discard_queue()
                self.stats["rejected"] += 1
                return
            native = self.native
            self.in_flight += 1
        try:
            native.publish(item.message)
        except Exception as exc:
            with self.lock:
                self.stats["error_count"] += 1
                self.state, self.reason = "error", str(exc)
            if authorized_stop:
                manager.stop_failed(str(exc))
        else:
            with self.lock:
                self.stats["tx_published"] += 1
                self.last_message_at = time.time()
        finally:
            with self.lock:
                self.in_flight -= 1

    def close(self):
        self.facade._close_port(self)

class RosSubscriptionHandle:
    def __init__(self, port): self._port = port
    def get_nowait(self): return self._port.get_nowait()
    def take_latest(self): return self._port.take_latest()
    def status(self): return self._port.status()
    def close(self): self._port.close()

class RosPublisherHandle:
    def __init__(self, port): self._port = port
    def new_message(self): return self._port.new_message()
    def publish(self, message): return self._port.publish(message)
    def publish_stop(self, message): return self._port.publish(message, stop=True)
    def status(self): return self._port.status()
    def close(self): self._port.close()

class PluginRosFacade:
    def __init__(self, *, plugin_id, topic_bus=None, environment_manager=None, ports=None, bindings=None):
        self.plugin_id = plugin_id
        self.owner_id = uuid.uuid4().hex
        self.environment_manager = environment_manager
        self.declarations = parse_ports({"ports": ports or []})
        self.bindings_config = normalize_bindings(self.declarations, bindings)
        self._handles = {}
        self._lock = threading.RLock()
        self.closed = False
        self.actor = None
        self._hook_local = threading.local()
        if environment_manager: environment_manager.register_owner(self)

    def _handle(self, port_id, direction):
        manager = self.environment_manager
        # Serialize creation/reconfiguration against an environment transition.
        with (manager.resources_lock if manager else self._lock):
            with self._lock:
                if self.closed: raise RosUnavailableError("plugin owner is closed")
                port = next((p for p in self.declarations if p["id"] == port_id), None)
                if port is None: raise RosBindingError(f"undeclared ROS port: {port_id}")
                if port["direction"] != direction: raise RosBindingError(f"{port_id}: incorrect direction")
                existing = self._handles.get(port_id)
                if existing: return existing
                endpoint = _Port(self, port, self.bindings_config[port_id])
                handle = RosSubscriptionHandle(endpoint) if direction == "subscribe" else RosPublisherHandle(endpoint)
                self._handles[port_id] = handle
            if manager: manager.attach_port(endpoint)
            return handle

    def subscribe(self, port_id): return self._handle(port_id, "subscribe")
    def publisher(self, port_id): return self._handle(port_id, "publish")
    def ports(self):
        with self._lock: return [h._port for h in self._handles.values()]
    def status(self): return {"owner_id": self.owner_id, "closed": self.closed, "endpoints": [p.status() for p in self.ports()]}

    def reconfigure(self, bindings):
        config = normalize_bindings(self.declarations, bindings)
        manager = self.environment_manager
        with (manager.resources_lock if manager else self._lock):
            self.validate_reconfigure(config)
            for port in self.ports():
                new = config[port.declaration["id"]]
                if new == port.binding: continue
                if manager: manager.detach_port(port)
                port.unavailable()
                port.binding = copy.deepcopy(new)
                if manager: manager.attach_port(port)
            self.bindings_config = config

    def validate_reconfigure(self, config):
        manager = self.environment_manager
        if manager is None:
            return
        from .models import EnvironmentBusyError
        if manager.snapshot()['phase'] in ('starting', 'stopping'):
            raise EnvironmentBusyError('environment switch is in progress')
        if manager.runtime_running() and any(p.declaration['requires_runtime_running'] and
                p.native is not None and config[p.declaration['id']] != p.binding for p in self.ports()):
            raise EnvironmentBusyError('stop the ROS control task before changing its binding')

    def _close_port(self, port):
        manager = self.environment_manager
        with (manager.resources_lock if manager else self._lock):
            if manager: manager.detach_port(port)
            with port.lock:
                port.closed = True
                port.unavailable()
            with self._lock:
                self._handles.pop(port.declaration["id"], None)

    def close(self):
        if self.closed: return
        self.closed = True
        errors = []
        for port in self.ports():
            try:
                self._close_port(port)
            except Exception as exc:
                errors.append(str(exc))
        if not errors and self.environment_manager:
            self.environment_manager.unregister_owner(self)
        if errors:
            self.closed = False
            raise RosUnavailableError('; '.join(errors))
