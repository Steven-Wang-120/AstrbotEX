"""Bounded, ordered log stream over the EventBus ring.

The frame shape is frozen by C01 §7::

    {"kind": "event",   "event": RuntimeEvent, "event_id": str}
    {"kind": "control", "control": dict}

Ordering never trusts thread scheduling. Every retained event carries an
authoritative ``sequence``; this stream buffers by sequence and only releases
the next expected one. Out-of-order callbacks are reordered, duplicate ids are
ignored, and a hole that survives the reorder grace is reported as a visible
``gap`` control frame instead of silently disappearing.
"""
from __future__ import annotations

import json
import re
import threading
from collections import deque
from time import monotonic
from typing import TYPE_CHECKING, Any

from astrbot_ex.core.models import RuntimeEvent

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from astrbot_ex.core.event_bus import EventBus

CURSOR_MAX_LENGTH = 128
MAX_LIVE_FRAMES = 200
MAX_LIVE_BYTES = 2 * 1024 * 1024
REORDER_GRACE_SEC = 0.25
PROJECTION_BUDGET = 64 * 1024
DETAIL_LIMIT = 16 * 1024
VALUE_LIMIT = 4 * 1024
TRUNCATION_MARK = "…[truncated]"
_SEQUENCE = re.compile(r"[0-9]+\Z")
_STREAM_ID = re.compile(r"[A-Za-z0-9_.:-]{1,120}\Z")
_GAP_KINDS = ("gap", "reset")


def parse_cursor(value: Any) -> tuple[str, int] | None:
    """Strict ``<stream_id>:<positive sequence>`` parsing; ValueError means HTTP 400."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("event cursor must be a string")
    text = value.strip()
    if not text:
        return None
    if len(text) > CURSOR_MAX_LENGTH:
        raise ValueError("event cursor is too long")
    stream_id, separator, sequence = text.rpartition(":")
    if not separator or not _STREAM_ID.match(stream_id) or not _SEQUENCE.match(sequence):
        raise ValueError("malformed event cursor")
    number = int(sequence)
    if number < 1:
        raise ValueError("event cursor sequence must be positive")
    return stream_id, number


def control_frame(kind: str, reason: str, *, stream_id: str | None,
                  oldest_sequence: int | None, newest_sequence: int | None,
                  dropped_count: int | None) -> dict:
    """Fixed C01 §7 control payload; an unknown drop count stays null."""
    if kind not in _GAP_KINDS:
        raise ValueError(f"unknown control kind: {kind}")
    return {
        "kind": kind,
        "reason": reason,
        "stream_id": stream_id,
        "oldest_sequence": oldest_sequence,
        "newest_sequence": newest_sequence,
        "dropped_count": dropped_count,
    }


def event_id_of(event: RuntimeEvent) -> str:
    block = (event.data or {}).get("log")
    if isinstance(block, dict):
        value = block.get("event_id")
        if isinstance(value, str):
            return value
    return ""


def event_frame(event: RuntimeEvent, event_id: str | None = None) -> dict:
    return {"kind": "event", "event": event, "event_id": event_id_of(event) if event_id is None else event_id}


def estimate_frame_bytes(event: RuntimeEvent, cap: int = PROJECTION_BUDGET) -> int:
    """Cheap bounded size estimate used only for the live queue budget."""
    size = 256 + len(event.type or "") + len(event.message or "")
    for key, value in (event.data or {}).items():
        size += len(str(key)) + 24
        if isinstance(value, str):
            size += len(value)
        elif isinstance(value, (bytes, bytearray)):
            size += min(len(value), VALUE_LIMIT)
        else:
            size += 24
        if size > cap:
            return cap
    return size


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + TRUNCATION_MARK


def _project_value(value: Any, depth: int) -> Any:
    if isinstance(value, str):
        return _clip(value, VALUE_LIMIT)
    if isinstance(value, (bytes, bytearray)):
        return _clip(bytes(value[:VALUE_LIMIT]).decode("utf-8", "replace"), VALUE_LIMIT)
    if depth <= 0:
        return TRUNCATION_MARK
    if isinstance(value, dict):
        items = list(value.items())[:64]
        projected = {str(key): _project_value(item, depth - 1) for key, item in items}
        if len(value) > len(items):
            projected[TRUNCATION_MARK] = len(value) - len(items)
        return projected
    if isinstance(value, (list, tuple)):
        items = list(value[:64])
        projected = [_project_value(item, depth - 1) for item in items]
        if len(value) > len(items):
            projected.append(f"{TRUNCATION_MARK} {len(value) - len(items)} more")
        return projected
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _clip(str(value), VALUE_LIMIT)


def project_event(event: RuntimeEvent, budget: int = PROJECTION_BUDGET) -> dict:
    """Bounded display projection for one transport frame.

    The retained event is never modified and the ``event_id`` is copied verbatim,
    so redaction or truncation can never change stream identity.
    """
    block = (event.data or {}).get("log")
    projected = {
        "type": _clip(str(event.type), VALUE_LIMIT),
        "message": _clip(str(event.message), DETAIL_LIMIT),
        "timestamp": event.timestamp,
        "data": {str(key): _project_value(value, 3)
                 for key, value in list((event.data or {}).items())[:64]},
        "event_id": event_id_of(event),
    }
    if isinstance(block, dict):
        projected["log"] = {key: block.get(key) for key in
                            ("level", "source", "stream_id", "sequence", "event_id")}
    if len(json.dumps(projected, ensure_ascii=False, default=str)) <= budget:
        return projected
    # Still too large: keep the envelope and replace the payload with a marker.
    projected["data"] = {"truncated": TRUNCATION_MARK,
                         "original_keys": len(event.data or {})}
    return projected


class EventStream:
    """Live tail of one EventBus, bounded in both frames and estimated bytes."""

    def __init__(self, bus: "EventBus", *, stream_id: str, head: int,
                 history: list[RuntimeEvent], controls: list[dict],
                 max_frames: int = MAX_LIVE_FRAMES, max_bytes: int = MAX_LIVE_BYTES,
                 reorder_grace: float = REORDER_GRACE_SEC) -> None:
        self.bus = bus
        self.stream_id = stream_id
        self.head = head
        self.max_frames = max_frames
        self.max_bytes = max_bytes
        self.reorder_grace = reorder_grace
        self._history = [event_frame(event) for event in history]
        self._controls: deque[dict] = deque(controls)
        self._buffer: dict[int, dict] = {}
        self._sizes: dict[int, int] = {}
        self._bytes = 0
        self._next = head + 1
        self._dropped_through = 0
        self._dropped_total = 0
        self._hole_since: float | None = None
        self._closed = False
        self._condition = threading.Condition()

    # --- public surface ---------------------------------------------------

    def recent(self) -> list[dict]:
        """History frames captured atomically with the subscription."""
        return [{"kind": frame["kind"], "event": frame["event"], "event_id": frame["event_id"]}
                for frame in self._history]

    def get(self, timeout: float = 0.0) -> dict | None:
        """Next frame in sequence order, or None on timeout/close."""
        deadline = monotonic() + max(0.0, float(timeout))
        with self._condition:
            while True:
                frame = self._take_locked()
                if frame is not None:
                    return frame
                if self._closed:
                    return None
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(min(remaining, self.reorder_grace))

    def close(self) -> None:
        """Idempotent; detaches outside the condition to keep a single lock order."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._buffer.clear()
            self._sizes.clear()
            self._bytes = 0
            self._condition.notify_all()
        self.bus._detach(self._on_event)

    @property
    def closed(self) -> bool:
        return self._closed

    def stats(self) -> dict:
        with self._condition:
            return {"buffered": len(self._buffer), "buffered_bytes": self._bytes,
                    "dropped": self._dropped_total, "next_sequence": self._next,
                    "closed": self._closed}

    # --- bus callback -----------------------------------------------------

    def _on_event(self, event: RuntimeEvent) -> None:
        """Called from emit on the producer thread: bounded copy and enqueue only."""
        try:
            block = (event.data or {}).get("log")
            sequence = block.get("sequence") if isinstance(block, dict) else None
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                return
            with self._condition:
                if self._closed or sequence <= self.head or sequence in self._buffer:
                    return
                self._buffer[sequence] = event_frame(event)
                self._sizes[sequence] = estimate_frame_bytes(event)
                self._bytes += self._sizes[sequence]
                while len(self._buffer) > self.max_frames or self._bytes > self.max_bytes:
                    oldest = min(self._buffer)
                    self._bytes -= self._sizes.pop(oldest)
                    self._buffer.pop(oldest)
                    self._dropped_through = max(self._dropped_through, oldest)
                    self._dropped_total += 1
                self._condition.notify_all()
        except Exception:
            # A broken stream must never break the producer or other subscribers.
            return

    # --- ordered release --------------------------------------------------

    def _take_locked(self) -> dict | None:
        if self._controls:
            return {"kind": "control", "control": self._controls.popleft()}
        if self._dropped_through >= self._next:
            dropped = self._dropped_through - self._next + 1
            self._next = self._dropped_through + 1
            return {"kind": "control", "control": self._live_gap("queue_overflow", dropped)}
        if self._next in self._buffer:
            frame = self._buffer.pop(self._next)
            self._bytes -= self._sizes.pop(self._next)
            self._next += 1
            self._hole_since = None
            return frame
        if self._buffer:
            now = monotonic()
            if self._hole_since is None:
                self._hole_since = now
            elif now - self._hole_since >= self.reorder_grace:
                # An earlier sequence never reached us (a sibling subscriber raised
                # or a thread died): skip forward visibly instead of stalling.
                oldest = min(self._buffer)
                newest = max(self._buffer)
                self._next = oldest
                self._hole_since = None
                return {"kind": "control", "control": control_frame(
                    "gap", "sequence_hole", stream_id=self.stream_id,
                    oldest_sequence=oldest, newest_sequence=newest, dropped_count=None)}
        return None

    def _live_gap(self, reason: str, dropped: int | None) -> dict:
        oldest = min(self._buffer) if self._buffer else None
        newest = max(self._buffer) if self._buffer else None
        return control_frame("gap", reason, stream_id=self.stream_id,
                             oldest_sequence=oldest, newest_sequence=newest,
                             dropped_count=dropped)
