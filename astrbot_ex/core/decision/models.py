"""B00 frozen goal/decision contracts shared by AstrBotEX and A.E.B.

Pure standard library, no runtime or hardware imports. A byte-identical copy
lives in the A.E.B plugin so both sides parse golden fixtures identically.

Method set (B00 搂2.1, text channel, inside the unchanged astrbotex-zmq v1
envelope):

    decision.context.get   AEB -> EX   read-only catalog/observations/status
    decision.goal.submit   AEB -> EX   atomic goal + params, expected_revision
    decision.goal.cancel   AEB -> EX   cancel current goal (accept != stopped)
    decision.goal.renew    AEB -> EX   renew lease only, never a new goal
    decision.state.get     AEB -> EX   active/pending goal, status, ex_session
    decision.events.get    AEB -> EX   replay by event_seq, resync_required
    decision.feedback      EX  -> AEB  feedback; AEB acks persisted event_seq
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any

from astrbot_ex.core.actions.models import (
    ContractError,
    ErrorCode,
    GoalPhase,
    IdempotencyRegistry,
    MAX_ID_LEN,
    MAX_SEQUENCE,
    ValidationError,
    check_ex_session,
    check_revision,
    err,
    find_non_finite,
    goal_text_marker,
    measure_json_budget,
    require_bool,
    reject,
    require_int,
    require_lease,
    require_object,
    require_string_list,
    require_text,
    validate_goal_text,
    validate_stop_evidence,
)
from astrbot_ex.core.actions.models import SCHEMA_VERSION

# --------------------------------------------------------------------------
# Frozen method names. An old peer that does not know a new method must report
# unsupported; it may never quietly fall back to the legacy proposal path.
# --------------------------------------------------------------------------

METHOD_CONTEXT_GET = "decision.context.get"
METHOD_GOAL_SUBMIT = "decision.goal.submit"
METHOD_GOAL_CANCEL = "decision.goal.cancel"
METHOD_GOAL_RENEW = "decision.goal.renew"
METHOD_STATE_GET = "decision.state.get"
METHOD_EVENTS_GET = "decision.events.get"
METHOD_FEEDBACK = "decision.feedback"

DECISION_METHODS = frozenset(
    {
        METHOD_CONTEXT_GET,
        METHOD_GOAL_SUBMIT,
        METHOD_GOAL_CANCEL,
        METHOD_GOAL_RENEW,
        METHOD_STATE_GET,
        METHOD_EVENTS_GET,
        METHOD_FEEDBACK,
    }
)

UNSUPPORTED_METHOD = "unsupported_method"
FEEDBACK_STATUSES = frozenset({"admitted", "rejected", "accepted", "running", "succeeded", "failed", "canceled", "timed_out", "unknown"})


def _wire_object(data: Any, path: str, fields: set[str]) -> dict[str, Any]:
    raw = require_object(data, path)
    # Report field-local invalid JSON values relative to the wire root; budget
    # failures describe the entire message and stay anchored to its message name.
    non_finite = find_non_finite(raw, "")
    if non_finite is not None and non_finite.code in (ErrorCode.NON_FINITE_NUMBER, ErrorCode.INVALID_TYPE):
        raise ContractError(non_finite)
    budget = measure_json_budget(raw, path)
    if budget is not None:
        reject(budget, path, "message exceeds JSON validation budget or is not JSON")
    if non_finite is not None:
        reject(non_finite.code, path, non_finite.message)
    for key in raw:
        if key not in fields:
            reject(ErrorCode.UNKNOWN_FIELD, f"{path}.{key}", "unknown field")
    _required(raw, ("schema_version",), path)
    return raw


def _version(raw: dict[str, Any]) -> None:
    if "schema_version" not in raw:
        reject(ErrorCode.MISSING_FIELD, "schema_version", "is required")
    version = raw["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        reject(ErrorCode.UNSUPPORTED_SCHEMA_VERSION, "schema_version", f"unsupported schema_version: {version!r}")


def _required(raw: dict[str, Any], fields: tuple[str, ...], path: str) -> None:
    for field_name in fields:
        if field_name not in raw:
            reject(ErrorCode.MISSING_FIELD, f"{path}.{field_name}", "is required")


def _id(value: Any, path: str) -> str:
    return require_text(value, path, max_len=MAX_ID_LEN)


def _seq(value: Any, path: str, *, minimum: int = 0) -> int:
    return require_int(value, path, minimum=minimum, maximum=MAX_SEQUENCE)


def _optional_text(value: Any, path: str) -> str:
    if not isinstance(value, str):
        reject(ErrorCode.INVALID_TYPE, path, "must be a string")
    if len(value) > MAX_ID_LEN:
        reject(ErrorCode.TEXT_TOO_LONG, path, f"must not exceed {MAX_ID_LEN} characters")
    return value


def _json_copy(value: Any) -> Any:
    """Copy already validated JSON values at the input and output boundaries."""
    return copy.deepcopy(value)


def _event(raw: Any, path: str) -> dict[str, Any]:
    """Validate one event before any caller reads event fields."""
    item = require_object(raw, path)
    non_finite = find_non_finite(item, path)
    if non_finite is not None:
        raise ContractError(non_finite)
    budget = measure_json_budget(item, path)
    if budget is not None:
        reject(budget, path, "event exceeds JSON validation budget")
    fields = {
        "event_id", "event_seq", "ex_session", "task_id", "goal_id",
        "goal_revision", "command_id", "owner", "status", "reason_code", "details",
    }
    for key in item:
        if key not in fields:
            reject(ErrorCode.UNKNOWN_FIELD, f"{path}.{key}", "unknown field")
    for key in fields - {"reason_code", "details"}:
        if key not in item:
            reject(ErrorCode.MISSING_FIELD, f"{path}.{key}", "is required")
    for key in ("event_id", "ex_session", "task_id", "goal_id", "command_id", "owner"):
        _id(item[key], f"{path}.{key}")
    _seq(item["event_seq"], f"{path}.event_seq", minimum=1)
    _seq(item["goal_revision"], f"{path}.goal_revision")
    status = _id(item["status"], f"{path}.status")
    if status not in FEEDBACK_STATUSES:
        reject(ErrorCode.ENUM_VIOLATION, f"{path}.status", "unknown status")
    _optional_text(item.get("reason_code", ""), f"{path}.reason_code")
    details = require_object(item.get("details", {}), f"{path}.details")
    if status == "canceled":
        validate_stop_evidence(details, f"{path}.details")
    return _json_copy(item)


def _parse_readonly_payload(data: Any, path: str) -> dict[str, Any]:
    """Read-only context/state requests may bootstrap without a session."""
    raw = _wire_object(data, path, {"schema_version", "ex_session"})
    _version(raw)
    result: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    if "ex_session" in raw:
        result["ex_session"] = _id(raw["ex_session"], "ex_session")
    return result


# The four distinct facts that must never be conflated (B00 §2.1).
FACT_TRANSPORT_ACK = "transport_ack"
FACT_SUBMIT_ACCEPTED = "submit_accepted"
FACT_GOAL_ACTIVE = "goal_active"
FACT_ACTION_COMPLETED = "action_completed"
DISTINCT_FACTS = (FACT_TRANSPORT_ACK, FACT_SUBMIT_ACCEPTED, FACT_GOAL_ACTIVE, FACT_ACTION_COMPLETED)


# --------------------------------------------------------------------------
# Goal submit
# --------------------------------------------------------------------------


@dataclass(slots=True)
class GoalSubmit:
    """Atomic goal + bound parameters for one revision.

    The goal text and its parameters travel as one indivisible revision: a
    goal must never be updated while its parameters stay at an old version.
    """

    request_id: str
    ex_session: str
    task_id: str
    step_id: str
    goal_id: str
    goal_text_en: str
    allowed_actions: list[str] = field(default_factory=list)
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    completion: dict[str, Any] = field(default_factory=dict)
    lease_ms: int = 0
    expected_revision: int | None = None
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def parse(cls, data: Any) -> "GoalSubmit":
        raw = _wire_object(data, "goal_submit", {
            "schema_version", "request_id", "ex_session", "task_id", "step_id",
            "goal_id", "goal_text_en", "allowed_actions", "parameters",
            "completion", "lease_ms", "expected_revision",
        })
        _version(raw)
        _required(raw, ("request_id", "ex_session", "task_id", "step_id", "goal_id", "goal_text_en", "lease_ms"), "goal_submit")
        # Only emptiness and length are parsed; language is a skill concern.
        goal_text = validate_goal_text(raw.get("goal_text_en"))

        allowed = require_string_list(raw.get("allowed_actions", []), "allowed_actions")
        for index, action_id in enumerate(allowed):
            _id(action_id, f"allowed_actions[{index}]")
        if len(set(allowed)) != len(allowed):
            reject(ErrorCode.ENUM_VIOLATION, "allowed_actions", "duplicate action")
        parameters = require_object(raw.get("parameters", {}), "parameters")
        for key, value in parameters.items():
            _id(key, f"parameters.{key}")
            if key not in allowed:
                reject(ErrorCode.UNKNOWN_ACTION, f"parameters.{key}", "action is not allowed")
            require_object(value, f"parameters.{key}")
        completion = require_object(raw.get("completion", {}), "completion")
        for key in completion:
            if key != "required_success_actions":
                reject(ErrorCode.UNKNOWN_FIELD, f"completion.{key}", "unknown field")
        if completion:
            required = require_string_list(
                completion.get("required_success_actions", []),
                "completion.required_success_actions",
            )
            for index, action_id in enumerate(required):
                if action_id not in allowed:
                    reject(
                        ErrorCode.UNKNOWN_ACTION,
                        f"completion.required_success_actions[{index}]",
                        f"required success action is not in allowed_actions: {action_id}",
                    )
        expected_revision = raw.get("expected_revision")
        if "expected_revision" in raw and expected_revision is None:
            reject(ErrorCode.INVALID_TYPE, "expected_revision", "omit the field instead of sending null")
        return cls(
            request_id=_id(raw.get("request_id"), "request_id"),
            ex_session=_id(raw.get("ex_session"), "ex_session"),
            task_id=_id(raw.get("task_id"), "task_id"),
            step_id=_id(raw.get("step_id"), "step_id"),
            goal_id=_id(raw.get("goal_id"), "goal_id"),
            goal_text_en=goal_text,
            allowed_actions=allowed,
            parameters=_json_copy(parameters),
            completion=_json_copy(completion),
            lease_ms=require_lease(raw.get("lease_ms"), "lease_ms"),
            expected_revision=(
                None if expected_revision is None else _seq(expected_revision, "expected_revision")
            ),
            schema_version=SCHEMA_VERSION,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "ex_session": self.ex_session,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "goal_id": self.goal_id,
            "goal_text_en": self.goal_text_en,
            "allowed_actions": list(self.allowed_actions),
            "parameters": _json_copy(self.parameters),
            "completion": _json_copy(self.completion),
            "lease_ms": self.lease_ms,
        }
        if self.expected_revision is not None:
            data["expected_revision"] = self.expected_revision
        return data

    def english_marker(self) -> ValidationError | None:
        """Advisory non-English marker for skill/eval reporting; never a gate."""
        return goal_text_marker(self.goal_text_en)


@dataclass(slots=True)
class GoalSubmitResult:
    """Only ``phase == active`` enters decision."""

    ok: bool
    request_id: str
    ex_session: str
    goal_id: str
    revision: int
    phase: str
    error: ValidationError | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": self.ok,
            "request_id": self.request_id,
            "ex_session": self.ex_session,
            "goal_id": self.goal_id,
            "revision": self.revision,
            "phase": self.phase,
        }
        if self.error is not None:
            data["error"] = self.error.to_dict()
        return data


# --------------------------------------------------------------------------
# Cancel / renew
# --------------------------------------------------------------------------


@dataclass(slots=True)
class GoalCancel:
    request_id: str
    ex_session: str
    goal_id: str
    goal_revision: int
    reason_code: str = ""
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def parse(cls, data: Any) -> "GoalCancel":
        raw = _wire_object(data, "goal_cancel", {
            "schema_version", "request_id", "ex_session", "goal_id", "goal_revision", "reason_code",
        })
        _version(raw)
        return cls(
            request_id=_id(raw.get("request_id"), "request_id"),
            ex_session=_id(raw.get("ex_session"), "ex_session"),
            goal_id=_id(raw.get("goal_id"), "goal_id"),
            goal_revision=_seq(raw.get("goal_revision"), "goal_revision", minimum=0),
            reason_code=_optional_text(raw.get("reason_code", ""), "reason_code"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "ex_session": self.ex_session,
            "goal_id": self.goal_id,
            "goal_revision": self.goal_revision,
            "reason_code": self.reason_code,
        }


@dataclass(slots=True)
class GoalRenew:
    """Renews the current control lease only. Must never create a new goal."""

    request_id: str
    ex_session: str
    goal_id: str
    goal_revision: int
    lease_ms: int = 0
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def parse(cls, data: Any) -> "GoalRenew":
        raw = _wire_object(data, "goal_renew", {
            "schema_version", "request_id", "ex_session", "goal_id", "goal_revision", "lease_ms",
        })
        _version(raw)
        return cls(
            request_id=_id(raw.get("request_id"), "request_id"),
            ex_session=_id(raw.get("ex_session"), "ex_session"),
            goal_id=_id(raw.get("goal_id"), "goal_id"),
            goal_revision=_seq(raw.get("goal_revision"), "goal_revision", minimum=0),
            lease_ms=require_lease(raw.get("lease_ms"), "lease_ms"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "ex_session": self.ex_session,
            "goal_id": self.goal_id,
            "goal_revision": self.goal_revision,
            "lease_ms": self.lease_ms,
        }


# --------------------------------------------------------------------------
# State / events / feedback
# --------------------------------------------------------------------------


@dataclass(slots=True)
class DecisionState:
    ex_session: str
    revision: int
    active_goal_id: str | None = None
    active_phase: str | None = None
    pending_goal_id: str | None = None
    pending_phase: str | None = None
    execution: dict[str, Any] = field(default_factory=dict)
    event_seq: int = 0

    @classmethod
    def parse(cls, data: Any) -> "DecisionState":
        raw = _wire_object(data, "state", {
            "schema_version", "ex_session", "revision", "active_goal_id", "active_phase",
            "pending_goal_id", "pending_phase", "execution", "event_seq",
        })
        _version(raw)
        active_goal_id = raw.get("active_goal_id")
        pending_goal_id = raw.get("pending_goal_id")
        active_phase = raw.get("active_phase")
        pending_phase = raw.get("pending_phase")
        if active_goal_id is not None:
            active_goal_id = _id(active_goal_id, "active_goal_id")
        if pending_goal_id is not None:
            pending_goal_id = _id(pending_goal_id, "pending_goal_id")
        if active_phase is not None:
            active_phase = _id(active_phase, "active_phase")
            if active_phase not in (GoalPhase.ACCEPTED, GoalPhase.PENDING_CANCEL, GoalPhase.ACTIVE, GoalPhase.BLOCKED, GoalPhase.REJECTED):
                reject(ErrorCode.ENUM_VIOLATION, "active_phase", "unknown goal phase")
        if pending_phase is not None:
            pending_phase = _id(pending_phase, "pending_phase")
            if pending_phase not in (GoalPhase.ACCEPTED, GoalPhase.PENDING_CANCEL, GoalPhase.ACTIVE, GoalPhase.BLOCKED, GoalPhase.REJECTED):
                reject(ErrorCode.ENUM_VIOLATION, "pending_phase", "unknown goal phase")
        if (active_goal_id is None) != (active_phase is None):
            reject(ErrorCode.INVALID_TYPE, "active_phase", "active goal and phase must be paired")
        if (pending_goal_id is None) != (pending_phase is None):
            reject(ErrorCode.INVALID_TYPE, "pending_phase", "pending goal and phase must be paired")
        if active_phase is not None and active_phase != GoalPhase.ACTIVE:
            reject(ErrorCode.ENUM_VIOLATION, "active_phase", "active goal must have active phase")
        if pending_phase is not None and pending_phase not in (GoalPhase.ACCEPTED, GoalPhase.PENDING_CANCEL, GoalPhase.BLOCKED):
            reject(ErrorCode.ENUM_VIOLATION, "pending_phase", "invalid pending goal phase")
        if active_goal_id is not None and active_goal_id == pending_goal_id:
            reject(ErrorCode.INVALID_TYPE, "pending_goal_id", "pending and active goal must differ")
        return cls(
            ex_session=_id(raw.get("ex_session"), "ex_session"),
            revision=_seq(raw.get("revision"), "revision"),
            active_goal_id=active_goal_id,
            active_phase=active_phase,
            pending_goal_id=pending_goal_id,
            pending_phase=pending_phase,
            execution=_json_copy(require_object(raw.get("execution", {}), "execution")),
            event_seq=_seq(raw.get("event_seq", 0), "event_seq"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "ex_session": self.ex_session,
            "revision": self.revision,
            "active_goal_id": self.active_goal_id,
            "active_phase": self.active_phase,
            "pending_goal_id": self.pending_goal_id,
            "pending_phase": self.pending_phase,
            "execution": _json_copy(self.execution),
            "event_seq": self.event_seq,
        }


@dataclass(slots=True)
class EventsRequest:
    ex_session: str
    since_event_seq: int = 0
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def parse(cls, data: Any) -> "EventsRequest":
        raw = _wire_object(data, "events_request", {"schema_version", "ex_session", "since_event_seq"})
        _version(raw)
        if "ex_session" not in raw:
            reject(ErrorCode.MISSING_FIELD, "ex_session", "is required")
        return cls(
            ex_session=_id(raw["ex_session"], "ex_session"),
            since_event_seq=_seq(raw.get("since_event_seq", 0), "since_event_seq"),
        )
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ex_session": self.ex_session,
            "since_event_seq": self.since_event_seq,
        }


@dataclass(slots=True)
class EventsReply:
    """When history has been trimmed past ``since_event_seq`` the EX side must
    report ``resync_required`` instead of pretending the gap does not exist."""

    ex_session: str
    events: list[dict[str, Any]] = field(default_factory=list)
    oldest_available_seq: int = 0
    latest_event_seq: int = 0
    resync_required: bool = False

    @classmethod
    def parse(cls, data: Any) -> "EventsReply":
        raw = _wire_object(data, "events_reply", {
            "schema_version", "ex_session", "events", "oldest_available_seq",
            "latest_event_seq", "resync_required",
        })
        _version(raw)
        for key in ("ex_session", "events", "oldest_available_seq", "latest_event_seq", "resync_required"):
            if key not in raw:
                reject(ErrorCode.MISSING_FIELD, f"events_reply.{key}", "is required")
        events = raw["events"]
        if not isinstance(events, list):
            reject(ErrorCode.INVALID_TYPE, "events", "must be an array")
        oldest = _seq(raw["oldest_available_seq"], "oldest_available_seq")
        latest = _seq(raw["latest_event_seq"], "latest_event_seq")
        if oldest > latest + 1:
            reject(ErrorCode.RANGE_VIOLATION, "oldest_available_seq", "cannot exceed latest_event_seq + 1")
        session = _id(raw["ex_session"], "ex_session")
        parsed = [_event(item, f"events[{index}]") for index, item in enumerate(events)]
        resync_required = require_bool(raw["resync_required"], "resync_required")
        if resync_required and parsed:
            reject(ErrorCode.INVALID_TYPE, "events", "resync_required replies must contain no events")
        previous = 0
        for index, item in enumerate(parsed):
            event_path = f"events[{index}]"
            if item["event_seq"] <= previous:
                reject(ErrorCode.RANGE_VIOLATION, f"{event_path}.event_seq", "event sequences must be strictly increasing")
            previous = item["event_seq"]
            if item["ex_session"] != session:
                reject(ErrorCode.STALE_EX_SESSION, f"{event_path}.ex_session", "event session differs from reply session")
            if not oldest <= item["event_seq"] <= latest:
                reject(ErrorCode.RANGE_VIOLATION, f"{event_path}.event_seq", "event sequence is outside reply range")
        return cls(
            ex_session=session,
            events=parsed,
            oldest_available_seq=oldest,
            latest_event_seq=latest,
            resync_required=resync_required,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "ex_session": self.ex_session,
            "events": _json_copy(self.events),
            "oldest_available_seq": self.oldest_available_seq,
            "latest_event_seq": self.latest_event_seq,
            "resync_required": self.resync_required,
        }


def events_reply(
    *,
    ex_session: str,
    buffered: list[dict[str, Any]],
    oldest_available_seq: int,
    since_event_seq: int,
    latest_event_seq: int | None = None,
) -> EventsReply:
    """Validate the full buffer before selecting events or reporting a gap."""
    if not isinstance(buffered, list):
        reject(ErrorCode.INVALID_TYPE, "buffered", "must be an array")
    budget = measure_json_budget(buffered, "events")
    if budget is not None:
        reject(budget, "events", "event buffer exceeds JSON validation budget")
    non_finite = find_non_finite(buffered, "events")
    if non_finite is not None:
        raise ContractError(non_finite)
    ex_session = _id(ex_session, "ex_session")
    oldest_available_seq = _seq(oldest_available_seq, "oldest_available_seq")
    since_event_seq = _seq(since_event_seq, "since_event_seq")
    parsed = [_event(item, f"events[{index}]") for index, item in enumerate(buffered)]
    sequences = [item["event_seq"] for item in parsed]
    previous = 0
    for index, item in enumerate(parsed):
        event_path = f"events[{index}]"
        if item["event_seq"] <= previous:
            reject(ErrorCode.RANGE_VIOLATION, f"{event_path}.event_seq", "event sequences must be strictly increasing")
        previous = item["event_seq"]
        if item["ex_session"] != ex_session:
            reject(ErrorCode.STALE_EX_SESSION, f"{event_path}.ex_session", "event session differs from requested session")
    latest = max(sequences, default=0) if latest_event_seq is None else _seq(latest_event_seq, "latest_event_seq")
    if latest < max(sequences, default=0) or oldest_available_seq > latest + 1:
        reject(ErrorCode.RANGE_VIOLATION, "latest_event_seq", "latest event sequence is inconsistent")
    resync = since_event_seq < oldest_available_seq - 1
    for index, item in enumerate(parsed):
        if not oldest_available_seq <= item["event_seq"] <= latest:
            reject(
                ErrorCode.RANGE_VIOLATION,
                f"events[{index}].event_seq",
                "event sequence is outside the declared buffer range",
            )
    selected = [] if resync else [item for item in parsed if item["event_seq"] > since_event_seq]
    return EventsReply(
        ex_session=ex_session,
        events=[] if resync else selected,
        oldest_available_seq=oldest_available_seq,
        latest_event_seq=latest,
        resync_required=resync,
    )


@dataclass(slots=True)
class Feedback:
    """EX -> AEB feedback. ``acked_event_seq`` is the highest event_seq AEB has
    durably persisted; it is an acknowledgement of receipt, not of execution."""

    ex_session: str
    task_id: str
    goal_id: str
    goal_revision: int
    event_seq: int
    status: str
    reason_code: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, data: Any) -> "Feedback":
        raw = _wire_object(data, "feedback", {
            "schema_version", "ex_session", "task_id", "goal_id", "goal_revision",
            "event_seq", "status", "reason_code", "details",
        })
        _version(raw)
        details = require_object(raw.get("details", {}), "details")
        status = _id(raw.get("status"), "status")
        if status not in FEEDBACK_STATUSES:
            reject(ErrorCode.ENUM_VIOLATION, "status", "unknown feedback status")
        if status == "canceled":
            validate_stop_evidence(details)
        return cls(
            ex_session=_id(raw.get("ex_session"), "ex_session"),
            task_id=_id(raw.get("task_id"), "task_id"),
            goal_id=_id(raw.get("goal_id"), "goal_id"),
            goal_revision=_seq(raw.get("goal_revision"), "goal_revision"),
            event_seq=_seq(raw.get("event_seq"), "event_seq", minimum=1),
            status=status,
            reason_code=_optional_text(raw.get("reason_code", ""), "reason_code"),
            details=_json_copy(details),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "ex_session": self.ex_session,
            "task_id": self.task_id,
            "goal_id": self.goal_id,
            "goal_revision": self.goal_revision,
            "event_seq": self.event_seq,
            "status": self.status,
            "reason_code": self.reason_code,
            "details": _json_copy(self.details),
        }


def _rebase_error(error: ValidationError, prefix: str, local_root: str | None = None) -> None:
    path = error.path
    if local_root and (path == local_root or path.startswith(f"{local_root}.")):
        path = path[len(local_root):]
    if path and not path.startswith(("[", ".")):
        path = f".{path}"
    reject(error.code, f"{prefix}{path}", error.message)


def _parse_nested(parser: Any, value: Any, prefix: str) -> Any:
    local_roots = {
        ObservationEnvelope: "observation",
        VersionSet: "versions",
        DecisionSnapshot: "snapshot",
        BackendDecision: "backend_decision",
    }
    try:
        return parser.parse(value)
    except ContractError as exc:
        _rebase_error(exc.error, prefix, local_roots.get(parser))


def _snapshot_object(data: Any, path: str, fields: set[str], required: tuple[str, ...]) -> dict[str, Any]:
    raw = require_object(data, path)
    for key in raw:
        if key not in fields:
            reject(ErrorCode.UNKNOWN_FIELD, f"{path}.{key}", "unknown field")
    for key in required:
        if key not in raw:
            reject(ErrorCode.MISSING_FIELD, f"{path}.{key}", "is required")
    return raw


def _nonnegative_number(value: Any, path: str, maximum: float | None = None) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        reject(ErrorCode.INVALID_TYPE, path, "must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        reject(ErrorCode.NON_FINITE_NUMBER, path, "must be finite")
    if value < 0 or (maximum is not None and value > maximum):
        reject(ErrorCode.RANGE_VIOLATION, path, "number outside allowed range")
    return value


MAX_MONOTONIC_NS = 2**63 - 1
CANDIDATE_KINDS = frozenset({"start", "cancel", "pause", "resume", "keep", "wait", "request_replan"})
PROBABILITY_SUM_TOLERANCE = 1e-9


@dataclass(slots=True)
class ObservationEnvelope:
    observation_id: str
    source_id: str
    source_epoch: str
    seq: int
    received_monotonic_ns: int
    age_ms: int | float
    description_hash: str
    health: dict[str, str]
    data: dict[str, Any]

    @classmethod
    def parse(cls, data: Any) -> "ObservationEnvelope":
        path = "observation"
        raw = _wire_object(data, path, {"schema_version", "observation_id", "source_id", "source_epoch", "seq", "received_monotonic_ns", "age_ms", "description_hash", "health", "data"})
        _version(raw)
        item = _snapshot_object(raw, path, set(raw), ("observation_id", "source_id", "source_epoch", "seq", "received_monotonic_ns", "age_ms", "description_hash", "health", "data"))
        health = _snapshot_object(item["health"], "health", {"status", "reason_code"}, ("status", "reason_code"))
        status = _id(health["status"], "health.status")
        if status not in {"ok", "stale", "error"}:
            reject(ErrorCode.ENUM_VIOLATION, "health.status", "unknown observation health")
        return cls(_id(item["observation_id"], "observation_id"), _id(item["source_id"], "source_id"),
                   _id(item["source_epoch"], "source_epoch"), _seq(item["seq"], "seq"),
                   require_int(item["received_monotonic_ns"], "received_monotonic_ns", minimum=0, maximum=MAX_MONOTONIC_NS),
                   _nonnegative_number(item["age_ms"], "age_ms"), _id(item["description_hash"], "description_hash"),
                   {"status": status, "reason_code": _optional_text(health["reason_code"], "health.reason_code")},
                   _json_copy(require_object(item["data"], "data")))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "observation_id": self.observation_id, "source_id": self.source_id,
                "source_epoch": self.source_epoch, "seq": self.seq, "received_monotonic_ns": self.received_monotonic_ns,
                "age_ms": self.age_ms, "description_hash": self.description_hash, "health": dict(self.health), "data": _json_copy(self.data)}


@dataclass(slots=True)
class VersionSet:
    ex_session: str
    goal_revision: int
    config_revision: int
    catalog_revision: int
    environment_generation: int
    gate_epoch: int
    plugin_generations: dict[str, int]

    @classmethod
    def parse(cls, data: Any) -> "VersionSet":
        invalid = find_non_finite(data, "versions")
        if invalid is not None: raise ContractError(invalid)
        budget = measure_json_budget(data, "versions")
        if budget is not None: reject(budget, "versions", "versions exceed JSON budget")
        keys = ("ex_session", "goal_revision", "config_revision", "catalog_revision", "environment_generation", "gate_epoch", "plugin_generations")
        raw = _snapshot_object(data, "versions", set(keys), keys)
        generations = require_object(raw["plugin_generations"], "plugin_generations")
        parsed = {_id(owner, f"plugin_generations.{owner}"): _seq(value, f"plugin_generations.{owner}") for owner, value in generations.items()}
        return cls(_id(raw["ex_session"], "ex_session"), *(_seq(raw[key], key) for key in keys[1:-1]), parsed)

    def to_dict(self) -> dict[str, Any]:
        return {"ex_session": self.ex_session, "goal_revision": self.goal_revision, "config_revision": self.config_revision,
                "catalog_revision": self.catalog_revision, "environment_generation": self.environment_generation,
                "gate_epoch": self.gate_epoch, "plugin_generations": dict(self.plugin_generations)}


@dataclass(slots=True)
class DecisionSnapshot:
    snapshot_id: str
    created_monotonic_ns: int
    versions: VersionSet
    goal: dict[str, Any]
    observations: list[dict[str, Any]]
    owners: list[dict[str, Any]]

    @classmethod
    def parse(cls, data: Any) -> "DecisionSnapshot":
        raw = _wire_object(data, "snapshot", {"schema_version", "snapshot_id", "created_monotonic_ns", "versions", "goal", "observations", "owners"})
        _version(raw)
        _snapshot_object(raw, "snapshot", set(raw), ("snapshot_id", "created_monotonic_ns", "versions", "goal", "observations", "owners"))
        versions = _parse_nested(VersionSet, raw["versions"], "versions")
        goal_keys = ("task_id", "goal_id", "goal_text_en", "allowed_actions", "parameters")
        goal = _snapshot_object(raw["goal"], "goal", set(goal_keys), goal_keys)
        allowed = require_string_list(goal["allowed_actions"], "goal.allowed_actions")
        for index, action in enumerate(allowed): _id(action, f"goal.allowed_actions[{index}]")
        if len(set(allowed)) != len(allowed): reject(ErrorCode.ENUM_VIOLATION, "goal.allowed_actions", "duplicate action")
        parameters = require_object(goal["parameters"], "goal.parameters")
        for action, params in parameters.items():
            _id(action, f"goal.parameters.{action}")
            if action not in allowed: reject(ErrorCode.UNKNOWN_ACTION, f"goal.parameters.{action}", "action is not allowed")
            require_object(params, f"goal.parameters.{action}")
        parsed_goal = {"task_id": _id(goal["task_id"], "goal.task_id"), "goal_id": _id(goal["goal_id"], "goal.goal_id"),
                       "goal_text_en": validate_goal_text(goal["goal_text_en"], "goal.goal_text_en"),
                       "allowed_actions": allowed, "parameters": _json_copy(parameters)}
        observations = raw["observations"]
        owners = raw["owners"]
        if not isinstance(observations, list) or not isinstance(owners, list):
            reject(ErrorCode.INVALID_TYPE, "observations" if not isinstance(observations, list) else "owners", "must be an array")
        parsed_observations = []
        for index, item in enumerate(observations):
            parsed_observations.append(_parse_nested(ObservationEnvelope, item, f"observations[{index}]" ).to_dict())
        parsed_owners: list[dict[str, Any]] = []
        owner_ids: set[str] = set()
        option_ids: set[str] = set()
        for index, item in enumerate(owners):
            path = f"owners[{index}]"
            record = _snapshot_object(item, path, {"owner", "plugin_generation", "status", "candidates"}, ("owner", "plugin_generation", "status", "candidates"))
            owner = _id(record["owner"], f"{path}.owner")
            generation = _seq(record["plugin_generation"], f"{path}.plugin_generation")
            if owner in owner_ids: reject(ErrorCode.ENUM_VIOLATION, f"{path}.owner", "duplicate owner")
            owner_ids.add(owner)
            if versions.plugin_generations.get(owner) != generation:
                reject(ErrorCode.REVISION_CONFLICT, f"{path}.plugin_generation", "owner generation differs from versions")
            candidates = record["candidates"]
            if not isinstance(candidates, list): reject(ErrorCode.INVALID_TYPE, f"{path}.candidates", "must be an array")
            parsed_candidates = []
            for j, candidate in enumerate(candidates):
                cp = f"{path}.candidates[{j}]"
                entry = _snapshot_object(candidate, cp, {"option_id", "kind", "description", "eligible", "reason_code", "action_id", "command_id"},
                                         ("option_id", "kind", "description", "eligible"))
                option = _id(entry["option_id"], f"{cp}.option_id")
                if option in option_ids: reject(ErrorCode.ENUM_VIOLATION, f"{cp}.option_id", "duplicate option")
                option_ids.add(option)
                kind = _id(entry["kind"], f"{cp}.kind")
                if kind not in CANDIDATE_KINDS: reject(ErrorCode.ENUM_VIOLATION, f"{cp}.kind", "unknown candidate kind")
                action = _id(entry["action_id"], f"{cp}.action_id") if "action_id" in entry else None
                command = _id(entry["command_id"], f"{cp}.command_id") if "command_id" in entry else None
                if kind == "start":
                    if action is None: reject(ErrorCode.MISSING_FIELD, f"{cp}.action_id", "start requires action")
                    if action not in allowed: reject(ErrorCode.UNKNOWN_ACTION, f"{cp}.action_id", "action is not allowed")
                    if command is not None: reject(ErrorCode.INVALID_TYPE, f"{cp}.command_id", "start has no existing command")
                elif kind in {"cancel", "pause", "resume", "keep"}:
                    if command is None: reject(ErrorCode.MISSING_FIELD, f"{cp}.command_id", "existing command is required")
                elif action is not None or command is not None:
                    reject(ErrorCode.INVALID_TYPE, cp, "wait/replan cannot carry an action or command")
                if action is not None and action.split(".", 1)[0] != owner:
                    reject(ErrorCode.OWNER_MISMATCH, f"{cp}.action_id", "action belongs to another owner")
                result = {"option_id": option, "kind": kind, "description": require_text(entry["description"], f"{cp}.description", max_len=4096),
                          "eligible": require_bool(entry["eligible"], f"{cp}.eligible"), "reason_code": _optional_text(entry.get("reason_code", ""), f"{cp}.reason_code")}
                if action is not None: result["action_id"] = action
                if command is not None: result["command_id"] = command
                parsed_candidates.append(result)
            parsed_owners.append({"owner": owner, "plugin_generation": generation, "status": _id(record["status"], f"{path}.status"),
                                  "candidates": parsed_candidates})
        return cls(_id(raw["snapshot_id"], "snapshot_id"),
                   require_int(raw["created_monotonic_ns"], "created_monotonic_ns", minimum=0, maximum=MAX_MONOTONIC_NS),
                   versions, parsed_goal, parsed_observations, parsed_owners)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "snapshot_id": self.snapshot_id, "created_monotonic_ns": self.created_monotonic_ns,
                "versions": self.versions.to_dict(), "goal": _json_copy(self.goal), "observations": _json_copy(self.observations),
                "owners": _json_copy(self.owners)}


@dataclass(slots=True)
class BackendDecision:
    snapshot_id: str
    versions: VersionSet
    backend: str
    model: str
    elapsed_ms: int | float
    choices: list[dict[str, Any]]

    @classmethod
    def parse(cls, data: Any) -> "BackendDecision":
        raw = _wire_object(data, "backend_decision", {"schema_version", "snapshot_id", "versions", "backend", "model", "elapsed_ms", "choices"})
        _version(raw)
        _snapshot_object(raw, "backend_decision", set(raw), ("snapshot_id", "versions", "backend", "model", "elapsed_ms", "choices"))
        choices = raw["choices"]
        if not isinstance(choices, list): reject(ErrorCode.INVALID_TYPE, "choices", "must be an array")
        parsed: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(choices):
            path = f"choices[{index}]"
            choice = _snapshot_object(item, path, {"owner", "option_id", "confidence", "probabilities"}, ("owner", "option_id"))
            owner = _id(choice["owner"], f"{path}.owner")
            if owner in seen: reject(ErrorCode.ENUM_VIOLATION, f"{path}.owner", "duplicate owner choice")
            seen.add(owner)
            result: dict[str, Any] = {"owner": owner, "option_id": _id(choice["option_id"], f"{path}.option_id")}
            if "confidence" in choice: result["confidence"] = _nonnegative_number(choice["confidence"], f"{path}.confidence", 1)
            if "probabilities" in choice:
                probabilities = require_object(choice["probabilities"], f"{path}.probabilities")
                result["probabilities"] = {_id(option, f"{path}.probabilities.{option}"): _nonnegative_number(probability, f"{path}.probabilities.{option}", 1)
                                           for option, probability in probabilities.items()}
            parsed.append(result)
        return cls(_id(raw["snapshot_id"], "snapshot_id"), _parse_nested(VersionSet, raw["versions"], "versions"),
                   _id(raw["backend"], "backend"), _id(raw["model"], "model"),
                   _nonnegative_number(raw["elapsed_ms"], "elapsed_ms"), parsed)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "snapshot_id": self.snapshot_id, "versions": self.versions.to_dict(),
                "backend": self.backend, "model": self.model, "elapsed_ms": self.elapsed_ms, "choices": _json_copy(self.choices)}


def validate_backend_selection(snapshot: DecisionSnapshot, decision: BackendDecision, current_versions: VersionSet) -> list[dict[str, Any]]:
    """Revalidate mutable inputs, then select only current eligible options."""
    for value, path in ((snapshot, "snapshot"), (decision, "decision"), (current_versions, "current_versions")):
        if not callable(getattr(value, "to_dict", None)):
            reject(ErrorCode.INVALID_TYPE, path, "must be a contract model")
    for value, path in ((snapshot, "snapshot.versions"), (decision, "decision.versions")):
        versions = getattr(value, "versions", None)
        if not callable(getattr(versions, "to_dict", None)):
            reject(ErrorCode.INVALID_TYPE, path, "must be a version set")
    snapshot_versions = getattr(snapshot, "versions")
    decision_versions = getattr(decision, "versions")
    for versions, path in ((snapshot_versions, "snapshot.versions.plugin_generations"),
                           (decision_versions, "decision.versions.plugin_generations"),
                           (current_versions, "current_versions.plugin_generations")):
        if not isinstance(getattr(versions, "plugin_generations", None), dict):
            reject(ErrorCode.INVALID_TYPE, path, "must be an object")
    snapshot = _parse_nested(DecisionSnapshot, snapshot.to_dict(), "snapshot")
    decision = _parse_nested(BackendDecision, decision.to_dict(), "decision")
    current_versions = _parse_nested(VersionSet, current_versions.to_dict(), "current_versions")
    if decision.snapshot_id != snapshot.snapshot_id:
        reject(ErrorCode.REVISION_CONFLICT, "decision.snapshot_id", "result belongs to another snapshot")
    if decision.versions.to_dict() != snapshot.versions.to_dict() or current_versions.to_dict() != snapshot.versions.to_dict():
        reject(ErrorCode.REVISION_CONFLICT, "decision.versions", "decision or trusted versions changed")
    owners = {item["owner"]: item for item in snapshot.owners}
    if len(decision.choices) != len(owners): reject(ErrorCode.MISSING_FIELD, "decision.choices", "exactly one choice per owner is required")
    selected: list[dict[str, Any]] = []
    selected_owners: set[str] = set()
    for index, choice in enumerate(decision.choices):
        path = f"decision.choices[{index}]"
        if choice["owner"] in selected_owners:
            reject(ErrorCode.ENUM_VIOLATION, f"{path}.owner", "duplicate owner choice")
        selected_owners.add(choice["owner"])
        owner = owners.get(choice["owner"])
        if owner is None: reject(ErrorCode.OWNER_MISMATCH, f"{path}.owner", "owner not in snapshot")
        eligible = {item["option_id"]: item for item in owner["candidates"] if item["eligible"]}
        if choice["option_id"] not in eligible:
            reject(ErrorCode.UNKNOWN_ACTION, f"{path}.option_id", "option is unknown or ineligible for owner")
        if "probabilities" in choice:
            probabilities = choice["probabilities"]
            if not probabilities: reject(ErrorCode.LENGTH_VIOLATION, f"{path}.probabilities", "distribution must not be empty")
            for option in probabilities:
                if option not in eligible: reject(ErrorCode.UNKNOWN_ACTION, f"{path}.probabilities.{option}", "option is not eligible for owner")
            if not math.isclose(math.fsum(probabilities.values()), 1.0, rel_tol=0.0, abs_tol=PROBABILITY_SUM_TOLERANCE):
                reject(ErrorCode.RANGE_VIOLATION, f"{path}.probabilities", "probabilities must sum to one")
        selected.append(_json_copy(eligible[choice["option_id"]]))
    if selected_owners != set(owners):
        reject(ErrorCode.MISSING_FIELD, "decision.choices", "exactly one choice per owner is required")
    return selected


# --------------------------------------------------------------------------
# Idempotency helper shared with the transport layer.
# --------------------------------------------------------------------------


class DecisionIdempotency:
    """request_id idempotency: same payload replays, different payload conflicts."""

    def __init__(self) -> None:
        self._registry = IdempotencyRegistry()

    def resolve(self, request_id: str, payload: dict[str, Any]) -> str:
        budget = measure_json_budget(payload, "idempotency.payload")
        if budget is not None:
            reject(budget, "idempotency.payload", "payload exceeds JSON validation budget")
        non_finite = find_non_finite(payload, "idempotency.payload")
        if non_finite is not None:
            raise ContractError(non_finite)
        return self._registry.resolve(request_id, _json_copy(payload))


# --------------------------------------------------------------------------
# Payload-level entry point used by both sides and by golden fixtures.
# --------------------------------------------------------------------------


def parse_request(method: Any, payload: Any) -> Any:
    """Parse a decision payload without raising a Python type error for method."""
    if not isinstance(method, str):
        reject(ErrorCode.INVALID_TYPE, "method", "must be a bounded non-empty string")
    if not method:
        reject(ErrorCode.EMPTY_STRING, "method", "must not be empty")
    if len(method) > MAX_ID_LEN:
        reject(ErrorCode.TEXT_TOO_LONG, "method", f"must not exceed {MAX_ID_LEN} characters")
    if method not in DECISION_METHODS:
        reject(UNSUPPORTED_METHOD, "method", f"unsupported method: {method}")
    if method == METHOD_GOAL_SUBMIT:
        return GoalSubmit.parse(payload)
    if method == METHOD_GOAL_CANCEL:
        return GoalCancel.parse(payload)
    if method == METHOD_GOAL_RENEW:
        return GoalRenew.parse(payload)
    if method == METHOD_STATE_GET:
        return _parse_readonly_payload(payload, "state_request")
    if method == METHOD_EVENTS_GET:
        return EventsRequest.parse(payload)
    if method == METHOD_CONTEXT_GET:
        return _parse_readonly_payload(payload, "context_request")
    return Feedback.parse(payload)


__all__ = [
    "DECISION_METHODS",
    "DISTINCT_FACTS",
    "DecisionIdempotency",
    "DecisionSnapshot",
    "BackendDecision",
    "ObservationEnvelope",
    "VersionSet",
    "validate_backend_selection",
    "MAX_MONOTONIC_NS",
    "PROBABILITY_SUM_TOLERANCE",
    "DecisionState",
    "EventsReply",
    "EventsRequest",
    "FACT_ACTION_COMPLETED",
    "FACT_GOAL_ACTIVE",
    "FACT_SUBMIT_ACCEPTED",
    "FACT_TRANSPORT_ACK",
    "Feedback",
    "GoalCancel",
    "GoalRenew",
    "GoalSubmit",
    "GoalSubmitResult",
    "METHOD_CONTEXT_GET",
    "METHOD_EVENTS_GET",
    "METHOD_FEEDBACK",
    "METHOD_GOAL_CANCEL",
    "METHOD_GOAL_RENEW",
    "METHOD_GOAL_SUBMIT",
    "METHOD_STATE_GET",
    "UNSUPPORTED_METHOD",
    "check_ex_session",
    "check_revision",
    "err",
    "events_reply",
    "parse_request",
]
