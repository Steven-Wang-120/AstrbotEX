"""B09 — silent receive of upper steps and the durable receipt log.

The control channel delivers one ordered, immutable batch of natural-language
segments.  This module validates it, records a durable receipt, and hands it to
the decision backend input adapter.  It is a receipt ledger, not a second task
coordinator: there is no retry loop, no suffix auto-send and no execution
feedback.

Phases, and what they do *not* prove:

* ``received``  — registered locally; the model has not been shown the text.
* ``injected``  — the text crossed the configured backend input boundary.
* ``rejected``  — refused before or at the boundary.
* ``unknown``   — consumption started and the outcome was never observed.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from astrbot_ex.core.decision.instruction_input import (
    MAX_ID_CHARS,
    MAX_PAYLOAD_BYTES,
    MAX_SEGMENT_CHARS,
    MAX_SEGMENTS,
    PHASES,
    SCHEMA,
    VISIBILITY,
    DecisionInstructionInput,
    InstructionBatch,
    InstructionInputError,
    StepSegment,
    measure_bytes,
)

METHOD_CAPABILITIES = "upper.capabilities.get"
METHOD_INJECT = "upper.steps.inject"
METHODS = (METHOD_CAPABILITIES, METHOD_INJECT)

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

_TERMINAL_PHASES = frozenset({"injected", "rejected"})


class StepInputError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


def _error_response(code: str, detail: str = "") -> dict[str, Any]:
    # ``error_code`` is mirrored at the top level because B06 reads it there;
    # the nested ``error`` object keeps the shape used by the rest of EX.
    return {
        "ok": False,
        "error": {"code": code, "message": detail or code},
        "error_code": code,
        "phase": "rejected",
    }


def _success_response(receipt: "StepReceipt", duplicate: bool) -> dict[str, Any]:
    """Flat receipt fields plus the nested copy.

    The upper controller reads ``batch_id``/``request_id``/``payload_hash``/
    ``phase``/``ex_session`` straight off the reply, so they are mirrored at the
    top level; ``receipt`` keeps one object for callers that prefer it.
    """

    body = receipt.public()
    return {"ok": True, **body, "receipt": body, "duplicate": duplicate}


def _trust_reason(trusted: "TrustedControlContext | None") -> str:
    if trusted is None:
        return "trusted_context_unavailable"
    if not trusted.robot_id:
        return "robot_not_configured"
    if not trusted.route_refs:
        return "route_ref_not_configured"
    if not trusted.ex_session:
        return "session_unavailable"
    return ""


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _restricted_id(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise StepInputError("invalid_batch", f"{name} 必须是字符串")
    text = value.strip()
    if not text or len(text) > MAX_ID_CHARS:
        raise StepInputError("invalid_batch", f"{name} 长度必须在 1..{MAX_ID_CHARS}")
    if any(ch.isspace() or ord(ch) < 32 for ch in text):
        raise StepInputError("invalid_batch", f"{name} 不能包含空白或控制字符")
    return text


def _optional_id(value: Any, name: str) -> str:
    if value in (None, ""):
        return ""
    return _restricted_id(value, name)


def parse_injection_batch(payload: Any) -> InstructionBatch:
    """Strict C01 parser.  Rejects unknown keys instead of dropping them."""

    if not isinstance(payload, Mapping):
        raise StepInputError("invalid_batch", "payload 必须是对象")
    unknown = set(payload) - _BATCH_KEYS
    if unknown:
        raise StepInputError("invalid_batch", "不接受的字段: " + ", ".join(sorted(unknown)))
    missing = _BATCH_KEYS - set(payload)
    # catalog_version / prompt_version are optional; everything else is required.
    missing -= {"catalog_version", "prompt_version"}
    if missing:
        raise StepInputError("invalid_batch", "缺少字段: " + ", ".join(sorted(missing)))

    if payload.get("schema") != SCHEMA:
        raise StepInputError("invalid_batch", f"schema 必须是 {SCHEMA}")
    if payload.get("visibility") != VISIBILITY:
        raise StepInputError("invalid_batch", f"visibility 必须是 {VISIBILITY}")

    revision = payload.get("plan_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise StepInputError("invalid_batch", "plan_revision 必须是 >=1 的整数")

    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise StepInputError("invalid_batch", "segments 必须是非空列表")
    if len(raw_segments) > MAX_SEGMENTS:
        raise StepInputError("invalid_batch", f"segments 最多 {MAX_SEGMENTS} 段")

    segments: list[StepSegment] = []
    for index, item in enumerate(raw_segments):
        if not isinstance(item, Mapping):
            raise StepInputError("invalid_batch", f"segments[{index}] 必须是对象")
        extra = set(item) - _SEGMENT_KEYS
        if extra:
            raise StepInputError(
                "invalid_batch",
                f"segments[{index}] 不接受字段: " + ", ".join(sorted(extra)),
            )
        if set(item) != _SEGMENT_KEYS:
            raise StepInputError("invalid_batch", f"segments[{index}] 缺少必需字段")
        ordinal = item.get("ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int):
            raise StepInputError("invalid_batch", f"segments[{index}].ordinal 必须是整数")
        if ordinal != index:
            raise StepInputError(
                "invalid_batch", f"segments[{index}].ordinal 必须是连续的，从 0 开始"
            )
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise StepInputError("invalid_batch", f"segments[{index}].text 不能为空")
        if len(text) > MAX_SEGMENT_CHARS:
            raise StepInputError(
                "invalid_batch", f"segments[{index}].text 超过 {MAX_SEGMENT_CHARS} 字符"
            )
        segments.append(
            StepSegment(
                segment_id=_restricted_id(item.get("segment_id"), f"segments[{index}].segment_id"),
                ordinal=ordinal,
                text=text,
            )
        )

    batch = InstructionBatch(
        schema=SCHEMA,
        batch_id=_restricted_id(payload.get("batch_id"), "batch_id"),
        request_id=_restricted_id(payload.get("request_id"), "request_id"),
        plan_id=_restricted_id(payload.get("plan_id"), "plan_id"),
        plan_revision=revision,
        robot_id=_restricted_id(payload.get("robot_id"), "robot_id"),
        route_ref=_restricted_id(payload.get("route_ref"), "route_ref"),
        ex_session=_restricted_id(payload.get("ex_session"), "ex_session"),
        catalog_version=_optional_id(payload.get("catalog_version"), "catalog_version"),
        prompt_version=_optional_id(payload.get("prompt_version"), "prompt_version"),
        segments=tuple(segments),
    )
    if measure_bytes(batch.to_wire()) > MAX_PAYLOAD_BYTES:
        raise StepInputError("payload_too_large", f"payload 超过 {MAX_PAYLOAD_BYTES} 字节")
    return batch


# --------------------------------------------------------------------------
# Durable receipt log
# --------------------------------------------------------------------------


@dataclass
class StepReceipt:
    batch_id: str
    request_id: str
    payload_hash: str
    phase: str
    connection_id: str
    route_ref: str
    ex_session: str
    robot_id: str
    segment_ids: tuple[str, ...]
    received_at: float
    reason: str = ""
    settled_at: float | None = None
    event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "request_id": self.request_id,
            "payload_hash": self.payload_hash,
            "phase": self.phase,
            "connection_id": self.connection_id,
            "route_ref": self.route_ref,
            "ex_session": self.ex_session,
            "robot_id": self.robot_id,
            "segment_ids": list(self.segment_ids),
            "segment_count": len(self.segment_ids),
            "received_at": self.received_at,
            "reason": self.reason,
            "settled_at": self.settled_at,
        }

    def public(self) -> dict[str, Any]:
        """Receipt shape returned to AEB.  No payload text, no model output."""

        return {
            "batch_id": self.batch_id,
            "request_id": self.request_id,
            "payload_hash": self.payload_hash,
            "phase": self.phase,
            "segment_count": len(self.segment_ids),
            "ex_session": self.ex_session,
            "reason": self.reason,
        }


class StepInputLog:
    """Append-only JSONL ledger.  One writer under a lock; last line wins."""

    def __init__(self, path: Path, *, max_entries: int = 512) -> None:
        self.path = Path(path)
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.RLock()
        self._receipts: dict[str, StepReceipt] = {}
        self._order: list[str] = []
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue  # a torn write is ignored, never replayed
            if not isinstance(value, dict):
                continue
            batch_id = value.get("batch_id")
            if not isinstance(batch_id, str) or not batch_id:
                continue
            existing = self._receipts.get(batch_id)
            phase = value.get("phase")
            if phase not in PHASES:
                phase = "unknown"
            if existing is None:
                receipt = StepReceipt(
                    batch_id=batch_id,
                    request_id=str(value.get("request_id") or ""),
                    payload_hash=str(value.get("payload_hash") or ""),
                    phase=phase,
                    connection_id=str(value.get("connection_id") or ""),
                    route_ref=str(value.get("route_ref") or ""),
                    ex_session=str(value.get("ex_session") or ""),
                    robot_id=str(value.get("robot_id") or ""),
                    segment_ids=tuple(value.get("segment_ids") or ()),
                    received_at=float(value.get("received_at") or 0.0),
                    reason=str(value.get("reason") or ""),
                    settled_at=value.get("settled_at"),
                )
                self._receipts[batch_id] = receipt
                self._order.append(batch_id)
            else:
                existing.phase = phase
                existing.reason = str(value.get("reason") or "")
                existing.settled_at = value.get("settled_at")
        # Anything that was mid-flight when the process died has an unknown
        # outcome.  It is never re-injected automatically.
        changed = False
        for receipt in self._receipts.values():
            if receipt.phase not in _TERMINAL_PHASES:
                receipt.phase = "unknown"
                receipt.reason = receipt.reason or "recovered_after_restart"
                receipt.event.set()
                changed = True
        if changed:
            self._rewrite()

    def _append(self, receipt: StepReceipt) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(receipt.to_dict(), ensure_ascii=False) + "\n")
                handle.flush()
        except OSError:
            # The ledger is evidence, not a lock: a disk error must not turn a
            # rejected write into a silent success.
            raise StepInputError("internal_error", "receipt log unavailable") from None

    def _rewrite(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            with temp.open("w", encoding="utf-8") as handle:
                for batch_id in self._order:
                    receipt = self._receipts.get(batch_id)
                    if receipt is not None:
                        handle.write(json.dumps(receipt.to_dict(), ensure_ascii=False) + "\n")
            temp.replace(self.path)
        except OSError:
            return

    def _trim(self) -> None:
        while len(self._order) > self.max_entries:
            oldest = self._order.pop(0)
            self._receipts.pop(oldest, None)

    def get(self, batch_id: str) -> StepReceipt | None:
        with self._lock:
            return self._receipts.get(batch_id)

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._receipts[key].to_dict() for key in self._order if key in self._receipts]

    def register(
        self,
        batch: InstructionBatch,
        *,
        connection_id: str,
        ex_session: str,
        robot_id: str,
        now: float | None = None,
    ) -> tuple[StepReceipt, bool]:
        """Atomic register.  Returns ``(receipt, created)``.

        The same source/session/batch id with the same content is a duplicate and
        returns the existing receipt; a different content is ``id_conflict``.
        """

        with self._lock:
            existing = self._receipts.get(batch.batch_id)
            if existing is not None:
                if (
                    existing.payload_hash == batch.payload_hash
                    and existing.connection_id == connection_id
                    and existing.ex_session == ex_session
                ):
                    return existing, False
                raise StepInputError("id_conflict", "batch_id 已绑定其他内容")
            receipt = StepReceipt(
                batch_id=batch.batch_id,
                request_id=batch.request_id,
                payload_hash=batch.payload_hash,
                phase="received",
                connection_id=connection_id,
                route_ref=batch.route_ref,
                ex_session=ex_session,
                robot_id=robot_id,
                segment_ids=batch.segment_ids,
                received_at=float(now if now is not None else time.time()),
            )
            self._receipts[batch.batch_id] = receipt
            self._order.append(batch.batch_id)
            self._append(receipt)
            self._trim()
            return receipt, True

    def update(self, batch_id: str, phase: str, *, reason: str = "") -> StepReceipt | None:
        if phase not in PHASES:
            raise ValueError(f"unknown phase: {phase}")
        with self._lock:
            receipt = self._receipts.get(batch_id)
            if receipt is None:
                return None
            receipt.phase = phase
            receipt.reason = reason
            receipt.settled_at = time.time()
            self._append(receipt)
            receipt.event.set()
            return receipt


# --------------------------------------------------------------------------
# Ingress
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustedControlContext:
    """Built from the local connection record, never from the wire."""

    robot_id: str
    route_refs: tuple[str, ...]
    ex_session: str

    @property
    def ready(self) -> bool:
        return bool(self.robot_id) and bool(self.route_refs) and bool(self.ex_session)


class StepInputIngress:
    """Validate, record, queue, and hand off — in that order, once."""

    def __init__(
        self,
        *,
        log: StepInputLog,
        adapter: DecisionInstructionInput,
        trusted_context: Callable[[str], TrustedControlContext | None],
        catalog_summary: Callable[[], Mapping[str, Any]] | None = None,
        queue_capacity: int = 32,
        request_budget_sec: float = 1.5,
        worker_budget_sec: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.log = log
        self.adapter = adapter
        self._trusted_context = trusted_context
        self._catalog_summary = catalog_summary
        self._queue: queue.Queue[InstructionBatch | None] = queue.Queue(
            maxsize=max(1, int(queue_capacity))
        )
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.request_budget_sec = float(request_budget_sec)
        self.worker_budget_sec = float(worker_budget_sec)
        self._clock = clock

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self._worker = threading.Thread(
                target=self._run, name="upper-step-input", daemon=True
            )
            self._worker.start()

    def close(self, timeout: float = 2.0) -> None:
        """Stop accepting, drop queued work, then release the worker."""

        self._stop.set()
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                self.log.update(item.batch_id, "rejected", reason="ingress_stopped")
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        worker = self._worker
        self._worker = None
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=timeout)

    # -- request handling -------------------------------------------------

    def handle(
        self,
        connection_id: str,
        method: str,
        payload: Mapping[str, Any],
        binary: bytes | None = None,
        *,
        feature: str = "control",
    ) -> tuple[dict[str, Any], bytes | None]:
        if feature != "control":
            return _error_response("session_mismatch", "upper.* 只允许在 control 通道"), None
        if method == METHOD_CAPABILITIES:
            return self._capabilities(connection_id), None
        if method == METHOD_INJECT:
            return self._inject(connection_id, payload, binary), None
        return _error_response("unsupported_method", f"未知方法: {method}"), None

    def _capabilities(self, connection_id: str) -> dict[str, Any]:
        trusted = self._safe_trusted(connection_id)
        ready, reason = self.adapter.supports()
        if ready:
            trust_reason = _trust_reason(trusted)
            if trust_reason:
                ready, reason = False, trust_reason
        summary = {}
        if callable(self._catalog_summary):
            try:
                summary = dict(self._catalog_summary() or {})
            except Exception:
                summary = {}
        return {
            "ok": True,
            "schema": SCHEMA,
            "visibility": VISIBILITY,
            "methods": list(METHODS),
            "input_ready": bool(ready),
            "input_ready_reason": "" if ready else str(reason),
            "ex_session": trusted.ex_session if trusted is not None else "",
            "backend": self.adapter.backend_name,
            "limits": {
                "max_segments": MAX_SEGMENTS,
                "max_segment_chars": MAX_SEGMENT_CHARS,
                "max_payload_bytes": MAX_PAYLOAD_BYTES,
                "queue_capacity": self._queue.maxsize,
            },
            # Flat aliases so an upper controller that reads the limits directly
            # off the reply does not have to know the nested shape.
            "max_segments": MAX_SEGMENTS,
            "max_bytes": MAX_PAYLOAD_BYTES,
            "catalog_version": summary.get("catalog_version"),
            "actions": list(summary.get("actions") or []),
        }

    def _safe_trusted(self, connection_id: str) -> TrustedControlContext | None:
        try:
            return self._trusted_context(connection_id)
        except Exception:
            return None

    def _inject(
        self,
        connection_id: str,
        payload: Mapping[str, Any],
        binary: bytes | None,
    ) -> dict[str, Any]:
        if self._stop.is_set() or self._worker is None or not self._worker.is_alive():
            return _error_response("backend_unsupported", "ingress 未运行")
        if binary is not None:
            return _error_response("invalid_batch", "不接受二进制帧")

        trusted = self._safe_trusted(connection_id)
        if trusted is None or not trusted.ready:
            return _error_response("session_mismatch", "本地 control 连接未完成身份配置")

        try:
            batch = parse_injection_batch(payload)
        except StepInputError as error:
            return _error_response(error.code, error.detail)

        if batch.robot_id != trusted.robot_id:
            return _error_response("session_mismatch", "robot_id 与本地连接配置不一致")
        if batch.ex_session != trusted.ex_session:
            return _error_response("session_mismatch", "ex_session 已过期")
        if batch.route_ref not in trusted.route_refs:
            return _error_response("route_mismatch", "route_ref 不属于该连接登记的来源")

        ready, reason = self.adapter.supports()
        if not ready:
            return _error_response("backend_unsupported", str(reason))

        with self._lock:
            try:
                receipt, created = self.log.register(
                    batch,
                    connection_id=connection_id,
                    ex_session=trusted.ex_session,
                    robot_id=trusted.robot_id,
                    now=self._clock(),
                )
            except StepInputError as error:
                return _error_response(error.code, error.detail)

            if not created:
                # A repeat of the same content is answered from the ledger; the
                # model is not shown the text twice.
                return _success_response(receipt, True)

            try:
                self._queue.put_nowait(batch)
            except queue.Full:
                self.log.update(batch.batch_id, "rejected", reason="input_busy")
                return _error_response("input_busy", "输入队列已满")

        receipt.event.wait(self.request_budget_sec)
        return _success_response(receipt, False)

    # -- worker -----------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                return
            self._consume(item)

    def _consume(self, batch: InstructionBatch) -> None:
        def settled(phase: str, detail: str = "") -> None:
            if phase not in PHASES:
                phase = "unknown"
            self.log.update(batch.batch_id, phase, reason=detail)

        try:
            outcome = self.adapter.accept(
                batch,
                budget_sec=self.worker_budget_sec,
                on_settled=settled,
            )
        except InstructionInputError as error:
            # ``internal_error`` and ``delivery_unknown`` mean the outcome was
            # never observed.  Claiming a clean rejection there would hide a
            # batch that may already have reached the model.
            phase = "unknown" if error.code in {"internal_error", "delivery_unknown"} else "rejected"
            self.log.update(batch.batch_id, phase, reason=error.code)
            return
        except Exception as error:  # noqa: BLE001 - outcome is genuinely unknown
            self.log.update(
                batch.batch_id, "unknown", reason=type(error).__name__ or "internal_error"
            )
            return

        phase = str(outcome.get("phase") or "received")
        if phase == "injected":
            self.log.update(batch.batch_id, "injected", reason=str(outcome.get("detail") or ""))
        elif phase == "received":
            # Still queued inside the service; the settled callback owns the
            # final phase.  Never claim injected here.
            self.log.get(batch.batch_id)
        elif phase == "rejected":
            self.log.update(batch.batch_id, "rejected", reason=str(outcome.get("detail") or ""))
        else:
            self.log.update(batch.batch_id, "unknown", reason=str(outcome.get("detail") or ""))


def build_trusted_context(
    *,
    robot_id: Any,
    route_refs: Any,
    ex_session: Any,
) -> TrustedControlContext:
    """Normalise the local connection record into a trusted context."""

    robot = str(robot_id or "").strip()
    session = str(ex_session or "").strip()
    refs: list[str] = []
    raw = route_refs
    if isinstance(raw, str):
        refs = [item.strip() for item in raw.split(",") if item.strip()]
    elif isinstance(raw, (list, tuple)):
        refs = [str(item).strip() for item in raw if str(item).strip()]
    return TrustedControlContext(robot_id=robot, route_refs=tuple(refs), ex_session=session)


__all__ = [
    "METHODS",
    "METHOD_CAPABILITIES",
    "METHOD_INJECT",
    "SCHEMA",
    "StepInputError",
    "StepInputIngress",
    "StepInputLog",
    "StepReceipt",
    "TrustedControlContext",
    "build_trusted_context",
    "parse_injection_batch",
]
