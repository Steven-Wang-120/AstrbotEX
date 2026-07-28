from __future__ import annotations

from collections import deque
from collections.abc import Callable
from threading import RLock
from time import monotonic

from astrbot_ex.core.models import RuntimeEvent


class EventBus:
    def __init__(self, max_events: int = 1000) -> None:
        self._events: deque[RuntimeEvent] = deque(maxlen=max_events)
        self._subscribers: list[Callable[[RuntimeEvent], None]] = []
        self._last_emit_at: dict[str, float] = {}
        self._lock = RLock()

    def emit(self, event_type: str, message: str, **data) -> RuntimeEvent:
        event = RuntimeEvent(type=event_type, message=message, data=data)
        if not self._should_publish(event):
            return event
        with self._lock:
            self._events.append(event)
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber(event)
        return event

    def emit_throttled(
        self,
        event_type: str,
        message: str,
        *,
        interval_sec: float = 1.0,
        key: str | None = None,
        **data,
    ) -> RuntimeEvent | None:
        throttle_key = key or f"{event_type}:{message}"
        now = monotonic()
        with self._lock:
            last_emit_at = self._last_emit_at.get(throttle_key)
            if last_emit_at is not None and now - last_emit_at < interval_sec:
                return None
            self._last_emit_at[throttle_key] = now
        return self.emit(event_type, message, **data)

    def subscribe(self, callback: Callable[[RuntimeEvent], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def recent(self, limit: int = 100) -> list[RuntimeEvent]:
        with self._lock:
            return list(self._events)[-limit:]

    @staticmethod
    def _should_publish(event: RuntimeEvent) -> bool:
        if event.type in {"fault", "plugin_fault", "runtime_state", "rule_rejected"}:
            return True

        data = event.data or {}
        severity = str(data.get("severity", "")).lower()
        if severity in {"error", "fatal"} or data.get("error"):
            return True

        text = f"{event.type} {event.message}".lower()
        lifecycle_keywords = (
            "start",
            "started",
            "stop",
            "stopped",
            "load",
            "loaded",
            "unload",
            "unloaded",
            "install",
            "installed",
            "uninstall",
            "uninstalled",
            "enable",
            "enabled",
            "disable",
            "disabled",
            "open failed",
            "failed",
        )
        return any(keyword in text for keyword in lifecycle_keywords)
