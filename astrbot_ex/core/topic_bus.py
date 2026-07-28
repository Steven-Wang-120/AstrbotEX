from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import RLock
from typing import Any, Callable


@dataclass(slots=True)
class TopicMessage:
    topic: str
    timestamp: float
    source: str
    payload: dict[str, Any]
    frame: str | None = None
    seq: int | None = None
    ttl_ms: int | None = None


TopicHandler = Callable[[TopicMessage], None]


class TopicInbox:
    def __init__(self, topic: str, max_messages: int) -> None:
        self.topic = topic
        self._queue: Queue[TopicMessage] = Queue(maxsize=max(1, max_messages))
        self._close: Callable[[], None] | None = None

    def get(self, timeout: float | None = None) -> TopicMessage | None:
        try:
            return self._queue.get(timeout=timeout)
        except Empty:
            return None

    def get_nowait(self) -> TopicMessage | None:
        return self.get(timeout=0.0)

    def take_latest(self) -> TopicMessage | None:
        latest = self.get_nowait()
        while True:
            item = self.get_nowait()
            if item is None:
                return latest
            latest = item

    def close(self) -> None:
        close = self._close
        self._close = None
        if close is not None:
            close()

    def _put(self, message: TopicMessage) -> None:
        try:
            self._queue.put_nowait(message)
            return
        except Full:
            pass
        try:
            self._queue.get_nowait()
        except Empty:
            pass
        try:
            self._queue.put_nowait(message)
        except Full:
            pass


class TopicBus:
    def __init__(self, recent_limit: int = 50) -> None:
        self.recent_limit = max(1, recent_limit)
        self._lock = RLock()
        self._subscribers: dict[str, dict[int, TopicHandler]] = defaultdict(dict)
        self._inboxes: dict[str, dict[int, TopicInbox]] = defaultdict(dict)
        self._latest: dict[str, TopicMessage] = {}
        self._recent: dict[str, deque[TopicMessage]] = defaultdict(lambda: deque(maxlen=self.recent_limit))
        self._next_token = 1

    def publish(self, topic: str, message: TopicMessage) -> None:
        with self._lock:
            self._latest[topic] = message
            self._recent[topic].append(message)
            handlers = list(self._subscribers.get(topic, {}).values())
            inboxes = list(self._inboxes.get(topic, {}).values())
        for inbox in inboxes:
            inbox._put(message)
        for handler in handlers:
            handler(message)

    def publish_payload(
        self,
        topic: str,
        *,
        timestamp: float,
        source: str,
        payload: dict[str, Any],
        frame: str | None = None,
        seq: int | None = None,
        ttl_ms: int | None = None,
    ) -> None:
        self.publish(
            topic,
            TopicMessage(
                topic=topic,
                timestamp=timestamp,
                source=source,
                payload=payload,
                frame=frame,
                seq=seq,
                ttl_ms=ttl_ms,
            ),
        )

    def subscribe(self, topic: str, handler: TopicHandler) -> Callable[[], None]:
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._subscribers[topic][token] = handler

        def unsubscribe() -> None:
            self.unsubscribe(topic, token)

        return unsubscribe

    def subscribe_inbox(self, topic: str, *, max_messages: int = 1) -> TopicInbox:
        inbox = TopicInbox(topic, max_messages)
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._inboxes[topic][token] = inbox

        def close() -> None:
            with self._lock:
                inboxes = self._inboxes.get(topic)
                if not inboxes:
                    return
                inboxes.pop(token, None)
                if not inboxes:
                    self._inboxes.pop(topic, None)

        inbox._close = close
        return inbox

    def unsubscribe(self, topic: str, token: int) -> None:
        with self._lock:
            handlers = self._subscribers.get(topic)
            if not handlers:
                return
            handlers.pop(token, None)
            if not handlers:
                self._subscribers.pop(topic, None)

    def get_latest(self, topic: str) -> TopicMessage | None:
        with self._lock:
            return self._latest.get(topic)

    def get_recent(self, topic: str, limit: int | None = None) -> list[TopicMessage]:
        with self._lock:
            recent = list(self._recent.get(topic, ()))
        if limit is None or limit >= len(recent):
            return recent
        return recent[-limit:]

    def list_topics(self) -> list[str]:
        with self._lock:
            topics = (
                set(self._latest.keys())
                | set(self._subscribers.keys())
                | set(self._inboxes.keys())
                | set(self._recent.keys())
            )
        return sorted(topics)
