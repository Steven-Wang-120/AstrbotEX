"""B09 — immutable upper-step batch and the decision-backend input adapter.

This module owns two things and nothing else:

* the frozen, ordered batch that crosses from the control channel into the
  decision service, and
* the adapter that hands that batch to whichever backend is currently
  configured, through the service's existing single-threaded backend slot.

It never applies a decision, never calls ``ActionService`` or the dispatcher and
never invents a ``Goal`` or a ``DecisionSnapshot``.  A successful hand-off only
proves that the natural-language text reached the model input boundary.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

SCHEMA = "aeb.upper_steps.v1"
VISIBILITY = "decision_only"

MAX_SEGMENTS = 64
MAX_SEGMENT_CHARS = 4096
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_ID_CHARS = 128

#: Codes a caller may see.  Format errors are rejected before any I/O; state
#: rejections mean "not this time"; ``delivery_unknown`` means the outcome is
#: genuinely unknown and must not be retried automatically.
ERROR_CODES = frozenset(
    {
        "invalid_batch",
        "payload_too_large",
        "route_mismatch",
        "session_mismatch",
        "id_conflict",
        "input_busy",
        "backend_unsupported",
        "delivery_unknown",
        "internal_error",
    }
)

#: Phases a receipt may carry.  ``received`` only proves registration and
#: ``injected`` only proves the text crossed the backend input boundary.
PHASES = ("received", "injected", "rejected", "unknown")


class InstructionInputError(Exception):
    """A bounded public error; the message never carries payload text."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown instruction input code: {code}")
        self.code = code
        self.detail = detail
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class StepSegment:
    segment_id: str
    ordinal: int
    text: str

    def to_wire(self) -> dict[str, Any]:
        return {"segment_id": self.segment_id, "ordinal": self.ordinal, "text": self.text}


@dataclass(frozen=True, slots=True)
class InstructionBatch:
    """One immutable, ordered hand-off.  Order and boundaries are preserved."""

    schema: str
    batch_id: str
    request_id: str
    plan_id: str
    plan_revision: int
    robot_id: str
    route_ref: str
    ex_session: str
    segments: tuple[StepSegment, ...]
    catalog_version: str = ""
    prompt_version: str = ""

    @property
    def segment_count(self) -> int:
        return len(self.segments)

    @property
    def segment_ids(self) -> tuple[str, ...]:
        return tuple(segment.segment_id for segment in self.segments)

    def to_wire(self) -> dict[str, Any]:
        """The exact payload shape that travels on the wire and into the model."""
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "batch_id": self.batch_id,
            "robot_id": self.robot_id,
            "route_ref": self.route_ref,
            "ex_session": self.ex_session,
            "catalog_version": self.catalog_version,
            "prompt_version": self.prompt_version,
            "visibility": VISIBILITY,
            "segments": [segment.to_wire() for segment in self.segments],
        }

    @property
    def payload_hash(self) -> str:
        return canonical_hash(self.to_wire())


def canonical_hash(value: Any) -> str:
    """UTF-8, sorted keys, compact separators, no non-finite numbers."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def measure_bytes(value: Any) -> int:
    return len(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    )


@runtime_checkable
class InstructionBackend(Protocol):
    """Optional capability.  It is detected explicitly, never assumed."""

    def consume_instruction(self, batch: InstructionBatch) -> Mapping[str, Any]:
        """Submit one batch at the model input boundary.

        Returns a mapping with at least ``accepted``.  Raising any exception
        means the text did not reach the model.
        """


class DecisionInstructionInput:
    """Hands batches to the configured backend through the decision service.

    The service owns the backend thread, the configuration epoch and the close
    ordering, so this adapter never touches the backend directly.  That keeps a
    new instruction round from racing an in-flight ``decide`` on a backend
    client that is not thread safe.
    """

    def __init__(self, service: Any, *, default_budget_sec: float = 1.5) -> None:
        self.service = service
        self.default_budget_sec = default_budget_sec

    @property
    def backend_name(self) -> str:
        return str(getattr(self.service, "_backend_name", "") or type(getattr(self.service, "backend", None)).__name__)

    def supports(self) -> tuple[bool, str]:
        checker = getattr(self.service, "instruction_ready", None)
        if not callable(checker):
            return False, "service_unsupported"
        try:
            ready, reason = checker()
        except Exception:
            return False, "service_unsupported"
        return bool(ready), str(reason or "")

    def accept(
        self,
        batch: InstructionBatch,
        *,
        budget_sec: float | None = None,
        on_settled: Any = None,
    ) -> dict[str, Any]:
        """Return ``{"phase": ..., "backend": ...}``; never raises for busy."""

        ready, reason = self.supports()
        if not ready:
            raise InstructionInputError("backend_unsupported", reason)

        submit = getattr(self.service, "submit_instruction", None)
        if not callable(submit):
            raise InstructionInputError("backend_unsupported", "submit_unsupported")

        budget = self.default_budget_sec if budget_sec is None else float(budget_sec)
        try:
            outcome = submit(batch, timeout_sec=budget, on_settled=on_settled)
        except InstructionInputError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            if isinstance(code, str) and code in ERROR_CODES:
                raise InstructionInputError(code) from None
            raise InstructionInputError("internal_error", type(exc).__name__) from None

        phase = str((outcome or {}).get("phase") or "received")
        if phase not in PHASES:
            phase = "unknown"
        return {
            "phase": phase,
            "backend": self.backend_name,
            "model": (outcome or {}).get("model"),
            "detail": (outcome or {}).get("detail", ""),
        }


__all__ = [
    "DecisionInstructionInput",
    "ERROR_CODES",
    "InstructionBackend",
    "InstructionBatch",
    "InstructionInputError",
    "MAX_ID_CHARS",
    "MAX_PAYLOAD_BYTES",
    "MAX_SEGMENT_CHARS",
    "MAX_SEGMENTS",
    "PHASES",
    "SCHEMA",
    "StepSegment",
    "VISIBILITY",
    "canonical_hash",
    "measure_bytes",
]
