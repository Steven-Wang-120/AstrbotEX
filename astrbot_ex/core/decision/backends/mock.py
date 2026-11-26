"""Deterministic, replayable choices and event-latched failure injection."""
import threading
from collections import deque

from .base import DecisionBackend
from ..models import BackendDecision, DecisionSnapshot


class MockBackend(DecisionBackend):
    def __init__(self, *, kind: str = "wait", block: bool = False, fail: bool = False,
                 choices: dict[str, str] | None = None, decline_instruction: bool = False,
                 block_instruction: bool = False) -> None:
        self.kind, self.fail = kind, fail
        self.choices = dict(choices or {})
        self.entered = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()
        self.instruction_release = threading.Event()
        if not block_instruction:
            self.instruction_release.set()
        self.instruction_entered = threading.Event()
        self.closed = threading.Event()
        self.calls = 0
        self.history = deque(maxlen=64)
        #: Instruction batches are recorded, never interpreted: this double only
        #: proves that the text reached the input boundary.
        self.instructions = deque(maxlen=64)
        self.instruction_calls = 0
        self.decline_instruction = decline_instruction
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

    def consume_instruction(self, batch) -> dict:
        """Record one batch at the model input boundary.

        No Goal, no snapshot, no decision: an accepted batch only proves the
        text crossed this boundary.
        """
        if self.closed.is_set():
            raise RuntimeError("mock backend closed")
        with self._lock:
            self.instruction_calls += 1
            self.instructions.append(batch)
        self.instruction_entered.set()
        self.instruction_release.wait()
        if self.closed.is_set():
            raise RuntimeError("mock backend closed")
        if self.fail:
            raise RuntimeError("mock backend failure")
        if self.decline_instruction:
            return {"accepted": False, "model": "deterministic-1", "detail": "declined",
                    "input_sha256": batch.payload_hash}
        return {"accepted": True, "model": "deterministic-1", "detail": "accepted",
                "input_sha256": batch.payload_hash}

    def supports_instruction_input(self) -> bool:
        return not self.closed.is_set()

    def close(self) -> None:
        self.closed.set()
        self.release.set()
        self.instruction_release.set()
