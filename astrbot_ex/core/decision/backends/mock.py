"""Deterministic, replayable choices and event-latched failure injection."""
import threading
from collections import deque

from .base import DecisionBackend
from ..models import BackendDecision, DecisionSnapshot


class MockBackend(DecisionBackend):
    def __init__(self, *, kind: str = "wait", block: bool = False, fail: bool = False,
                 choices: dict[str, str] | None = None) -> None:
        self.kind, self.fail = kind, fail
        self.choices = dict(choices or {})
        self.entered = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()
        self.closed = threading.Event()
        self.calls = 0
        self.history = deque(maxlen=64)
        self._lock = threading.Lock()

    def decide(self, snapshot: DecisionSnapshot) -> BackendDecision:
        snapshot = DecisionSnapshot.parse(snapshot.to_dict())
        with self._lock:
            self.calls += 1
            self.history.append(snapshot.snapshot_id)
        self.entered.set()
        self.release.wait()
        if self.closed.is_set():
            raise RuntimeError("mock backend closed")
        if self.fail:
            raise RuntimeError("mock backend failure")
        choices = []
        for owner in snapshot.owners:
            eligible = [c for c in owner["candidates"] if c["eligible"]]
            desired = self.choices.get(owner["owner"], self.kind)
            choice = next((c for c in eligible if c["kind"] == desired or c["option_id"] == desired),
                          next(c for c in eligible if c["kind"] == "wait"))
            choices.append({"owner": owner["owner"], "option_id": choice["option_id"]})
        return BackendDecision.parse({"schema_version": 1, "snapshot_id": snapshot.snapshot_id,
                                      "versions": snapshot.versions.to_dict(), "backend": "mock",
                                      "model": "deterministic-1", "elapsed_ms": 0, "choices": choices})

    def close(self) -> None:
        self.closed.set()
        self.release.set()
