"""B00 frozen contract types for the direct action dispatch channel.

Pure standard library. This module must never import runtime, hardware, ROS, or
transport code. Both AstrBotEX and A.E.B keep a byte-identical copy so that the
two sides parse the same payloads into the same result.

Frozen by B00 (see AstrBotEX/docs/DECISION-CONTRACT.md). Business payloads carry
``schema_version = 1`` inside the unchanged ``astrbotex-zmq`` version 1 envelope.
"""

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
