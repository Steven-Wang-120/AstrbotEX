from __future__ import annotations

from collections import deque
from collections.abc import Callable
from time import monotonic

from astrbot_ex.core.models import RuntimeEvent


class EventBus:
    def __init__(self, max_events: int = 1000) -> None:
        self._events: deque[RuntimeEvent] = deque(maxlen=max_events)
        self._subscribers: list[Callable[[RuntimeEvent], None]] = []
        self._last_emit_at: dict[str, float] = {}

    def emit(self, event_type: str, message: str, **data) -> RuntimeEvent:
        event = RuntimeEvent(type=event_type, message=message, data=data)
        self._events.append(event)
        for subscriber in list(self._subscribers):
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
        last_emit_at = self._last_emit_at.get(throttle_key)
        if last_emit_at is not None and now - last_emit_at < interval_sec:
            return None
        self._last_emit_at[throttle_key] = now
        return self.emit(event_type, message, **data)

    def subscribe(self, callback: Callable[[RuntimeEvent], None]) -> Callable[[], None]:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def recent(self, limit: int = 100) -> list[RuntimeEvent]:
        return list(self._events)[-limit:]
