from __future__ import annotations

from collections import deque
from collections.abc import Callable

from astrbot_ex.core.models import RuntimeEvent


class EventBus:
    def __init__(self, max_events: int = 1000) -> None:
        self._events: deque[RuntimeEvent] = deque(maxlen=max_events)
        self._subscribers: list[Callable[[RuntimeEvent], None]] = []

    def emit(self, event_type: str, message: str, **data) -> RuntimeEvent:
        event = RuntimeEvent(type=event_type, message=message, data=data)
        self._events.append(event)
        for subscriber in list(self._subscribers):
            subscriber(event)
        return event

    def subscribe(self, callback: Callable[[RuntimeEvent], None]) -> Callable[[], None]:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def recent(self, limit: int = 100) -> list[RuntimeEvent]:
        return list(self._events)[-limit:]
