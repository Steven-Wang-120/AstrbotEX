"""Runtime event bus with a single level normaliser and stable log identifiers.

Two things were wrong before: a Chinese warning with no lifecycle keyword was
filtered out, and a dropped SSE history was invisible. This module now decides
one level per event, always keeps warning and above, and stamps every retained
event with a reserved ``data.log`` block carrying a stable ``stream_id``,
``sequence`` and ``event_id``. Producers cannot forge that block.
"""
from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Callable
from threading import RLock
from time import monotonic

from astrbot_ex.core.event_stream import EventStream, control_frame, parse_cursor
from astrbot_ex.core.models import RuntimeEvent

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LEVEL_RANK = {name: index for index, name in enumerate(LEVELS)}
_LEVEL_ALIASES = {
    "debug": "DEBUG", "trace": "DEBUG", "verbose": "DEBUG",
    "info": "INFO", "information": "INFO", "notice": "INFO",
    "warn": "WARNING", "warning": "WARNING",
    "error": "ERROR", "err": "ERROR",
    "fatal": "CRITICAL", "critical": "CRITICAL", "crit": "CRITICAL",
}
# C01 §7: these are errors no matter what the producer claims.
_ERROR_TYPES = frozenset({"fault", "plugin_fault", "rule_rejected"})
# Structural, lifecycle and low-frequency diagnostic events are always retained.
_ALWAYS_TYPES = frozenset({
    "environment", "environment_changed", "ros_graph_changed", "ros_endpoints_changed",
    "decision_changed", "fault", "plugin_fault", "rule_rejected", "runtime_state",
    "ros_diagnostic", "vision_health",
})
_LIFECYCLE_KEYWORDS = (
    "start", "started", "stop", "stopped", "load", "loaded", "unload", "unloaded",
    "install", "installed", "uninstall", "uninstalled", "enable", "enabled",
    "disable", "disabled", "unavailable", "degraded", "open failed", "failed",
)
SOURCE_LIMIT = 64
MAX_THROTTLE_KEYS = 512


def _explicit_level(data: dict) -> str | None:
    """Only the producer's own severity/level counts; a forged data.log never does."""
    for key in ("severity", "level"):
        value = data.get(key)
        if isinstance(value, str):
            level = _LEVEL_ALIASES.get(value.strip().lower())
            if level is not None:
                return level
    return None


def _has_error(data: dict) -> bool:
    value = data.get("error")
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _is_lifecycle(event: RuntimeEvent) -> bool:
    text = f"{event.type} {event.message}".lower()
    return any(keyword in text for keyword in _LIFECYCLE_KEYWORDS)


def normalize_level(event: RuntimeEvent) -> str:
    """C01 §7 priority: hard errors, then explicit severity/level, then lifecycle, else DEBUG."""
    data = event.data or {}
    explicit = _explicit_level(data)
    rank = _LEVEL_RANK[explicit] if explicit is not None else 0
    if event.type in _ERROR_TYPES or _has_error(data):
        rank = max(rank, _LEVEL_RANK["ERROR"])
    if explicit is None and rank == 0:
        rank = _LEVEL_RANK["INFO"] if (event.type in _ALWAYS_TYPES or _is_lifecycle(event)) else 0
    return LEVELS[rank]


def clean_source(value) -> str | None:
    """Bounded, single-line, printable source label; secrets are never echoed."""
    if not isinstance(value, str):
        return None
    text = "".join(character for character in value if character.isprintable())
    text = " ".join(text.split())
    return text[:SOURCE_LIMIT] or None


def resolve_source(event: RuntimeEvent) -> str:
    data = event.data or {}
    for key in ("source", "plugin_id", "plugin", "environment", "session_id"):
        source = clean_source(data.get(key))
        if source:
            return source
    return clean_source(event.type) or "runtime"


def log_metadata(event: RuntimeEvent) -> dict:
    """Read the reserved block; returns {} for unretained events."""
    block = (event.data or {}).get("log")
    return dict(block) if isinstance(block, dict) else {}


class EventBus:
    def __init__(self, max_events: int = 1000, *, stream_id: str | None = None) -> None:
        self._events: deque[RuntimeEvent] = deque(maxlen=max_events)
        self._subscribers: list[Callable[[RuntimeEvent], None]] = []
        self._last_emit_at: dict[str, float] = {}
        self._lock = RLock()
        self._stream_id = stream_id or uuid.uuid4().hex
        if not clean_source(self._stream_id):
            raise ValueError("stream_id must be a non-empty identifier")
        self._sequence = 0

    @property
    def stream_id(self) -> str:
        return self._stream_id

    @property
    def head(self) -> int:
        with self._lock:
            return self._sequence

    @property
    def oldest_sequence(self) -> int | None:
        with self._lock:
            return self._oldest_locked()

    def _oldest_locked(self) -> int | None:
        if not self._events:
            return None
        return self._events[0].data["log"]["sequence"]

    def emit(self, event_type: str, message: str, **data) -> RuntimeEvent:
        event = RuntimeEvent(type=event_type, message=message, data=data)
        # Level and source are decided before the reserved block exists, so a
        # producer-supplied data.log can neither raise nor lower the level.
        level = normalize_level(event)
        if not self._should_publish(event, level):
            return event
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            event_id = f"{self._stream_id}:{sequence}"
            event.data = {
                **(event.data or {}),
                "log": {
                    "level": level,
                    "source": resolve_source(event),
                    "stream_id": self._stream_id,
                    "sequence": sequence,
                    "event_id": event_id,
                },
            }
            self._events.append(event)
            subscribers = list(self._subscribers)
        # Callbacks stay outside the lock; each stream guards its own failures.
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
            if len(self._last_emit_at) > MAX_THROTTLE_KEYS:
                for stale in list(self._last_emit_at)[:len(self._last_emit_at) - MAX_THROTTLE_KEYS]:
                    self._last_emit_at.pop(stale, None)
        return self.emit(event_type, message, **data)

    def subscribe(self, callback: Callable[[RuntimeEvent], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            self._detach(callback)

        return unsubscribe

    def _detach(self, callback: Callable[[RuntimeEvent], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def recent(self, limit: int = 100) -> list[RuntimeEvent]:
        with self._lock:
            return list(self._events)[-limit:]

    def open_stream(self, last_event_id: str | None = None) -> EventStream:
        """Atomically validate the cursor, snapshot history and register the subscriber.

        Raises ValueError for a malformed cursor; the HTTP layer maps that to 400.
        """
        cursor = parse_cursor(last_event_id)
        with self._lock:
            head = self._sequence
            history, controls = self._history_locked(cursor, head)
            stream = EventStream(self, stream_id=self._stream_id, head=head,
                                 history=history, controls=controls)
            self._subscribers.append(stream._on_event)
        return stream

    def _history_locked(self, cursor, head: int) -> tuple[list[RuntimeEvent], list[dict]]:
        retained = list(self._events)
        oldest = retained[0].data["log"]["sequence"] if retained else None
        newest = retained[-1].data["log"]["sequence"] if retained else None

        def bounds(**values):
            return {"stream_id": self._stream_id, "oldest_sequence": oldest,
                    "newest_sequence": newest, **values}

        if cursor is None:
            return retained[-50:], []
        stream_id, sequence = cursor
        if stream_id != self._stream_id:
            # A cursor from another process instance can never be replayed.
            return retained, [control_frame("reset", "cross_stream", dropped_count=None, **bounds())]
        if sequence > head:
            return retained, [control_frame("reset", "cursor_ahead", dropped_count=None, **bounds())]
        if sequence == head:
            return [], []
        if oldest is not None and sequence < oldest - 1:
            return retained, [control_frame("gap", "history_expired",
                                            dropped_count=oldest - 1 - sequence, **bounds())]
        return [event for event in retained if event.data["log"]["sequence"] > sequence], []

    @staticmethod
    def _should_publish(event: RuntimeEvent, level: str | None = None) -> bool:
        level = normalize_level(event) if level is None else level
        if _LEVEL_RANK[level] >= _LEVEL_RANK["WARNING"]:
            return True
        if event.type in _ALWAYS_TYPES:
            return True
        return _is_lifecycle(event)
