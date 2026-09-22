from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Any

from astrbot_ex.core.topic_bus import TopicBus, TopicInbox


class RosBindingError(RuntimeError):
    """Raised when a plugin ROS binding cannot be created or used."""


@dataclass(slots=True)
class RosBindingState:
    plugin_id: str
    direction: str
    topic: str
    type_name: str
    qos: dict[str, Any]
    status: str
    error: str | None = None


class RosPublisherHandle:
    def __init__(self, facade: "PluginRosFacade", state: RosBindingState) -> None:
        self._facade = facade
        self.state = state
        self._closed = False

    def publish(self, payload: dict[str, Any], *, source: str | None = None) -> None:
        if self._closed:
            raise RosBindingError("publisher is closed")
        self._facade._publish(self.state, payload, source=source)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._facade._close_binding(self.state)


class RosSubscriptionHandle:
    def __init__(self, facade: "PluginRosFacade", state: RosBindingState, inbox: TopicInbox) -> None:
        self._facade = facade
        self.state = state
        self._inbox = inbox
        self._closed = False

    def get(self, timeout: float | None = None):
        return self._inbox.get(timeout=timeout)

    def get_nowait(self):
        return self._inbox.get_nowait()

    def take_latest(self):
        return self._inbox.take_latest()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._inbox.close()
            self._facade._close_binding(self.state)


class PluginRosFacade:
    """Stable plugin-facing ROS API backed by the EX TopicBus.

    The TopicBus remains the in-process delivery path in every environment. The
    active environment adapter can later attach a native ROS publisher or
    subscription to the same binding without changing plugin code.
    """

    def __init__(self, *, plugin_id: str, topic_bus: TopicBus, environment_manager: Any = None) -> None:
        self.plugin_id = plugin_id
        self.topic_bus = topic_bus
        self.environment_manager = environment_manager
        self._bindings: dict[int, RosBindingState] = {}
        self._next_binding = 1
        self._lock = RLock()

    def subscribe(
        self,
        topic: str,
        *,
        type_name: str = "",
        qos: dict[str, Any] | None = None,
        max_messages: int = 1,
    ) -> RosSubscriptionHandle:
        state = self._new_state("subscription", topic, type_name, qos)
        inbox = self.topic_bus.subscribe_inbox(state.topic, max_messages=max_messages)
        self._register(state)
        return RosSubscriptionHandle(self, state, inbox)

    def publisher(
        self,
        topic: str,
        *,
        type_name: str = "",
        qos: dict[str, Any] | None = None,
    ) -> RosPublisherHandle:
        state = self._new_state("publisher", topic, type_name, qos)
        self._register(state)
        return RosPublisherHandle(self, state)

    def bindings(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._state_payload(item) for item in self._bindings.values()]

    def _publish(self, state: RosBindingState, payload: dict[str, Any], *, source: str | None) -> None:
        if not isinstance(payload, dict):
            raise RosBindingError("ROS payload must be an object")
        self.topic_bus.publish_payload(
            state.topic,
            timestamp=__import__("time").time(),
            source=source or f"plugin:{self.plugin_id}",
            payload=payload,
        )

    def _new_state(
        self,
        direction: str,
        topic: str,
        type_name: str,
        qos: dict[str, Any] | None,
    ) -> RosBindingState:
        normalized_topic = str(topic or "").strip()
        if not normalized_topic:
            raise RosBindingError("ROS topic is required")
        if not normalized_topic.startswith("/"):
            normalized_topic = "/" + normalized_topic
        return RosBindingState(
            plugin_id=self.plugin_id,
            direction=direction,
            topic=normalized_topic,
            type_name=str(type_name or "").strip(),
            qos=dict(qos or {}),
            status="ready",
        )

    def _register(self, state: RosBindingState) -> None:
        with self._lock:
            binding_id = self._next_binding
            self._next_binding += 1
            self._bindings[binding_id] = state

    def _close_binding(self, state: RosBindingState) -> None:
        with self._lock:
            for binding_id, item in list(self._bindings.items()):
                if item is state:
                    self._bindings.pop(binding_id, None)

    @staticmethod
    def _state_payload(state: RosBindingState) -> dict[str, Any]:
        return {
            "plugin_id": state.plugin_id,
            "direction": state.direction,
            "topic": state.topic,
            "type_name": state.type_name,
            "qos": dict(state.qos),
            "status": state.status,
            "error": state.error,
        }