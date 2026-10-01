"""B00 frozen cross-endpoint contracts (canonical, self-contained).

GENERATED FILE - do not edit by hand; edit the two source modules listed below
and rerun ``python scripts/build_contracts.py``.

Sources
-------
    astrbot_ex/core/actions/models.py    command, status, action declaration v2
    astrbot_ex/core/decision/models.py   goal submit/cancel/renew, state, feedback

This module imports only the Python standard library, so a byte-identical copy
can live in the A.E.B plugin directory and both sides are then guaranteed to
parse the same payload into the same result:

    AstrBotEX:  astrbot_ex/core/contracts.py
    A.E.B:      astrbot_plugin_astrbotex_interaction/task_contracts.py

Frozen surface
--------------
* transport: ``astrbotex-zmq`` envelope version 1, unchanged. Business payloads
  carry ``schema_version = 1``.
* methods: the seven ``decision.*`` methods.
* phases: accepted / pending_cancel / active / blocked / rejected. Only
  ``active`` enters decision.
* action statuses: admitted -> accepted -> running -> terminal. Terminal states
  are mutually exclusive and never move backwards. ``canceled`` requires plugin
  stop evidence.
* action declaration v2; v1 legacy topic actions stay isolated both ways.

See ``docs/DECISION-CONTRACT.md`` for parser-backed field/state/error tables
and clearly labeled planned B02/B04/B08 surfaces.
"""

# NOTE: the A.E.B mirror is executed as a top-level module, so this generated
# file must not contain package-relative imports.


from __future__ import annotations

import copy
import json
import math
import re
from decimal import Decimal
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1
ACTION_API_VERSION_V2 = 2

MAX_GOAL_TEXT_LEN = 4096

# B00 §2.4: a pure-action plugin declares this capability instead of borrowing
# ``motion_bridge``. B02 must register it in local_plugins.ALLOWED_PLUGIN_TYPES,
# RUNTIME_KIND_BY_CAPABILITY and DEFAULT_CATEGORY_BY_CAPABILITY.
ACTION_OWNER_CAPABILITY = "action_owner"
ACTION_OWNER_RUNTIME_KIND = "action"
ACTION_OWNER_CATEGORY = "control"


class ErrorCode:
    """Frozen rejection codes. Strings so they survive JSON round trips."""

    OK = "ok"
    INVALID_TYPE = "invalid_type"
    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    EMPTY_STRING = "empty_string"
    TEXT_TOO_LONG = "text_too_long"
    NOT_ENGLISH_MARKER = "not_english_marker"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    UNSUPPORTED_ACTION_API_VERSION = "unsupported_action_api_version"
    UNKNOWN_ACTION = "unknown_action"
    OWNER_MISMATCH = "owner_mismatch"
    MISSING_PARAMS = "missing_params"
    NON_FINITE_NUMBER = "non_finite_number"
    NEGATIVE_TTL = "negative_ttl"
    NON_POSITIVE_LEASE = "non_positive_lease"
    DUPLICATE_REQUEST_ID_CONFLICT = "duplicate_request_id_conflict"
    STALE_EX_SESSION = "stale_ex_session"
    REVISION_CONFLICT = "revision_conflict"
    ILLEGAL_TRANSITION = "illegal_transition"
    CANCEL_UNSUPPORTED = "cancel_unsupported"
    LEGACY_ISOLATION = "legacy_isolation"
    SCHEMA_UNSUPPORTED_KEYWORD = "schema_unsupported_keyword"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    DUPLICATE_ACTION_ID = "duplicate_action_id"
    INVALID_ACTION_ID = "invalid_action_id"
    RESOURCE_CONFLICT = "resource_conflict"
    ENUM_VIOLATION = "enum_violation"
    RANGE_VIOLATION = "range_violation"
    LENGTH_VIOLATION = "length_violation"
    PATTERN_VIOLATION = "pattern_violation"
    FORMAT_VIOLATION = "format_violation"
    VALUE_BUDGET_EXCEEDED = "value_budget_exceeded"
    SCHEMA_DEPTH_EXCEEDED = "schema_depth_exceeded"
    SCHEMA_BOUNDS_CONFLICT = "schema_bounds_conflict"


MAX_JSON_INTEGER_DIGITS = 4096
MAX_PATTERN_LENGTH = 256
MAX_ID_LEN = 256
MAX_SEQUENCE = 2**53 - 1
MAX_LEASE_MS = 600_000


@dataclass(slots=True)
class ValidationError:
    """A single rejection reason. ``path`` is a dotted/bracketed JSON pointer."""

    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "path": self.path, "message": self.message}


class ContractError(ValueError):
    """Raised by the contract parser when a payload must be rejected."""

    def __init__(self, error: ValidationError) -> None:
        super().__init__(f"{error.code}: {error.path}: {error.message}")
        self.error = error

    @property
    def code(self) -> str:
        return self.error.code


def err(code: str, path: str, message: str) -> ValidationError:
    return ValidationError(code=code, path=path, message=message)


def reject(code: str, path: str, message: str) -> None:
    raise ContractError(err(code, path, message))


# --------------------------------------------------------------------------
# Identity comes from the trusted connection mapping, never from the payload.
# --------------------------------------------------------------------------


def resolve_identity(connection_map: dict[str, str], connection_id: str) -> str | None:
    """Return the trusted owner for a connection, ignoring any payload claim."""
    return connection_map.get(connection_id)


# --------------------------------------------------------------------------
# Strict value checks. Non-finite numbers are refused everywhere.
# --------------------------------------------------------------------------


def _string_json_size(value: str, limit: int | None = None) -> int | None:
    """Return JSON UTF-8 size, stopping when the bounded limit is exceeded."""
    size = 2
    if limit is not None and size > limit:
        return -1
    for char in value:
        codepoint = ord(char)
        if codepoint < 0x20:
            size += 6
        elif char in ('"', "\\"):
            size += 2
        else:
            try:
                size += len(char.encode("utf-8"))
            except UnicodeEncodeError:
                return None
        if limit is not None and size > limit:
            return -1
    return size


def _scalar_json_size(value: Any, limit: int | None = None) -> int | None:
    if value is None:
        size = 4
    elif isinstance(value, bool):
        size = 4 if value else 5
    elif isinstance(value, str):
        return _string_json_size(value, limit)
    elif isinstance(value, int) and not isinstance(value, bool):
        if value.bit_length() > MAX_JSON_INTEGER_DIGITS * 4:
            return -1
        try:
            digits = len(str(abs(value)))
        except (ValueError, RecursionError):
            return -1
        if digits > MAX_JSON_INTEGER_DIGITS:
            return -1
        size = digits + (1 if value < 0 else 0)
    elif isinstance(value, float):
        if not math.isfinite(value):
            return None
        size = len(repr(value))
    else:
        return None
    if limit is not None and size > limit:
        return -1
    return size


def find_non_finite(value: Any, path: str = "$", *, depth: int = 0) -> ValidationError | None:
    """Find invalid JSON values with bounded, lazy, cycle-safe traversal."""
    stack: list[tuple[str, Any, int, str, int]] = [
        ("visit", value, depth, path, 0)
    ]
    active: set[int] = set()
    nodes = 0
    total_bytes = 0

    while stack:
        tag, item, item_depth, item_path, index = stack.pop()

        if tag == "leave":
            active.discard(item)
            total_bytes += 1
            if total_bytes > MAX_VALIDATION_BYTES:
                return err(
                    ErrorCode.VALUE_BUDGET_EXCEEDED,
                    item_path,
                    "value exceeds byte budget",
                )
            continue

        if tag == "dict_iter":
            iterator = item
            try:
                key, child = next(iterator)
            except StopIteration:
                continue
            stack.append(("dict_iter", iterator, item_depth, item_path, index + 1))
            if index:
                total_bytes += 1
            if not isinstance(key, str):
                return err(
                    ErrorCode.INVALID_TYPE,
                    item_path,
                    "object keys must be strings",
                )
            key_bytes = _string_json_size(
                key,
                MAX_VALIDATION_BYTES - total_bytes,
            )
            if key_bytes == -1:
                return err(
                    ErrorCode.VALUE_BUDGET_EXCEEDED,
                    item_path,
                    "value exceeds byte budget",
                )
            if key_bytes is None:
                return err(
                    ErrorCode.INVALID_TYPE,
                    item_path,
                    "object key is not valid JSON",
                )
            total_bytes += key_bytes + 1
            if total_bytes > MAX_VALIDATION_BYTES:
                return err(
                    ErrorCode.VALUE_BUDGET_EXCEEDED,
                    item_path,
                    "value exceeds byte budget",
                )
            stack.append(("visit", child, item_depth, _child_path(item_path, f".{key}"), 0))
            continue

        if tag == "list_iter":
            iterator = item
            try:
                child_index, child = next(iterator)
            except StopIteration:
                continue
            stack.append(("list_iter", iterator, item_depth, item_path, index + 1))
            if index:
                total_bytes += 1
            stack.append(
                ("visit", child, item_depth, _child_path(item_path, f"[{child_index}]"), 0)
            )
            continue

        nodes += 1
        if nodes > MAX_VALIDATION_NODES:
            return err(
                ErrorCode.VALUE_BUDGET_EXCEEDED,
                item_path,
                "value exceeds node budget",
            )
        if item_depth > MAX_VALIDATION_DEPTH:
            return err(
                ErrorCode.SCHEMA_DEPTH_EXCEEDED,
                item_path,
                "value nesting exceeds maximum depth",
            )
        if isinstance(item, float) and not math.isfinite(item):
            return err(
                ErrorCode.NON_FINITE_NUMBER,
                item_path,
                "number must be finite (no NaN or Infinity)",
            )

        if isinstance(item, dict):
            item_id = id(item)
            if item_id in active:
                return err(
                    ErrorCode.INVALID_TYPE,
                    item_path,
                    "value contains a cycle",
                )
            active.add(item_id)
            total_bytes += 1
            if total_bytes > MAX_VALIDATION_BYTES:
                return err(
                    ErrorCode.VALUE_BUDGET_EXCEEDED,
                    item_path,
                    "value exceeds byte budget",
                )
            stack.append(("leave", item_id, item_depth, item_path, 0))
            stack.append(
                ("dict_iter", iter(item.items()), item_depth + 1, item_path, 0)
            )
            continue

        if isinstance(item, list):
            item_id = id(item)
            if item_id in active:
                return err(
                    ErrorCode.INVALID_TYPE,
                    item_path,
                    "value contains a cycle",
                )
            active.add(item_id)
            total_bytes += 1
            if total_bytes > MAX_VALIDATION_BYTES:
                return err(
                    ErrorCode.VALUE_BUDGET_EXCEEDED,
                    item_path,
                    "value exceeds byte budget",
                )
            stack.append(("leave", item_id, item_depth, item_path, 0))
            stack.append(
                ("list_iter", iter(enumerate(item)), item_depth + 1, item_path, 0)
            )
            continue

        scalar_bytes = _scalar_json_size(
            item,
            MAX_VALIDATION_BYTES - total_bytes,
        )
        if scalar_bytes == -1:
            return err(
                ErrorCode.VALUE_BUDGET_EXCEEDED,
                item_path,
                "value exceeds byte budget",
            )
        if scalar_bytes is None:
            return err(
                ErrorCode.INVALID_TYPE,
                item_path,
                "value is not valid JSON",
            )
        total_bytes += scalar_bytes
        if total_bytes > MAX_VALIDATION_BYTES:
            return err(
                ErrorCode.VALUE_BUDGET_EXCEEDED,
                item_path,
                "value exceeds byte budget",
            )

    return None


def require_object(value: Any, path: str) -> dict[str, Any]:
    if isinstance(value, bool) or not isinstance(value, dict):
        reject(ErrorCode.INVALID_TYPE, path, "must be an object")
    return value


def require_text(value: Any, path: str, *, max_len: int | None = None) -> str:
    if not isinstance(value, str):
        reject(ErrorCode.INVALID_TYPE, path, "must be a string")
    text = value.strip()
    if not text:
        reject(ErrorCode.EMPTY_STRING, path, "must not be empty")
    if max_len is not None and len(text) > max_len:
        reject(ErrorCode.TEXT_TOO_LONG, path, f"must not exceed {max_len} characters")
    return text


def require_int(value: Any, path: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        reject(ErrorCode.INVALID_TYPE, path, "must be an integer")
    if minimum is not None and value < minimum:
        reject(ErrorCode.RANGE_VIOLATION, path, f"must be >= {minimum}")
    if maximum is not None and value > maximum:
        reject(ErrorCode.RANGE_VIOLATION, path, f"must be <= {maximum}")
    return value


def require_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        reject(ErrorCode.INVALID_TYPE, path, "must be a boolean")
    return value


def require_string_list(value: Any, path: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list):
        reject(ErrorCode.INVALID_TYPE, path, "must be an array")
    if not value and not allow_empty:
        reject(ErrorCode.LENGTH_VIOLATION, path, "must not be empty")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(require_text(item, f"{path}[{index}]"))
    return result


def require_lease(value: Any, path: str) -> int:
    """TTL/lease must be a strictly positive integer number of milliseconds."""
    if isinstance(value, bool) or not isinstance(value, int):
        reject(ErrorCode.INVALID_TYPE, path, "must be an integer number of milliseconds")
    if value < 0:
        reject(ErrorCode.NEGATIVE_TTL, path, "must not be negative")
    if value == 0:
        reject(ErrorCode.NON_POSITIVE_LEASE, path, "must be greater than zero")
    if value > MAX_LEASE_MS:
        reject(ErrorCode.RANGE_VIOLATION, path, f"must be <= {MAX_LEASE_MS}")
    return value


def require_id(value: Any, path: str) -> str:
    return require_text(value, path, max_len=MAX_ID_LEN)


def require_sequence(value: Any, path: str, *, minimum: int = 0) -> int:
    return require_int(value, path, minimum=minimum, maximum=MAX_SEQUENCE)


def _optional_action_text(value: Any, path: str) -> str:
    if not isinstance(value, str):
        reject(ErrorCode.INVALID_TYPE, path, "must be a string")
    if len(value) > MAX_ID_LEN:
        reject(ErrorCode.TEXT_TOO_LONG, path, f"must not exceed {MAX_ID_LEN} characters")
    return value


def _unique_strings(value: Any, path: str, *, allow_empty: bool = True) -> list[str]:
    items = require_string_list(value, path, allow_empty=allow_empty)
    for index, item in enumerate(items):
        require_id(item, f"{path}[{index}]")
    if len(items) != len(set(items)):
        reject(ErrorCode.SCHEMA_BOUNDS_CONFLICT, path, "items must be unique")
    return items


# --------------------------------------------------------------------------
# Validation budgets (B00 review finding 2). Every recursive walk over
# attacker-supplied data is bounded, and exceeding a bound is reported as a
# contract error, never as RecursionError/TypeError/MemoryError.
# --------------------------------------------------------------------------

MAX_VALIDATION_DEPTH = 32
MAX_VALIDATION_NODES = 20_000
MAX_VALIDATION_BYTES = 1_048_576
MAX_VALIDATION_WORK = MAX_VALIDATION_NODES
MAX_DIAGNOSTIC_PATH_LEN = 1024


def _child_path(path: str, suffix: str) -> str:
    return ((path + suffix) if path or not suffix.startswith(".") else suffix[1:])[:MAX_DIAGNOSTIC_PATH_LEN]


def measure_json_budget(value: Any, path: str = "$") -> str | None:
    """Return the first budget violation without serializing or copying input."""
    nodes = 0
    total_bytes = 0
    active: set[int] = set()
    stack: list[tuple[str, Any, int, str, int]] = [
        ("visit", value, 0, path, 0)
    ]

    while stack:
        tag, item, depth, item_path, index = stack.pop()

        if tag == "leave":
            active.discard(item)
            total_bytes += 1
            if total_bytes > MAX_VALIDATION_BYTES:
                return ErrorCode.VALUE_BUDGET_EXCEEDED
            continue

        if tag == "dict_iter":
            iterator = item
            try:
                key, child = next(iterator)
            except StopIteration:
                continue
            stack.append(("dict_iter", iterator, depth, item_path, index + 1))
            if index:
                total_bytes += 1
            if not isinstance(key, str):
                return ErrorCode.INVALID_TYPE
            key_bytes = _string_json_size(
                key,
                MAX_VALIDATION_BYTES - total_bytes,
            )
            if key_bytes == -1:
                return ErrorCode.VALUE_BUDGET_EXCEEDED
            if key_bytes is None:
                return ErrorCode.INVALID_TYPE
            total_bytes += key_bytes + 1
            if total_bytes > MAX_VALIDATION_BYTES:
                return ErrorCode.VALUE_BUDGET_EXCEEDED
            stack.append(("visit", child, depth, _child_path(item_path, f".{key}"), 0))
            continue

        if tag == "list_iter":
            iterator = item
            try:
                child_index, child = next(iterator)
            except StopIteration:
                continue
            stack.append(("list_iter", iterator, depth, item_path, index + 1))
            if index:
                total_bytes += 1
            stack.append(("visit", child, depth, _child_path(item_path, f"[{child_index}]"), 0))
            continue

        nodes += 1
        if nodes > MAX_VALIDATION_NODES:
            return ErrorCode.VALUE_BUDGET_EXCEEDED
        if depth > MAX_VALIDATION_DEPTH:
            return ErrorCode.SCHEMA_DEPTH_EXCEEDED

        if isinstance(item, float) and not math.isfinite(item):
            return ErrorCode.NON_FINITE_NUMBER

        if isinstance(item, dict):
            item_id = id(item)
            if item_id in active:
                return ErrorCode.INVALID_TYPE
            active.add(item_id)
            total_bytes += 1
            if total_bytes > MAX_VALIDATION_BYTES:
                return ErrorCode.VALUE_BUDGET_EXCEEDED
            stack.append(("leave", item_id, depth, item_path, 0))
            stack.append(("dict_iter", iter(item.items()), depth + 1, item_path, 0))
            continue

        if isinstance(item, list):
            item_id = id(item)
            if item_id in active:
                return ErrorCode.INVALID_TYPE
            active.add(item_id)
            total_bytes += 1
            if total_bytes > MAX_VALIDATION_BYTES:
                return ErrorCode.VALUE_BUDGET_EXCEEDED
            stack.append(("leave", item_id, depth, item_path, 0))
            stack.append(
                ("list_iter", iter(enumerate(item)), depth + 1, item_path, 0)
            )
            continue

        scalar_bytes = _scalar_json_size(
            item,
            MAX_VALIDATION_BYTES - total_bytes,
        )
        if scalar_bytes == -1:
            return ErrorCode.VALUE_BUDGET_EXCEEDED
        if scalar_bytes is None:
            return ErrorCode.INVALID_TYPE
        total_bytes += scalar_bytes
        if total_bytes > MAX_VALIDATION_BYTES:
            return ErrorCode.VALUE_BUDGET_EXCEEDED

    return None


def _budget_error(value: Any, path: str) -> ValidationError | None:
    code = measure_json_budget(value, path)
    if code is None:
        return None
    if code == ErrorCode.INVALID_TYPE:
        return err(code, path, "value is not valid JSON (cycle or non-string key)")
    return err(code, path, "payload exceeds the validation budget")


def _check_input_budget(value: Any, path: str) -> None:
    budget_error = _budget_error(value, path)
    if budget_error is not None:
        raise ContractError(budget_error)


def _reject_unknown_fields(raw: dict[str, Any], allowed: frozenset[str], path: str) -> None:
    for key in raw:
        if key not in allowed:
            reject(ErrorCode.UNKNOWN_FIELD, f"{path}.{key}", f"unknown field: {key}")


# --------------------------------------------------------------------------
# Strict-only English guidance. Deliberately NOT an ASCII gate: proper nouns
# may contain non-ASCII characters and must not be rejected by the parser.
# --------------------------------------------------------------------------

_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")


def goal_text_marker(text: str) -> ValidationError | None:
    """Advisory marker only. Callers must not use this to reject a goal.

    Returns a marker when the text looks non-English for skill/eval reporting.
    Never raises: the language requirement is enforced by skill and evaluation,
    not by the parser.
    """
    if _NON_ASCII_RE.search(text) and not _ASCII_LETTER_RE.search(text):
        return err(
            ErrorCode.NOT_ENGLISH_MARKER,
            "goal_text_en",
            "advisory: no ASCII letters present; language is enforced by skill, not the parser",
        )
    return None


def validate_goal_text(text: Any, path: str = "goal_text_en") -> str:
    """Only emptiness and length are parsed. Language is not an ASCII check."""
    return require_text(text, path, max_len=MAX_GOAL_TEXT_LEN)


# --------------------------------------------------------------------------
# Supported JSON Schema subset. Unknown keywords are refused rather than
# silently ignored, so a manifest can never appear stricter than it is.
# --------------------------------------------------------------------------

_NUMERIC_KEYS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf")
_VALUELESS_PASSTHROUGH = ("description", "title")
_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "enum",
        "items",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
        "pattern",
        "format",
        *_NUMERIC_KEYS,
        *_VALUELESS_PASSTHROUGH,
    }
)

_SUPPORTED_FORMATS = frozenset({"uuid"})


def _number_is_finite(value: Any) -> bool:
    """Check JSON numeric finiteness without coercing huge integers to float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return not isinstance(value, float) or math.isfinite(value)


def _check_schema_supported(
    schema: Any,
    path: str,
    errors: list[ValidationError],
    depth: int = 0,
    visited: set[int] | None = None,
) -> None:
    """Validate the bounded JSON Schema subset without recursive calls."""
    del depth
    seen = visited if visited is not None else set()
    stack: list[tuple[Any, str, bool]] = [(schema, path, False)]
    while stack:
        current, current_path, leaving = stack.pop()
        if leaving:
            seen.discard(id(current))
            continue
        if not isinstance(current, dict):
            errors.append(err(ErrorCode.INVALID_TYPE, current_path, "schema must be an object"))
            continue
        schema_id = id(current)
        if schema_id in seen:
            errors.append(err(ErrorCode.INVALID_TYPE, current_path, "schema contains a cycle"))
            continue
        seen.add(schema_id)
        stack.append((current, current_path, True))

        for key in current:
            if not isinstance(key, str):
                errors.append(err(ErrorCode.INVALID_TYPE, current_path, "schema keyword must be a string"))
            elif key not in _SCHEMA_KEYWORDS:
                errors.append(err(ErrorCode.SCHEMA_UNSUPPORTED_KEYWORD, f"{current_path}.{key}", f"unsupported schema keyword: {key}"))
            elif current[key] is None:
                errors.append(err(ErrorCode.INVALID_TYPE, f"{current_path}.{key}", f"{key} must not be null"))

        schema_type = current.get("type")
        if schema_type is not None:
            if not isinstance(schema_type, str):
                errors.append(err(ErrorCode.INVALID_TYPE, f"{current_path}.type", "type must be a string"))
            elif schema_type not in {"object", "array", "string", "number", "integer", "boolean", "null"}:
                errors.append(err(ErrorCode.SCHEMA_UNSUPPORTED_KEYWORD, f"{current_path}.type", f"unsupported type: {schema_type}"))

        enum = current.get("enum")
        if enum is not None:
            if not isinstance(enum, list):
                errors.append(err(ErrorCode.INVALID_TYPE, f"{current_path}.enum", "enum must be an array"))
            elif not enum:
                errors.append(err(ErrorCode.LENGTH_VIOLATION, f"{current_path}.enum", "enum must not be empty"))
            else:
                enum_error = find_non_finite(enum, f"{current_path}.enum")
                if enum_error is not None:
                    errors.append(enum_error)

        for key in _NUMERIC_KEYS:
            value = current.get(key)
            if value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(err(ErrorCode.INVALID_TYPE, f"{current_path}.{key}", f"{key} must be a number"))
            elif not _number_is_finite(value):
                errors.append(err(ErrorCode.NON_FINITE_NUMBER, f"{current_path}.{key}", f"{key} must be finite"))
            elif key == "multipleOf" and value <= 0:
                errors.append(err(ErrorCode.RANGE_VIOLATION, f"{current_path}.{key}", "multipleOf must be positive"))

        numeric_values = [
            current[key]
            for key in _NUMERIC_KEYS
            if key in current and _number_is_finite(current[key])
        ]
        lower = [
            (current[key], key == "exclusiveMinimum")
            for key in ("minimum", "exclusiveMinimum")
            if key in current and _number_is_finite(current[key])
        ]
        upper = [
            (current[key], key == "exclusiveMaximum")
            for key in ("maximum", "exclusiveMaximum")
            if key in current and _number_is_finite(current[key])
        ]
        if lower and upper:
            lower_value = max(value for value, _ in lower)
            upper_value = min(value for value, _ in upper)
            lower_open = any(value == lower_value and exclusive for value, exclusive in lower)
            upper_open = any(value == upper_value and exclusive for value, exclusive in upper)
            if lower_value > upper_value or (lower_value == upper_value and (lower_open or upper_open)):
                errors.append(err(ErrorCode.SCHEMA_BOUNDS_CONFLICT, current_path, "numeric bounds are contradictory"))
        if numeric_values and schema_type not in (None, "number", "integer"):
            errors.append(err(ErrorCode.SCHEMA_BOUNDS_CONFLICT, current_path, "numeric constraints require number or integer type"))

        for key in ("minLength", "maxLength", "minItems", "maxItems"):
            value = current.get(key)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(err(ErrorCode.INVALID_TYPE, f"{current_path}.{key}", f"{key} must be an integer"))
            elif value < 0:
                errors.append(err(ErrorCode.RANGE_VIOLATION, f"{current_path}.{key}", f"{key} must be non-negative"))
        min_len, max_len = current.get("minLength"), current.get("maxLength")
        if isinstance(min_len, int) and not isinstance(min_len, bool) and isinstance(max_len, int) and not isinstance(max_len, bool) and min_len > max_len:
            errors.append(err(ErrorCode.SCHEMA_BOUNDS_CONFLICT, current_path, "string length bounds are contradictory"))
        min_items, max_items = current.get("minItems"), current.get("maxItems")
        if isinstance(min_items, int) and not isinstance(min_items, bool) and isinstance(max_items, int) and not isinstance(max_items, bool) and min_items > max_items:
            errors.append(err(ErrorCode.SCHEMA_BOUNDS_CONFLICT, current_path, "array length bounds are contradictory"))
        if min_len is not None or max_len is not None:
            if schema_type not in (None, "string"):
                errors.append(err(ErrorCode.SCHEMA_BOUNDS_CONFLICT, current_path, "string length constraints require string type"))
        if min_items is not None or max_items is not None:
            if schema_type not in (None, "array"):
                errors.append(err(ErrorCode.SCHEMA_BOUNDS_CONFLICT, current_path, "item length constraints require array type"))

        if "uniqueItems" in current:
            if not isinstance(current["uniqueItems"], bool):
                errors.append(err(ErrorCode.INVALID_TYPE, f"{current_path}.uniqueItems", "must be a boolean"))
            if schema_type not in (None, "array"):
                errors.append(err(ErrorCode.SCHEMA_BOUNDS_CONFLICT, current_path, "uniqueItems requires array type"))

        for key in _VALUELESS_PASSTHROUGH:
            if key in current and not isinstance(current[key], str):
                errors.append(err(ErrorCode.INVALID_TYPE, f"{current_path}.{key}", f"{key} must be a string"))

        pattern = current.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str):
                errors.append(err(ErrorCode.INVALID_TYPE, f"{current_path}.pattern", "pattern must be a string"))
            elif len(pattern) > MAX_PATTERN_LENGTH or re.fullmatch(r"\^?[A-Za-z0-9 _.,:/-]*\$?", pattern) is None:
                errors.append(err(ErrorCode.PATTERN_VIOLATION, f"{current_path}.pattern", "pattern is outside the bounded literal subset"))

        if "format" in current:
            fmt = current["format"]
            if not isinstance(fmt, str):
                errors.append(err(ErrorCode.INVALID_TYPE, f"{current_path}.format", "format must be a string"))
            elif fmt not in _SUPPORTED_FORMATS:
                errors.append(err(ErrorCode.SCHEMA_UNSUPPORTED_KEYWORD, f"{current_path}.format", f"unsupported format: {fmt}"))

        required = current.get("required")
        if required is not None:
            if not isinstance(required, list):
                errors.append(err(ErrorCode.INVALID_TYPE, f"{current_path}.required", "required must be an array"))
            else:
                required_seen: set[str] = set()
                for index, name in enumerate(required):
                    if not isinstance(name, str):
                        errors.append(err(ErrorCode.INVALID_TYPE, f"{current_path}.required[{index}]", "required item must be a string"))
                    elif not name:
                        errors.append(err(ErrorCode.EMPTY_STRING, f"{current_path}.required[{index}]", "required item must not be empty"))
                    elif name in required_seen:
                        errors.append(err(ErrorCode.SCHEMA_BOUNDS_CONFLICT, f"{current_path}.required[{index}]", "required items must be unique"))
                    else:
                        required_seen.add(name)
            if schema_type not in (None, "object"):
                errors.append(err(ErrorCode.SCHEMA_BOUNDS_CONFLICT, current_path, "required requires object type"))

        properties = current.get("properties")
        if properties is not None:
            if not isinstance(properties, dict):
                errors.append(err(ErrorCode.INVALID_TYPE, f"{current_path}.properties", "must be an object"))
            else:
                if schema_type not in (None, "object"):
                    errors.append(err(ErrorCode.SCHEMA_BOUNDS_CONFLICT, current_path, "properties requires object type"))
                for name, child in properties.items():
                    if not isinstance(name, str):
                        errors.append(err(ErrorCode.INVALID_TYPE, f"{current_path}.properties", "property names must be strings"))
                    stack.append((child, f"{current_path}.properties.{name}", False))

        items = current.get("items")
        if items is not None:
            if not isinstance(items, dict):
                errors.append(err(ErrorCode.INVALID_TYPE, f"{current_path}.items", "items must be an object schema"))
            else:
                if schema_type not in (None, "array"):
                    errors.append(err(ErrorCode.SCHEMA_BOUNDS_CONFLICT, current_path, "items requires array type"))
                stack.append((items, f"{current_path}.items", False))

        additional = current.get("additionalProperties")
        if additional is not None:
            if not isinstance(additional, (bool, dict)):
                errors.append(err(ErrorCode.INVALID_TYPE, f"{current_path}.additionalProperties", "must be a boolean or an object schema"))
            elif isinstance(additional, dict):
                if schema_type not in (None, "object"):
                    errors.append(err(ErrorCode.SCHEMA_BOUNDS_CONFLICT, current_path, "additionalProperties requires object type"))
                stack.append((additional, f"{current_path}.additionalProperties", False))

        if schema_type is None and any(key not in {"enum", *_VALUELESS_PASSTHROUGH} for key in current):
            errors.append(err(ErrorCode.SCHEMA_UNSUPPORTED_KEYWORD, current_path, "schema without type supports only enum and descriptive metadata"))


def _check_numeric(schema: dict[str, Any], value: int | float, path: str, errors: list[ValidationError]) -> None:
    bounds = (
        ("minimum", lambda a, b: a < b),
        ("maximum", lambda a, b: a > b),
        ("exclusiveMinimum", lambda a, b: a <= b),
        ("exclusiveMaximum", lambda a, b: a >= b),
    )
    for key, bad in bounds:
        bound = schema.get(key)
        if bound is not None:
            if not isinstance(bound, (int, float)) or isinstance(bound, bool):
                errors.append(err(ErrorCode.INVALID_TYPE, f"schema.{key}", f"{key} must be a number, got {type(bound).__name__}"))
                continue
            if not _number_is_finite(bound):
                errors.append(err(ErrorCode.NON_FINITE_NUMBER, f"schema.{key}", f"{key} must be finite"))
                continue
            if bad(value, bound):
                errors.append(err(ErrorCode.RANGE_VIOLATION, path, f"violates {key}={bound}"))
    step = schema.get("multipleOf")
    if step is not None:
        if not isinstance(step, (int, float)) or isinstance(step, bool):
            errors.append(err(ErrorCode.INVALID_TYPE, "schema.multipleOf", f"multipleOf must be a number, got {type(step).__name__}"))
        elif not _number_is_finite(step) or step <= 0:
            errors.append(err(ErrorCode.RANGE_VIOLATION, "schema.multipleOf", "multipleOf must be finite and positive"))
        else:
            try:
                if isinstance(value, int) and isinstance(step, int):
                    valid = value % step == 0
                else:
                    # Float wire values use their decimal JSON representation, not
                    # a rounded binary quotient (0.3 is a multiple of 0.1).
                    # validate_params has already bounded both JSON operands by
                    # digits/bytes; finite floats have bounded decimal exponents.
                    numerator, denominator = Decimal(str(value)).as_integer_ratio()
                    step_numerator, step_denominator = Decimal(str(step)).as_integer_ratio()
                    valid = (numerator * step_denominator) % (step_numerator * denominator) == 0
            except (OverflowError, ZeroDivisionError, ValueError):
                errors.append(err(ErrorCode.RANGE_VIOLATION, path, "multipleOf calculation exceeds supported numeric range"))
                return
            if not valid:
                errors.append(err(ErrorCode.RANGE_VIOLATION, path, f"must be a multiple of {step}"))


def _check_string(schema: dict[str, Any], value: str, path: str, errors: list[ValidationError]) -> None:
    min_len = schema.get("minLength")
    if min_len is not None and isinstance(min_len, int) and not isinstance(min_len, bool):
        if len(value) < min_len:
            errors.append(err(ErrorCode.LENGTH_VIOLATION, path, f"shorter than minLength {min_len}"))

    max_len = schema.get("maxLength")
    if max_len is not None and isinstance(max_len, int) and not isinstance(max_len, bool):
        if len(value) > max_len:
            errors.append(err(ErrorCode.LENGTH_VIOLATION, path, f"longer than maxLength {max_len}"))

    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        try:
            if re.search(pattern, value) is None:
                errors.append(err(ErrorCode.PATTERN_VIOLATION, path, f"does not match pattern {pattern}"))
        except re.error:
            # Schema validation should have caught this, but defend here too
            pass

    fmt = schema.get("format")
    if fmt == "uuid":
        try:
            import uuid as _uuid
            _uuid.UUID(value)
        except (ValueError, AttributeError, TypeError):
            errors.append(err(ErrorCode.FORMAT_VIOLATION, path, "must be a uuid"))


def _json_equal(left: Any, right: Any, work: list[int] | None = None) -> bool:
    """Compare bounded JSON values with JSON number/boolean semantics."""
    stack: list[tuple[str, Any, Any, int, Any, int]] = [
        ("compare", left, right, 0, None, 0)
    ]
    active: set[tuple[int, int]] = set()
    nodes = 0

    while stack:
        tag, current_left, current_right, depth, iterator, index = stack.pop()

        if tag == "leave":
            active.discard((current_left, current_right))
            continue

        if tag == "dict_iter":
            try:
                key, child_left = next(iterator)
            except StopIteration:
                continue
            stack.append(
                ("dict_iter", current_left, current_right, depth, iterator, index + 1)
            )
            if not isinstance(key, str) or key not in current_right:
                return False
            stack.append(
                (
                    "compare",
                    child_left,
                    current_right[key],
                    depth,
                    None,
                    0,
                )
            )
            continue

        if tag == "list_iter":
            try:
                child_index, child_left = next(iterator)
            except StopIteration:
                continue
            stack.append(
                ("list_iter", current_left, current_right, depth, iterator, index + 1)
            )
            stack.append(
                (
                    "compare",
                    child_left,
                    current_right[child_index],
                    depth,
                    None,
                    child_index,
                )
            )
            continue

        nodes += 1
        if work is not None:
            work[0] += 1
            if work[0] > MAX_VALIDATION_WORK:
                return False
        if nodes > MAX_VALIDATION_NODES or depth > MAX_VALIDATION_DEPTH:
            return False

        if isinstance(current_left, bool) or isinstance(current_right, bool):
            if (
                not isinstance(current_left, bool)
                or not isinstance(current_right, bool)
                or current_left != current_right
            ):
                return False
            continue

        left_number = isinstance(current_left, (int, float)) and not isinstance(current_left, bool)
        right_number = isinstance(current_right, (int, float)) and not isinstance(current_right, bool)
        if left_number or right_number:
            if not left_number or not right_number:
                return False
            if isinstance(current_left, float) and not math.isfinite(current_left):
                return False
            if isinstance(current_right, float) and not math.isfinite(current_right):
                return False
            try:
                if current_left != current_right:
                    return False
            except (OverflowError, ValueError):
                return False
            continue

        if current_left is None or current_right is None:
            if current_left is not None or current_right is not None:
                return False
            continue

        if isinstance(current_left, str) or isinstance(current_right, str):
            if (
                not isinstance(current_left, str)
                or not isinstance(current_right, str)
                or current_left != current_right
            ):
                return False
            continue

        if isinstance(current_left, dict) or isinstance(current_right, dict):
            if (
                not isinstance(current_left, dict)
                or not isinstance(current_right, dict)
                or len(current_left) != len(current_right)
            ):
                return False
            pair = (id(current_left), id(current_right))
            if pair in active:
                return False
            active.add(pair)
            stack.append(("leave", pair[0], pair[1], depth, None, 0))
            stack.append(
                (
                    "dict_iter",
                    current_left,
                    current_right,
                    depth + 1,
                    iter(current_left.items()),
                    0,
                )
            )
            continue

        if isinstance(current_left, list) or isinstance(current_right, list):
            if (
                not isinstance(current_left, list)
                or not isinstance(current_right, list)
                or len(current_left) != len(current_right)
            ):
                return False
            pair = (id(current_left), id(current_right))
            if pair in active:
                return False
            active.add(pair)
            stack.append(("leave", pair[0], pair[1], depth, None, 0))
            stack.append(
                (
                    "list_iter",
                    current_left,
                    current_right,
                    depth + 1,
                    iter(enumerate(current_left)),
                    0,
                )
            )
            continue

        return False

    return True


def _validate_node(schema: dict[str, Any], value: Any, path: str, errors: list[ValidationError]) -> None:
    stack = [(schema, value, path)]
    work = [0]
    while stack:
        current, item, item_path = stack.pop()
        work[0] += 1
        if work[0] > MAX_VALIDATION_WORK:
            errors.append(err(ErrorCode.VALUE_BUDGET_EXCEEDED, item_path, "schema validation work exceeded"))
            return
        expected = current.get("type")
        enum = current.get("enum")
        if isinstance(enum, list) and not any(_json_equal(item, allowed, work) for allowed in enum):
            if work[0] > MAX_VALIDATION_WORK:
                errors.append(err(ErrorCode.VALUE_BUDGET_EXCEEDED, item_path, "enum comparison work exceeded"))
                return
            errors.append(err(ErrorCode.ENUM_VIOLATION, item_path, "value is outside enum"))
        if expected is None:
            continue
        if expected == "object":
            if not isinstance(item, dict):
                errors.append(err(ErrorCode.INVALID_TYPE, item_path, "must be an object"))
                continue
            for key in current.get("required", []):
                if key not in item:
                    errors.append(err(ErrorCode.MISSING_FIELD, _child_path(item_path, f".{key}"), "is required"))
            properties = current.get("properties", {})
            for key, child in item.items():
                child_path = _child_path(item_path, f".{key}")
                if key in properties:
                    stack.append((properties[key], child, child_path))
                else:
                    additional = current.get("additionalProperties", True)
                    if additional is False:
                        errors.append(err(ErrorCode.UNKNOWN_FIELD, child_path, "additional property not allowed"))
                    elif isinstance(additional, dict):
                        stack.append((additional, child, child_path))
            continue
        if expected == "array":
            if not isinstance(item, list):
                errors.append(err(ErrorCode.INVALID_TYPE, item_path, "must be an array"))
                continue
            if "minItems" in current and len(item) < current["minItems"]:
                errors.append(err(ErrorCode.LENGTH_VIOLATION, item_path, "fewer than minItems"))
            if "maxItems" in current and len(item) > current["maxItems"]:
                errors.append(err(ErrorCode.LENGTH_VIOLATION, item_path, "more than maxItems"))
            if current.get("uniqueItems"):
                for index, child in enumerate(item):
                    work[0] += index
                    if work[0] > MAX_VALIDATION_WORK:
                        errors.append(err(ErrorCode.VALUE_BUDGET_EXCEEDED, item_path, "uniqueItems work exceeded"))
                        return
                    if any(_json_equal(child, previous, work) for previous in item[:index]):
                        errors.append(err(ErrorCode.ENUM_VIOLATION, _child_path(item_path, f"[{index}]"), "array items must be unique"))
                    if work[0] > MAX_VALIDATION_WORK:
                        errors.append(err(ErrorCode.VALUE_BUDGET_EXCEEDED, item_path, "uniqueItems comparison work exceeded"))
                        return
            child_schema = current.get("items")
            if isinstance(child_schema, dict):
                stack.extend((child_schema, child, _child_path(item_path, f"[{index}]")) for index, child in enumerate(item))
            continue
        if expected == "string":
            if isinstance(item, str):
                _check_string(current, item, item_path, errors)
            else:
                errors.append(err(ErrorCode.INVALID_TYPE, item_path, "must be a string"))
        elif expected == "boolean":
            if not isinstance(item, bool):
                errors.append(err(ErrorCode.INVALID_TYPE, item_path, "must be a boolean"))
        elif expected == "integer":
            if isinstance(item, bool) or not isinstance(item, int):
                errors.append(err(ErrorCode.INVALID_TYPE, item_path, "must be an integer"))
            else:
                _check_numeric(current, item, item_path, errors)
        elif expected == "number":
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                errors.append(err(ErrorCode.INVALID_TYPE, item_path, "must be a number"))
            else:
                _check_numeric(current, item, item_path, errors)
        elif expected == "null" and item is not None:
            errors.append(err(ErrorCode.INVALID_TYPE, item_path, "must be null"))

def validate_params(schema: Any, params: Any, path: str = "params") -> list[ValidationError]:
    """Validate params against the supported schema subset; returns all errors.

    Performs bounded validation:
    1. Budget check (depth, nodes, bytes, cycles, non-finite)
    2. Schema structure validation
    3. Instance validation against schema

    All validation happens without unbounded recursion or serialization.
    """
    errors: list[ValidationError] = []

    # Step 1: Budget check on params before any other processing
    budget_error = _budget_error(params, path)
    if budget_error is not None:
        return [budget_error]

    # Step 2: Validate schema structure
    schema_errors = check_schema_supported(schema)
    if schema_errors:
        return schema_errors

    # Step 3: Type check params root
    if isinstance(params, bool) or not isinstance(params, dict):
        return [err(ErrorCode.INVALID_TYPE, path, "must be an object")]

    # Step 4: Check for non-finite numbers (already bounded)
    non_finite = find_non_finite(params, path)
    if non_finite is not None:
        return [non_finite]

    # Step 5: Validate instance against schema
    _validate_node(schema, params, path, errors)
    return errors


def check_schema_supported(schema: Any) -> list[ValidationError]:
    """Check the schema itself: object root and only known keywords.

    Separate from ``validate_params`` so a caller can validate a manifest schema
    without supplying instance params (which would otherwise report the required
    fields as missing).
    """
    errors: list[ValidationError] = []

    # Budget check on schema itself
    budget_code = measure_json_budget(schema, "schema")
    if budget_code is not None:
        if budget_code == ErrorCode.INVALID_TYPE:
            return [err(budget_code, "schema", "schema is not JSON-serializable (cycle or non-string key)")]
        return [err(budget_code, "schema", "schema exceeds the validation budget")]

    if not isinstance(schema, dict):
        return [err(ErrorCode.INVALID_TYPE, "schema", "schema must be an object")]
    _check_schema_supported(schema, "schema", errors)
    if errors:
        return errors
    if schema.get("type") != "object":
        return [err(ErrorCode.INVALID_TYPE, "schema.type", "action schema must be an object schema")]
    return errors


# --------------------------------------------------------------------------
# Command operations and the action state machine.
# --------------------------------------------------------------------------


class CommandOperation:
    START = "start"
    CANCEL = "cancel"
    PAUSE = "pause"
    RESUME = "resume"


ALL_OPERATIONS = frozenset(
    {CommandOperation.START, CommandOperation.CANCEL, CommandOperation.PAUSE, CommandOperation.RESUME}
)


class ActionStatus:
    ADMITTED = "admitted"
    REJECTED = "rejected"
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"


TERMINAL_STATUSES = frozenset(
    {
        ActionStatus.REJECTED,
        ActionStatus.SUCCEEDED,
        ActionStatus.FAILED,
        ActionStatus.CANCELED,
        ActionStatus.TIMED_OUT,
        ActionStatus.UNKNOWN,
    }
)

# Terminal states are mutually exclusive and cannot be left. Progress may be
# re-reported (idempotent) but may never overwrite a terminal state.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    ActionStatus.ADMITTED: frozenset(
        {
            ActionStatus.ACCEPTED,
            ActionStatus.REJECTED,
            ActionStatus.CANCELED,
            ActionStatus.TIMED_OUT,
            ActionStatus.UNKNOWN,
        }
    ),
    ActionStatus.ACCEPTED: frozenset(
        {
            ActionStatus.RUNNING,
            ActionStatus.SUCCEEDED,
            ActionStatus.FAILED,
            ActionStatus.CANCELED,
            ActionStatus.TIMED_OUT,
            ActionStatus.UNKNOWN,
        }
    ),
    ActionStatus.RUNNING: frozenset(
        {
            ActionStatus.SUCCEEDED,
            ActionStatus.FAILED,
            ActionStatus.CANCELED,
            ActionStatus.TIMED_OUT,
            ActionStatus.UNKNOWN,
        }
    ),
    ActionStatus.REJECTED: frozenset(),
    ActionStatus.SUCCEEDED: frozenset(),
    ActionStatus.FAILED: frozenset(),
    ActionStatus.CANCELED: frozenset(),
    ActionStatus.TIMED_OUT: frozenset(),
    ActionStatus.UNKNOWN: frozenset(),
}


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def can_transition(current: str, incoming: str) -> bool:
    """True when ``current -> incoming`` is legal (re-reporting current is legal)."""
    if current not in LEGAL_TRANSITIONS or incoming not in LEGAL_TRANSITIONS:
        return False
    if incoming == current:
        return True
    return incoming in LEGAL_TRANSITIONS[current]


def apply_status(current: str, incoming: str) -> str:
    """Merge an incoming status. Terminal states win and never move backwards."""
    if not can_transition(current, incoming):
        reject(
            ErrorCode.ILLEGAL_TRANSITION,
            "status",
            f"illegal status transition: {current} -> {incoming}",
        )
    return incoming


def cancel_outcome(*, has_stop_evidence: bool, timed_out: bool) -> str | None:
    """Decide the status of a cancellation.

    ``canceled`` requires positive plugin stop evidence. A cancellation that
    exceeds its timeout without evidence becomes ``timed_out``; it must never be
    reported as ``canceled``. Returns ``None`` while cancellation is still
    pending (the action stays non-terminal, goal phase ``pending_cancel``).
    """
    if has_stop_evidence:
        return ActionStatus.CANCELED
    if timed_out:
        return ActionStatus.TIMED_OUT
    return None


# --------------------------------------------------------------------------
# Goal phases. Only ``active`` enters decision.
# --------------------------------------------------------------------------


class GoalPhase:
    ACCEPTED = "accepted"
    PENDING_CANCEL = "pending_cancel"
    ACTIVE = "active"
    BLOCKED = "blocked"
    REJECTED = "rejected"


GOAL_PHASES = frozenset(
    {
        GoalPhase.ACCEPTED,
        GoalPhase.PENDING_CANCEL,
        GoalPhase.ACTIVE,
        GoalPhase.BLOCKED,
        GoalPhase.REJECTED,
    }
)


def phase_enters_decision(phase: str) -> bool:
    return phase == GoalPhase.ACTIVE


def decide_phase(*, accepted: bool, replacing: bool, old_stopped: bool) -> str:
    """Resolve the phase after a goal submission.

    A goal that replaces a live one only becomes ``active`` once the old
    action's safe stop is confirmed; otherwise it stays ``pending_cancel`` and
    the goal never enters decision. A failed stop is ``blocked``, never a
    silent switch.
    """
    if not accepted:
        return GoalPhase.REJECTED
    if not replacing:
        return GoalPhase.ACTIVE
    if old_stopped:
        return GoalPhase.ACTIVE
    return GoalPhase.BLOCKED


# --------------------------------------------------------------------------
# Commands, events.
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ActionCommand:
    command_id: str
    ex_session: str
    goal_id: str
    goal_revision: int
    decision_id: str
    owner: str
    plugin_generation: int
    action_id: str
    operation: str
    params: dict[str, Any] = field(default_factory=dict)
    lease_ms: int = 0
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def parse(cls, data: Any) -> "ActionCommand":
        raw = require_object(data, "command")
        _check_input_budget(raw, "command")
        _reject_unknown_fields(raw, frozenset({"schema_version", "command_id", "ex_session", "goal_id", "goal_revision", "decision_id", "owner", "plugin_generation", "action_id", "operation", "params", "lease_ms"}), "command")
        if "schema_version" not in raw:
            reject(ErrorCode.MISSING_FIELD, "schema_version", "is required")
        if raw.get("schema_version") != SCHEMA_VERSION or type(raw.get("schema_version")) is not int:
            reject(
                ErrorCode.UNSUPPORTED_SCHEMA_VERSION,
                "schema_version",
                f"unsupported schema_version: {raw.get('schema_version')!r}",
            )
        operation = require_text(raw.get("operation"), "operation")
        if operation not in ALL_OPERATIONS:
            reject(ErrorCode.UNSUPPORTED_OPERATION, "operation", f"unsupported operation: {operation}")
        params = raw.get("params")
        if params is None:
            reject(ErrorCode.MISSING_PARAMS, "params", "is required")
        params = require_object(params, "params")
        non_finite = find_non_finite(params)
        if non_finite is not None:
            raise ContractError(non_finite)
        return cls(
            command_id=require_id(raw.get("command_id"), "command_id"),
            ex_session=require_id(raw.get("ex_session"), "ex_session"),
            goal_id=require_id(raw.get("goal_id"), "goal_id"),
            goal_revision=require_sequence(raw.get("goal_revision"), "goal_revision"),
            decision_id=require_id(raw.get("decision_id"), "decision_id"),
            owner=require_id(raw.get("owner"), "owner"),
            plugin_generation=require_sequence(raw.get("plugin_generation"), "plugin_generation"),
            action_id=require_id(raw.get("action_id"), "action_id"),
            operation=operation,
            params=copy.deepcopy(params),
            lease_ms=require_lease(raw.get("lease_ms"), "lease_ms"),
            schema_version=SCHEMA_VERSION,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "command_id": self.command_id,
            "ex_session": self.ex_session,
            "goal_id": self.goal_id,
            "goal_revision": self.goal_revision,
            "decision_id": self.decision_id,
            "owner": self.owner,
            "plugin_generation": self.plugin_generation,
            "action_id": self.action_id,
            "operation": self.operation,
            "params": copy.deepcopy(self.params),
            "lease_ms": self.lease_ms,
        }


def validate_stop_evidence(details: dict[str, Any], path: str = "details") -> None:
    """A canceled report needs explicit positive plugin stop evidence."""
    if "stop_evidence" not in details:
        reject(ErrorCode.MISSING_FIELD, f"{path}.stop_evidence", "canceled requires stop evidence")
    evidence = require_object(details["stop_evidence"], f"{path}.stop_evidence")
    if "stopped" not in evidence:
        reject(ErrorCode.MISSING_FIELD, f"{path}.stop_evidence.stopped", "is required")
    stopped = require_bool(evidence["stopped"], f"{path}.stop_evidence.stopped")
    if not stopped:
        reject(ErrorCode.ENUM_VIOLATION, f"{path}.stop_evidence.stopped", "must be true")
    for key in evidence:
        if key not in {"stopped", "source", "reference"}:
            reject(ErrorCode.UNKNOWN_FIELD, f"{path}.stop_evidence.{key}", "unknown evidence field")
        if key in {"source", "reference"}:
            require_id(evidence[key], f"{path}.stop_evidence.{key}")


@dataclass(slots=True)
class ActionEvent:
    event_id: str
    event_seq: int
    ex_session: str
    task_id: str
    goal_id: str
    goal_revision: int
    command_id: str
    owner: str
    status: str
    reason_code: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, data: Any) -> "ActionEvent":
        raw = require_object(data, "event")
        _check_input_budget(raw, "event")
        _reject_unknown_fields(raw, frozenset({"event_id", "event_seq", "ex_session", "task_id", "goal_id", "goal_revision", "command_id", "owner", "status", "reason_code", "details"}), "event")
        status = require_text(raw.get("status"), "status")
        if status not in LEGAL_TRANSITIONS:
            reject(ErrorCode.INVALID_TYPE, "status", f"unknown status: {status}")
        details = raw.get("details", {})
        details = require_object(details, "details")
        if status == ActionStatus.CANCELED:
            validate_stop_evidence(details)
        non_finite = find_non_finite(details)
        if non_finite is not None:
            raise ContractError(non_finite)
        return cls(
            event_id=require_id(raw.get("event_id"), "event_id"),
            event_seq=require_sequence(raw.get("event_seq"), "event_seq", minimum=1),
            ex_session=require_id(raw.get("ex_session"), "ex_session"),
            task_id=require_id(raw.get("task_id"), "task_id"),
            goal_id=require_id(raw.get("goal_id"), "goal_id"),
            goal_revision=require_sequence(raw.get("goal_revision"), "goal_revision"),
            command_id=require_id(raw.get("command_id"), "command_id"),
            owner=require_id(raw.get("owner"), "owner"),
            status=status,
            reason_code=_optional_action_text(raw.get("reason_code", ""), "reason_code"),
            details=copy.deepcopy(details),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_seq": self.event_seq,
            "ex_session": self.ex_session,
            "task_id": self.task_id,
            "goal_id": self.goal_id,
            "goal_revision": self.goal_revision,
            "command_id": self.command_id,
            "owner": self.owner,
            "status": self.status,
            "reason_code": self.reason_code,
            "details": copy.deepcopy(self.details),
        }


# --------------------------------------------------------------------------
# Action declaration v2. v1 legacy manifests stay isolated.
# --------------------------------------------------------------------------

_ACTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*\.[a-z0-9][a-z0-9_.-]*\.v[0-9]+$")
_RESOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")

_DANGER_LEVELS = frozenset({"low", "medium", "high"})


@dataclass(slots=True)
class ActionDeclarationV2:
    action_id: str
    description: str
    schema: dict[str, Any]
    resources: list[str] = field(default_factory=list)
    operations: list[str] = field(default_factory=lambda: [CommandOperation.START])
    requires_runtime_state: list[str] = field(default_factory=list)
    requires_observations: list[str] = field(default_factory=list)
    max_duration_ms: int | None = None
    cancel_timeout_ms: int | None = None
    danger: str = "low"

    def supports(self, operation: str) -> bool:
        return operation in self.operations

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "action_id": self.action_id,
            "description": self.description,
            "schema": copy.deepcopy(self.schema),
            "resources": list(self.resources),
            "operations": list(self.operations),
            "requires_runtime_state": list(self.requires_runtime_state),
            "requires_observations": list(self.requires_observations),
            "danger": self.danger,
        }
        if self.max_duration_ms is not None:
            data["max_duration_ms"] = self.max_duration_ms
        if self.cancel_timeout_ms is not None:
            data["cancel_timeout_ms"] = self.cancel_timeout_ms
        return data


@dataclass(slots=True)
class ActionManifestV2:
    action_api_version: int
    actions: list[ActionDeclarationV2]
    id: str = ""
    observation_sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    observation_guide: str = ""
    provides: list[str] = field(default_factory=list)
    ros2_ports: list[dict[str, Any]] = field(default_factory=list)

    def by_id(self, action_id: str) -> ActionDeclarationV2 | None:
        for action in self.actions:
            if action.action_id == action_id:
                return action
        return None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "action_api_version": self.action_api_version,
            "actions": [action.to_dict() for action in self.actions],
            "observation_sources": copy.deepcopy(self.observation_sources),
            "observation_guide": self.observation_guide,
            "provides": list(self.provides),
            "ros2_ports": copy.deepcopy(self.ros2_ports),
        }
        if self.id:
            result["id"] = self.id
        return result


def _validate_action_declaration(owner: str, raw: Any, path: str) -> ActionDeclarationV2:
    item = require_object(raw, path)
    _reject_unknown_fields(item, frozenset({"action_id", "description", "schema", "resources", "operations", "requires_runtime_state", "requires_observations", "max_duration_ms", "cancel_timeout_ms", "danger"}), path)
    action_id = require_id(item.get("action_id"), f"{path}.action_id")
    if _ACTION_ID_RE.match(action_id) is None:
        reject(ErrorCode.INVALID_ACTION_ID, f"{path}.action_id", f"invalid action_id: {action_id}")
    if owner and not action_id.startswith(f"{owner}."):
        reject(
            ErrorCode.OWNER_MISMATCH,
            f"{path}.action_id",
            f"action_id must be owned by '{owner}': {action_id}",
        )
    description = require_text(item.get("description"), f"{path}.description")
    schema = item.get("schema")
    if schema is None:
        reject(ErrorCode.MISSING_FIELD, f"{path}.schema", "is required")
    # Only validate the schema itself, not instance params (which would require values)
    schema_errors = check_schema_supported(schema)
    if schema_errors:
        raise ContractError(schema_errors[0])
    operations = _unique_strings(item.get("operations", []), f"{path}.operations", allow_empty=False)
    for index, operation in enumerate(operations):
        if operation not in ALL_OPERATIONS:
            reject(
                ErrorCode.UNSUPPORTED_OPERATION,
                f"{path}.operations[{index}]",
                f"unsupported operation: {operation}",
            )
    resources = _unique_strings(item.get("resources", []), f"{path}.resources")
    for index, resource in enumerate(resources):
        if _RESOURCE_RE.match(resource) is None:
            reject(ErrorCode.INVALID_TYPE, f"{path}.resources[{index}]", f"invalid resource name: {resource}")
    danger = item.get("danger", "low")
    if not isinstance(danger, str):
        reject(ErrorCode.INVALID_TYPE, f"{path}.danger", "must be a string")
    if danger not in _DANGER_LEVELS:
        reject(ErrorCode.ENUM_VIOLATION, f"{path}.danger", f"danger must be one of {sorted(_DANGER_LEVELS)}")
    max_duration_ms = item.get("max_duration_ms")
    if max_duration_ms is not None:
        max_duration_ms = require_int(max_duration_ms, f"{path}.max_duration_ms", minimum=1, maximum=MAX_LEASE_MS)
    cancel_timeout_ms = item.get("cancel_timeout_ms")
    claims_cancel = CommandOperation.CANCEL in operations
    if claims_cancel:
        if cancel_timeout_ms is None:
            reject(
                ErrorCode.CANCEL_UNSUPPORTED,
                f"{path}.cancel_timeout_ms",
                "an action that declares 'cancel' must declare a positive cancel_timeout_ms",
            )
        cancel_timeout_ms = require_int(cancel_timeout_ms, f"{path}.cancel_timeout_ms", minimum=1, maximum=MAX_LEASE_MS)
    elif cancel_timeout_ms is not None:
        reject(
            ErrorCode.CANCEL_UNSUPPORTED,
            f"{path}.cancel_timeout_ms",
            "cancel_timeout_ms is only meaningful when 'cancel' is declared",
        )
    requires_runtime_state = _unique_strings(
        item.get("requires_runtime_state", []), f"{path}.requires_runtime_state"
    )
    known_states = frozenset({"idle", "ready", "running", "paused", "fault", "finished"})
    for index, state in enumerate(requires_runtime_state):
        if state not in known_states:
            reject(
                ErrorCode.ENUM_VIOLATION,
                f"{path}.requires_runtime_state[{index}]",
                f"unknown runtime state: {state}",
            )
    requires_observations = _unique_strings(
        item.get("requires_observations", []), f"{path}.requires_observations"
    )
    return ActionDeclarationV2(
        action_id=action_id,
        description=description,
        schema=copy.deepcopy(schema),
        resources=resources,
        operations=operations,
        requires_runtime_state=requires_runtime_state,
        requires_observations=requires_observations,
        max_duration_ms=max_duration_ms,
        cancel_timeout_ms=cancel_timeout_ms,
        danger=danger,
    )


def parse_action_manifest(data: Any, *, owner: str = "") -> ActionManifestV2:
    """Parse an action declaration v2 manifest.

    A manifest without ``action_api_version`` is legacy: it is refused here so
    that a v1 manifest can never be silently driven through the v2 dispatcher.
    """
    if not isinstance(owner, str) or (owner and (_RESOURCE_RE.fullmatch(owner) is None or len(owner) > MAX_ID_LEN)):
        reject(ErrorCode.INVALID_TYPE, "owner", "must be a bounded plugin id")
    raw = require_object(data, "manifest")
    _check_input_budget(raw, "manifest")
    _reject_unknown_fields(raw, frozenset({"id", "action_api_version", "actions", "observation_sources", "observation_guide", "provides", "ros2_ports"}), "manifest")
    if "id" in raw:
        declared_owner = require_id(raw["id"], "manifest.id")
        if _RESOURCE_RE.fullmatch(declared_owner) is None:
            reject(ErrorCode.INVALID_TYPE, "manifest.id", "must be a plugin id")
        if owner and declared_owner != owner:
            reject(ErrorCode.OWNER_MISMATCH, "manifest.id", "manifest id differs from trusted owner")
        owner = declared_owner
    if "action_api_version" not in raw:
        reject(
            ErrorCode.LEGACY_ISOLATION,
            "action_api_version",
            "legacy manifest without action_api_version cannot be parsed as v2",
        )
    version = raw.get("action_api_version")
    # Strict type check: refuse bool (True==1) and float (2.0 is not int 2 for protocol purposes)
    if not isinstance(version, int) or isinstance(version, bool):
        reject(
            ErrorCode.UNSUPPORTED_ACTION_API_VERSION,
            "action_api_version",
            f"action_api_version must be an integer, got {type(version).__name__}: {version!r}",
        )
    if version != ACTION_API_VERSION_V2:
        reject(
            ErrorCode.UNSUPPORTED_ACTION_API_VERSION,
            "action_api_version",
            f"unsupported action_api_version: {version!r}",
        )
    raw_actions = raw.get("actions")
    if not isinstance(raw_actions, list):
        reject(ErrorCode.INVALID_TYPE, "actions", "must be an array")
    if not raw_actions:
        reject(ErrorCode.LENGTH_VIOLATION, "actions", "must not be empty")
    actions: list[ActionDeclarationV2] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_actions):
        declaration = _validate_action_declaration(owner, item, f"actions[{index}]")
        if declaration.action_id in seen:
            reject(
                ErrorCode.DUPLICATE_ACTION_ID,
                f"actions[{index}].action_id",
                f"duplicate action declaration: {declaration.action_id}",
            )
        seen.add(declaration.action_id)
        actions.append(declaration)
    sources = require_object(raw.get("observation_sources", {}), "observation_sources")
    parsed_sources: dict[str, dict[str, Any]] = {}
    for name, source in sources.items():
        require_id(name, "observation_sources.name")
        if _RESOURCE_RE.fullmatch(name) is None:
            reject(ErrorCode.INVALID_TYPE, "observation_sources.name", "must be a source id")
        item = require_object(source, f"observation_sources.{name}")
        _reject_unknown_fields(item, frozenset({"topic", "max_age_ms", "required_fields"}), f"observation_sources.{name}")
        topic = require_id(item.get("topic"), f"observation_sources.{name}.topic")
        if _RESOURCE_RE.fullmatch(topic) is None or "." not in topic:
            reject(ErrorCode.INVALID_TYPE, f"observation_sources.{name}.topic", "must be a qualified internal topic")
        parsed_sources[name] = {
            "topic": topic,
            "max_age_ms": require_lease(item.get("max_age_ms"), f"observation_sources.{name}.max_age_ms"),
            "required_fields": _unique_strings(item.get("required_fields", []), f"observation_sources.{name}.required_fields"),
        }
    for action in actions:
        for name in action.requires_observations:
            if name not in parsed_sources:
                reject(ErrorCode.MISSING_FIELD, f"observation_sources.{name}", "required observation needs a source mapping")
    guide = raw.get("observation_guide", "")
    guide = _optional_action_text(guide, "observation_guide")
    ports = raw.get("ros2_ports", [])
    if not isinstance(ports, list) or any(not isinstance(port, dict) for port in ports):
        reject(ErrorCode.INVALID_TYPE, "ros2_ports", "must be an array of objects")
    return ActionManifestV2(
        action_api_version=ACTION_API_VERSION_V2,
        actions=actions,
        id=raw.get("id", ""),
        observation_sources=parsed_sources,
        observation_guide=guide,
        provides=_unique_strings(raw.get("provides", []), "provides"),
        ros2_ports=copy.deepcopy(ports),
    )


@dataclass(slots=True)
class LegacyActionDeclaration:
    """v1 declaration: command delivery is a TopicBus topic."""

    action_id: str
    topic: str


def parse_legacy_action_manifest(data: Any) -> list[LegacyActionDeclaration]:
    """Parse a v1 manifest. A v2 manifest is refused here (isolation is two-way)."""
    raw = require_object(data, "manifest")
    _check_input_budget(raw, "manifest")
    if "action_api_version" in raw:
        reject(
            ErrorCode.LEGACY_ISOLATION,
            "action_api_version",
            "v2 manifest cannot be parsed as a legacy topic action manifest",
        )
    _reject_unknown_fields(raw, frozenset({"id", "actions"}), "manifest")
    owner = require_id(raw["id"], "manifest.id") if "id" in raw else ""
    raw_actions = raw.get("actions", [])
    if not isinstance(raw_actions, list):
        reject(ErrorCode.INVALID_TYPE, "actions", "must be an array")
    legacy: list[LegacyActionDeclaration] = []
    for index, item in enumerate(raw_actions):
        entry = require_object(item, f"actions[{index}]")
        _reject_unknown_fields(entry, frozenset({"action_id", "topic", "description"}), f"actions[{index}]")
        action_id = require_id(entry.get("action_id"), f"actions[{index}].action_id")
        if owner and manifest_owner(action_id) != owner:
            reject(ErrorCode.OWNER_MISMATCH, f"actions[{index}].action_id", "action differs from manifest owner")
        topic = entry.get("topic")
        if topic is None:
            reject(
                ErrorCode.MISSING_FIELD,
                f"actions[{index}].topic",
                "legacy actions are delivered over a command topic and require one",
            )
        legacy.append(LegacyActionDeclaration(action_id=action_id, topic=require_id(topic, f"actions[{index}].topic")))
    return legacy


# --------------------------------------------------------------------------
# Admission helpers: idempotency, revision/session guards, resource exclusion.
# --------------------------------------------------------------------------


@dataclass(slots=True)
class IdempotencyRegistry:
    """Same request_id + same payload replays; same request_id + different payload conflicts."""

    _seen: dict[str, str] = field(default_factory=dict)

    def resolve(self, request_id: str, payload: dict[str, Any]) -> str:
        """Return ``new``, ``replay`` or raise a conflict."""
        require_id(request_id, "request_id")
        _check_input_budget(payload, "idempotency.payload")
        require_object(payload, "idempotency.payload")
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        previous = self._seen.get(request_id)
        if previous is None:
            self._seen[request_id] = canonical
            return "new"
        if previous == canonical:
            return "replay"
        reject(
            ErrorCode.DUPLICATE_REQUEST_ID_CONFLICT,
            "request_id",
            f"request_id reused with a different payload: {request_id}",
        )


def check_ex_session(payload_session: Any, current_session: str) -> str:
    """Refuse replies produced before an EX restart (B00 §8)."""
    session = require_text(payload_session, "ex_session")
    if session != current_session:
        reject(
            ErrorCode.STALE_EX_SESSION,
            "ex_session",
            f"stale ex_session {session!r}, current is {current_session!r}",
        )
    return session


def check_revision(expected: Any, current: int) -> int:
    """Compare-then-set guard for goal replacement.

    When expected is None, the caller does not require CAS semantics and the
    current revision is returned unchanged (no increment). When present, expected
    must match current exactly, and the result is current + 1.
    """
    current = require_int(
        current,
        "current_revision",
        minimum=0,
        maximum=MAX_SEQUENCE,
    )
    if expected is not None:
        value = require_int(expected, "expected_revision", minimum=0)
        if value != current:
            reject(
                ErrorCode.REVISION_CONFLICT,
                "expected_revision",
                f"expected_revision {value} does not match current {current}",
            )
    if current >= MAX_SEQUENCE:
        reject(
            ErrorCode.RANGE_VIOLATION,
            "current_revision",
            f"revision cannot exceed {MAX_SEQUENCE}",
        )
    return current + 1


def check_resources(holders: dict[str, str], requested: list[str], command_id: str) -> None:
    """Refuse a start whose resources are held by a different live command.

    Resource names express real controller mutual exclusion; they are never
    invented by the LLM.
    """
    for resource in requested:
        holder = holders.get(resource)
        if holder is not None and holder != command_id:
            reject(
                ErrorCode.RESOURCE_CONFLICT,
                "resources",
                f"resource {resource!r} is held by command {holder!r}",
            )


def check_operation_supported(declaration: ActionDeclarationV2, operation: str) -> None:
    """Refuse an operation the action never declared (e.g. cancel on a
    non-cancelable action must not be reported as supported)."""
    if not declaration.supports(operation):
        reject(
            ErrorCode.CANCEL_UNSUPPORTED if operation == CommandOperation.CANCEL else ErrorCode.UNSUPPORTED_OPERATION,
            "operation",
            f"{declaration.action_id} does not declare operation {operation!r}",
        )


def validate_command_against_manifest(
    command: ActionCommand,
    manifest: ActionManifestV2,
    *,
    current_ex_session: str,
    current_revision: int,
    runtime_state: str,
    trusted_owner: str,
    granted_operations: frozenset[str] | None = None,
) -> ActionDeclarationV2:
    """Full admission check for a direct action command."""
    check_ex_session(command.ex_session, require_id(current_ex_session, "current_ex_session"))
    current_revision = require_sequence(current_revision, "current_revision")
    runtime_state = require_id(runtime_state, "runtime_state")
    if runtime_state not in KNOWN_RUNTIME_STATES:
        reject(ErrorCode.ENUM_VIOLATION, "runtime_state", "unknown runtime state")
    if command.goal_revision != current_revision:
        reject(
            ErrorCode.REVISION_CONFLICT,
            "goal_revision",
            f"goal_revision {command.goal_revision} does not match current {current_revision}",
        )
    declaration = manifest.by_id(command.action_id)
    if declaration is None:
        reject(ErrorCode.UNKNOWN_ACTION, "action_id", f"unknown action: {command.action_id}")
    if command.owner != manifest_owner(declaration.action_id) or command.owner != require_id(trusted_owner, "trusted_owner"):
        reject(
            ErrorCode.OWNER_MISMATCH,
            "owner",
            f"owner {command.owner!r} does not own {command.action_id}",
        )
    check_operation_supported(declaration, command.operation)
    if granted_operations is not None and command.operation not in granted_operations:
        reject(
            ErrorCode.UNSUPPORTED_OPERATION,
            "operation",
            f"operation {command.operation!r} is not granted to this owner",
        )
    if (
        declaration.requires_runtime_state
        and runtime_state not in declaration.requires_runtime_state
    ):
        reject(
            ErrorCode.ENUM_VIOLATION,
            "runtime_state",
            f"{command.action_id} requires runtime state {declaration.requires_runtime_state}, got {runtime_state}",
        )
    if command.operation == CommandOperation.START:
        errors = validate_params(declaration.schema, command.params)
        if errors:
            raise ContractError(errors[0])
    return declaration


def manifest_owner(action_id: str) -> str:
    """owner is the plugin id prefix of a v2 action_id."""
    return action_id.split(".", 1)[0]


ALL_STATUSES = frozenset(LEGAL_TRANSITIONS)
KNOWN_RUNTIME_STATES = frozenset({"idle", "ready", "running", "paused", "fault", "finished"})
PROTOCOL_NAME = "astrbotex-zmq"
PROTOCOL_VERSION = 1


def is_valid_params(schema: Any, params: Any) -> bool:
    return not validate_params(schema, params)


def error_code_of(exc: BaseException) -> str:
    """Return the frozen error code for an exception, or ``ok``."""
    if isinstance(exc, ContractError):
        return exc.error.code
    return ErrorCode.OK


def require_phase(value: Any) -> str:
    phase = require_text(value, "phase")
    if phase not in GOAL_PHASES:
        reject(ErrorCode.ENUM_VIOLATION, "phase", f"phase must be one of {sorted(GOAL_PHASES)}")
    return phase


# ========================================================================
# Decision channel
# ========================================================================


import copy
import math
from dataclasses import dataclass, field
from typing import Any


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
