"""B00 upper-control wire contracts, EX side.

This is the receiving half of ``aeb.upper_steps.v1``. It is a *new namespace*:
the seven frozen ``decision.*`` methods and the old ``Goal`` /
``DecisionSnapshot`` contracts are untouched, and a peer that does not know
``upper.*`` must report unsupported rather than fall back to
``interaction.message``, ``interaction.reply`` or ``bridge.proposal.submit``.

Scope: this module parses the wire batch and the delivery receipt only. Route
probabilities and the generation result are AEB-internal and never cross the
channel, so they are not duplicated here.

Invariants (B00 §3.5, C01 §4):

* only ``aeb.upper_steps.v1`` is accepted;
* only ``visibility = decision_only`` is accepted;
* ordinals are contiguous, no binary payload is accepted;
* unknown fields are rejected, never dropped;
* a format-valid ``robot_id`` is **not** an authorisation — identity is checked
  by ingress against the local connection record (``check_ingress``).

The AEB mirror is ``astrbot_plugin_astrbotex_interaction/upper_contracts.py``.
The two files are deliberately isomorphic but not shared at runtime: neither
repository imports the other. Both are verified against byte-identical
fixtures under ``tests/fixtures/upper_control``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Wire constants (C01 §4) -- must stay byte-identical to the AEB side
# --------------------------------------------------------------------------

UPPER_STEPS_SCHEMA = "aeb.upper_steps.v1"

ROUTE_SPEECH_ONLY = "speech_only"
ROUTE_ACTION_ONLY = "action_only"
ROUTE_SPEECH_AND_ACTION = "speech_and_action"

ROUTE_TIE_ORDER: tuple[str, ...] = (
    ROUTE_SPEECH_ONLY,
    ROUTE_SPEECH_AND_ACTION,
    ROUTE_ACTION_ONLY,
)
ROUTE_SET = frozenset(ROUTE_TIE_ORDER)

VISIBILITY_DECISION_ONLY = "decision_only"

PHASE_RECEIVED = "received"
PHASE_INJECTED = "injected"
PHASE_REJECTED = "rejected"
RECEIPT_PHASES = frozenset({PHASE_RECEIVED, PHASE_INJECTED, PHASE_REJECTED})

MAX_SEGMENTS = 64
MAX_SEGMENT_CHARS = 4096
MAX_PUBLIC_TEXT_CHARS = 4096
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_WIRE_ID_LEN = 128

PROBABILITY_TOLERANCE = 0.001

_WIRE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class ErrorCode:
    """Published rejection codes (B00 §3.6)."""

    OK = "ok"

    INVALID_PROBABILITIES = "invalid_probabilities"
    INVALID_RESULT = "invalid_result"
    INVALID_BATCH = "invalid_batch"
    PAYLOAD_TOO_LARGE = "payload_too_large"

    ROUTE_MISMATCH = "route_mismatch"
    SESSION_MISMATCH = "session_mismatch"
    ID_CONFLICT = "id_conflict"
    INPUT_BUSY = "input_busy"
    BACKEND_UNSUPPORTED = "backend_unsupported"

    DELIVERY_UNKNOWN = "delivery_unknown"


FORMAT_ERROR_CODES = frozenset(
    {
        ErrorCode.INVALID_PROBABILITIES,
        ErrorCode.INVALID_RESULT,
        ErrorCode.INVALID_BATCH,
        ErrorCode.PAYLOAD_TOO_LARGE,
    }
)
STATE_ERROR_CODES = frozenset(
    {
        ErrorCode.ROUTE_MISMATCH,
        ErrorCode.SESSION_MISMATCH,
        ErrorCode.ID_CONFLICT,
        ErrorCode.INPUT_BUSY,
        ErrorCode.BACKEND_UNSUPPORTED,
    }
)
UNCERTAIN_ERROR_CODES = frozenset({ErrorCode.DELIVERY_UNKNOWN})


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidationError:
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "path": self.path, "message": self.message}


class UpperContractError(ValueError):
    """Raised when an upper-control payload must be rejected."""

    def __init__(self, error: ValidationError) -> None:
        super().__init__(f"{error.code}: {error.path}: {error.message}")
        self.error = error

    @property
    def code(self) -> str:
        return self.error.code


def err(code: str, path: str, message: str) -> ValidationError:
    return ValidationError(code=code, path=path, message=message)


def reject(code: str, path: str, message: str) -> None:
    raise UpperContractError(err(code, path, message))


# --------------------------------------------------------------------------
# Canonical JSON and hashing (B00 §3.4)
# --------------------------------------------------------------------------


def canonical(value: Any, *, code: str = ErrorCode.INVALID_BATCH) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise UpperContractError(err(code, "$", "value is not canonical JSON")) from exc


def canonical_bytes(value: Any, *, code: str = ErrorCode.INVALID_BATCH) -> bytes:
    try:
        return canonical(value, code=code).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise UpperContractError(err(code, "$", "text is not valid UTF-8")) from exc


def canonical_hash(value: Any, *, code: str = ErrorCode.INVALID_BATCH) -> str:
    return hashlib.sha256(canonical_bytes(value, code=code)).hexdigest()


def _require_budget(payload: Any, path: str = "$") -> None:
    size = len(canonical_bytes(payload))
    if size > MAX_PAYLOAD_BYTES:
        reject(
            ErrorCode.PAYLOAD_TOO_LARGE,
            path,
            f"payload is {size} bytes, limit is {MAX_PAYLOAD_BYTES}",
        )


# --------------------------------------------------------------------------
# Primitive guards
# --------------------------------------------------------------------------


def _require_object(value: Any, path: str, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        reject(code, path, "must be an object")
    return value


def _require_exact_keys(
    value: dict[str, Any], expected: frozenset[str], path: str, code: str
) -> None:
    missing = sorted(expected - set(value))
    if missing:
        reject(code, f"{path}.{missing[0]}", "missing required field")
    unknown = sorted(set(value) - expected)
    if unknown:
        reject(code, f"{path}.{unknown[0]}", "unknown field is not accepted")


def _require_text(
    value: Any, path: str, code: str, *, max_len: int, preserve: bool = True
) -> str:
    if not isinstance(value, str):
        reject(code, path, "must be a string")
    text = value if preserve else value.strip()
    if not text.strip():
        reject(code, path, "must not be blank")
    if len(text) > max_len:
        reject(code, path, f"must not exceed {max_len} characters")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise UpperContractError(err(code, path, "must be valid UTF-8")) from exc
    return text


def _require_int(
    value: Any,
    path: str,
    code: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if type(value) is not int:
        reject(code, path, "must be an integer")
    if minimum is not None and value < minimum:
        reject(code, path, f"must be >= {minimum}")
    if maximum is not None and value > maximum:
        reject(code, path, f"must be <= {maximum}")
    return value


def _require_wire_id(value: Any, path: str, code: str) -> str:
    if not isinstance(value, str):
        reject(code, path, "must be a string")
    if not value:
        reject(code, path, "must not be empty")
    if len(value) > MAX_WIRE_ID_LEN:
        reject(code, path, f"must not exceed {MAX_WIRE_ID_LEN} characters")
    if not _WIRE_ID_RE.match(value):
        reject(code, path, "must match [A-Za-z0-9][A-Za-z0-9._:-]*")
    return value


def is_wire_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_WIRE_ID_LEN
        and _WIRE_ID_RE.match(value) is not None
    )


# --------------------------------------------------------------------------
# Wire batch (C01 §4)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepSegment:
    segment_id: str
    ordinal: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "ordinal": self.ordinal,
            "text": self.text,
        }


_BATCH_KEYS = frozenset(
    {
        "schema",
        "request_id",
        "plan_id",
        "plan_revision",
        "batch_id",
        "robot_id",
        "route_ref",
        "ex_session",
        "catalog_version",
        "prompt_version",
        "visibility",
        "segments",
    }
)
_SEGMENT_KEYS = frozenset({"segment_id", "ordinal", "text"})


@dataclass(frozen=True, slots=True)
class InjectionBatch:
    schema: str
    request_id: str
    plan_id: str
    plan_revision: int
    batch_id: str
    robot_id: str
    route_ref: str
    ex_session: str
    catalog_version: str
    prompt_version: str
    visibility: str
    segments: tuple[StepSegment, ...]

    @classmethod
    def parse(cls, payload: Any) -> "InjectionBatch":
        data = _require_object(payload, "$", ErrorCode.INVALID_BATCH)
        _require_exact_keys(data, _BATCH_KEYS, "$", ErrorCode.INVALID_BATCH)
        _require_budget(data)

        if data["schema"] != UPPER_STEPS_SCHEMA:
            reject(
                ErrorCode.INVALID_BATCH,
                "$.schema",
                f"only {UPPER_STEPS_SCHEMA} is accepted",
            )
        if data["visibility"] != VISIBILITY_DECISION_ONLY:
            reject(
                ErrorCode.INVALID_BATCH,
                "$.visibility",
                f"only {VISIBILITY_DECISION_ONLY} is accepted",
            )

        ids = {
            name: _require_wire_id(data[name], f"$.{name}", ErrorCode.INVALID_BATCH)
            for name in (
                "request_id",
                "plan_id",
                "batch_id",
                "robot_id",
                "route_ref",
                "ex_session",
                "catalog_version",
                "prompt_version",
            )
        }
        plan_revision = _require_int(
            data["plan_revision"], "$.plan_revision", ErrorCode.INVALID_BATCH, minimum=1
        )

        raw_segments = data["segments"]
        if not isinstance(raw_segments, list):
            reject(ErrorCode.INVALID_BATCH, "$.segments", "must be an array")
        if not raw_segments:
            reject(ErrorCode.INVALID_BATCH, "$.segments", "must not be empty")
        if len(raw_segments) > MAX_SEGMENTS:
            reject(
                ErrorCode.INVALID_BATCH,
                "$.segments",
                f"must not exceed {MAX_SEGMENTS} segments",
            )

        segments: list[StepSegment] = []
        seen_ids: set[str] = set()
        previous_ordinal: int | None = None
        for index, raw in enumerate(raw_segments):
            path = f"$.segments[{index}]"
            item = _require_object(raw, path, ErrorCode.INVALID_BATCH)
            _require_exact_keys(item, _SEGMENT_KEYS, path, ErrorCode.INVALID_BATCH)
            segment_id = _require_wire_id(
                item["segment_id"], f"{path}.segment_id", ErrorCode.INVALID_BATCH
            )
            if segment_id in seen_ids:
                reject(
                    ErrorCode.ID_CONFLICT,
                    f"{path}.segment_id",
                    "duplicate segment_id within one batch",
                )
            seen_ids.add(segment_id)
            ordinal = _require_int(
                item["ordinal"], f"{path}.ordinal", ErrorCode.INVALID_BATCH, minimum=0
            )
            if previous_ordinal is not None and ordinal != previous_ordinal + 1:
                reject(
                    ErrorCode.INVALID_BATCH,
                    f"{path}.ordinal",
                    "ordinals must increase contiguously by one",
                )
            previous_ordinal = ordinal
            text = _require_text(
                item["text"], f"{path}.text", ErrorCode.INVALID_BATCH,
                max_len=MAX_SEGMENT_CHARS,
            )
            segments.append(
                StepSegment(segment_id=segment_id, ordinal=ordinal, text=text)
            )

        return cls(
            schema=data["schema"],
            request_id=ids["request_id"],
            plan_id=ids["plan_id"],
            plan_revision=plan_revision,
            batch_id=ids["batch_id"],
            robot_id=ids["robot_id"],
            route_ref=ids["route_ref"],
            ex_session=ids["ex_session"],
            catalog_version=ids["catalog_version"],
            prompt_version=ids["prompt_version"],
            visibility=data["visibility"],
            segments=tuple(segments),
        )

    def to_dict(self) -> dict[str, Any]:
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
            "visibility": self.visibility,
            "segments": [segment.to_dict() for segment in self.segments],
        }

    def payload_hash(self) -> str:
        return canonical_hash(self.to_dict())

    def ordinals(self) -> tuple[int, ...]:
        return tuple(segment.ordinal for segment in self.segments)

    def joined_text(self) -> str:
        """One immutable model input: order and paragraph boundaries preserved."""
        return "\n".join(segment.text for segment in self.segments)


# --------------------------------------------------------------------------
# Receipt (C01 §5)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """``received``/``injected`` prove delivery, never execution."""

    batch_id: str
    request_id: str
    payload_hash: str
    phase: str
    error_code: str = ""

    def __post_init__(self) -> None:
        if self.phase not in RECEIPT_PHASES:
            reject(ErrorCode.INVALID_BATCH, "$.phase", "unknown receipt phase")
        if self.phase == PHASE_REJECTED and not self.error_code:
            reject(
                ErrorCode.INVALID_BATCH,
                "$.error_code",
                "a rejected receipt must name its error code",
            )
        if self.phase != PHASE_REJECTED and self.error_code:
            reject(
                ErrorCode.INVALID_BATCH,
                "$.error_code",
                "only a rejected receipt carries an error code",
            )

    @classmethod
    def parse(cls, payload: Any) -> "DeliveryReceipt":
        data = _require_object(payload, "$", ErrorCode.INVALID_BATCH)
        _require_exact_keys(
            data,
            frozenset({"batch_id", "request_id", "payload_hash", "phase", "error_code"}),
            "$",
            ErrorCode.INVALID_BATCH,
        )
        batch_id = _require_wire_id(
            data["batch_id"], "$.batch_id", ErrorCode.INVALID_BATCH
        )
        request_id = _require_wire_id(
            data["request_id"], "$.request_id", ErrorCode.INVALID_BATCH
        )
        payload_hash = _require_text(
            data["payload_hash"], "$.payload_hash", ErrorCode.INVALID_BATCH, max_len=128
        )
        phase = data["phase"]
        if phase not in RECEIPT_PHASES:
            reject(ErrorCode.INVALID_BATCH, "$.phase", "unknown receipt phase")
        error_code = data["error_code"]
        if not isinstance(error_code, str):
            reject(ErrorCode.INVALID_BATCH, "$.error_code", "must be a string")
        if phase == PHASE_REJECTED and not error_code:
            reject(
                ErrorCode.INVALID_BATCH,
                "$.error_code",
                "a rejected receipt must name its error code",
            )
        if phase != PHASE_REJECTED and error_code:
            reject(
                ErrorCode.INVALID_BATCH,
                "$.error_code",
                "only a rejected receipt carries an error code",
            )
        return cls(
            batch_id=batch_id,
            request_id=request_id,
            payload_hash=payload_hash,
            phase=phase,
            error_code=error_code,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "request_id": self.request_id,
            "payload_hash": self.payload_hash,
            "phase": self.phase,
            "error_code": self.error_code,
        }

    @property
    def is_delivered(self) -> bool:
        return self.phase in {PHASE_RECEIVED, PHASE_INJECTED}


def receipt_for(batch: InjectionBatch, phase: str, error_code: str = "") -> DeliveryReceipt:
    return DeliveryReceipt(
        batch_id=batch.batch_id,
        request_id=batch.request_id,
        payload_hash=batch.payload_hash(),
        phase=phase,
        error_code=error_code,
    )


# --------------------------------------------------------------------------
# Ingress identity checks (C01 §4)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrustedIngress:
    """Built from the local control-connection record, never from the payload."""

    robot_id: str
    ex_session: str
    route_refs: frozenset[str]

    def __post_init__(self) -> None:
        if not is_wire_id(self.robot_id):
            raise UpperContractError(err(ErrorCode.INVALID_BATCH, "$.robot_id", "invalid robot id"))
        if not is_wire_id(self.ex_session):
            raise UpperContractError(err(ErrorCode.INVALID_BATCH, "$.ex_session", "invalid ex session"))


def check_ingress(batch: InjectionBatch, trusted: TrustedIngress) -> None:
    """A format-valid id is not an authorisation; only this comparison is."""
    if batch.robot_id != trusted.robot_id:
        reject(
            ErrorCode.SESSION_MISMATCH,
            "$.robot_id",
            "robot_id does not match the local connection record",
        )
    if batch.ex_session != trusted.ex_session:
        reject(
            ErrorCode.SESSION_MISMATCH,
            "$.ex_session",
            "ex_session is stale or unknown",
        )
    if batch.route_ref not in trusted.route_refs:
        reject(
            ErrorCode.ROUTE_MISMATCH,
            "$.route_ref",
            "route_ref is not registered for this connection",
        )


def check_replay(previous: InjectionBatch | None, incoming: InjectionBatch) -> bool:
    """True for a byte-identical repeat; ``id_conflict`` for same ids, new content."""
    if previous is None:
        return False
    if previous.batch_id != incoming.batch_id:
        return False
    if previous.request_id != incoming.request_id:
        reject(
            ErrorCode.ID_CONFLICT,
            "$.request_id",
            "batch_id reused with a different request_id",
        )
    if previous.payload_hash() != incoming.payload_hash():
        reject(ErrorCode.ID_CONFLICT, "$", "same ids but different payload content")
    return True


# --------------------------------------------------------------------------
# Error classification helper
# --------------------------------------------------------------------------


def classify_error(code: str) -> str:
    if code in FORMAT_ERROR_CODES:
        return "format"
    if code in STATE_ERROR_CODES:
        return "state"
    if code in UNCERTAIN_ERROR_CODES:
        return "uncertain"
    return "unknown"


# --------------------------------------------------------------------------
# Model input adapter (B09 seam, declared here so B06 can depend on one object)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstructionInput:
    """One immutable instruction handed to the configured decision backend.

    ``segments`` keep their own boundaries; ``joined_text`` is the single
    natural-language block. Neither carries execution authority.
    """

    batch_id: str
    request_id: str
    payload_hash: str
    ex_session: str
    robot_id: str
    route_ref: str
    catalog_version: str
    prompt_version: str
    segments: tuple[StepSegment, ...] = field(default=())

    @classmethod
    def from_batch(cls, batch: InjectionBatch) -> "InstructionInput":
        return cls(
            batch_id=batch.batch_id,
            request_id=batch.request_id,
            payload_hash=batch.payload_hash(),
            ex_session=batch.ex_session,
            robot_id=batch.robot_id,
            route_ref=batch.route_ref,
            catalog_version=batch.catalog_version,
            prompt_version=batch.prompt_version,
            segments=batch.segments,
        )

    @property
    def joined_text(self) -> str:
        return "\n".join(segment.text for segment in self.segments)
