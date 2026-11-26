"""One framework-authorized goal, one replacement; no automatic plan queue."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Callable

from ..actions.models import ContractError, check_ex_session, check_revision, reject, validate_params
from .models import GoalCancel, GoalRenew, GoalSubmit, GoalSubmitResult


@dataclass(frozen=True)
class GoalRecord:
    payload_json: str
    revision: int
    expires_ns: int

    def payload(self) -> dict:
        return json.loads(self.payload_json)


class GoalManager:
    def __init__(self, catalog, *, ex_session: str | None = None,
                 revoke: Callable[[], None] = lambda: None,
                 clock_ns: Callable[[], int] = time.monotonic_ns,
                 request_limit: int = 4096) -> None:
        self.catalog = catalog
        self.ex_session = ex_session or uuid.uuid4().hex
        self._revoke = revoke
        self._clock = clock_ns
        self._lock = threading.RLock()
        self.revision = 0
        self.gate_epoch = 0
        self.active: GoalRecord | None = None
        self.pending_replace: GoalRecord | None = None
        self._terminal_stop = None  # one revoked goal/epoch, never new execution authority
        self.phase = "idle"
        self.reason = "new_authorization_required"
        self._requests: dict[str, tuple[str, dict]] = {}
        self._request_limit = request_limit
        self._history = deque(maxlen=64)

    def _replay(self, request_id: str, kind: str, payload: dict) -> tuple[str, dict | None]:
        digest = hashlib.sha256(json.dumps([kind, payload], sort_keys=True,
                                         ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        old = self._requests.get(request_id)
        if old:
            if old[0] != digest:
                reject("duplicate_request_id_conflict", "request_id", "request ID already binds another payload")
            return digest, copy.deepcopy(old[1])
        if len(self._requests) >= self._request_limit:
            reject("request_capacity", "request_id", "session request tombstones full; stop and create a new session")
        return digest, None

    def _remember(self, request_id: str, digest: str, response: dict) -> dict:
        self._requests[request_id] = (digest, copy.deepcopy(response))
        return response

    def _close_gate(self, reason: str) -> None:
        self.gate_epoch += 1
        self.reason = reason[:256]
        self._revoke()  # framework gate only; no I/O or physical-stop wait

    def submit(self, raw: dict | GoalSubmit) -> dict:
        goal = GoalSubmit.parse(raw.to_dict() if isinstance(raw, GoalSubmit) else raw)
        with self._lock:
            check_ex_session(goal.ex_session, self.ex_session)
            digest, replay = self._replay(goal.request_id, "submit", goal.to_dict())
            if replay is not None:
                return replay
            revision = check_revision(goal.expected_revision, self.revision)
            entries = self.catalog.snapshot().entries
            actions = {a["action_id"]: (entry, a) for entry in entries for a in entry["manifest"]["actions"]}
            if len(goal.allowed_actions) > 128:
                reject("goal_capacity", "allowed_actions", "at most 128 actions per goal")
            for action_id in goal.allowed_actions:
                pair = actions.get(action_id)
                if pair is None or not pair[0]["available"]:
                    reject("unknown_action", "allowed_actions", f"unavailable action: {action_id}")
                if "start" not in pair[1]["operations"] or "cancel" not in pair[1]["operations"]:
                    reject("cancel_unsupported", "allowed_actions", "decision actions require start and bounded cancel")
                if action_id not in goal.parameters:
                    reject("missing_params", f"parameters.{action_id}", "exact bound parameters required")
                errors = validate_params(pair[1]["schema"], goal.parameters[action_id], f"parameters.{action_id}")
                if errors:
                    raise ContractError(errors[0])
            if self.active and self.active.payload()["goal_id"] == goal.goal_id:
                reject("revision_conflict", "goal_id", "replacement requires a distinct goal ID")
            record = GoalRecord(json.dumps(goal.to_dict(), sort_keys=True, ensure_ascii=False,
                                           allow_nan=False), revision, self._clock() + goal.lease_ms * 1_000_000)
            self._close_gate("goal_replaced" if self.active else "goal_accepted")
            self.revision = revision
            self.pending_replace = record
            if self.phase != "blocked":
                self.phase = "pending_cancel"
            response = GoalSubmitResult(True, goal.request_id, self.ex_session, goal.goal_id,
                                        revision, "blocked" if self.phase == "blocked" else "pending_cancel").to_dict()
            self._history.append({"revision": revision, "goal_id": goal.goal_id, "phase": response["phase"]})
            return self._remember(goal.request_id, digest, response)

    def stop(self, reason: str = "stopped", *, terminal_status: str | None = None) -> int:
        with self._lock:
            target, active = self.pending_replace or self.active, self.active
            self._close_gate(reason)
            self._terminal_stop = ((target, self.gate_epoch, terminal_status, reason[:256], active)
                if target is not None and terminal_status in {"canceled", "timed_out", "failed"} else None)
            self.pending_replace = None
            if self.phase != "blocked":
                self.phase = "stopping"
            return self.gate_epoch

    def resolve_stop(self, epoch: int, proven: bool, *, activate: bool = True) -> bool:
        with self._lock:
            if epoch != self.gate_epoch or self.phase == "blocked":
                return False
            if not proven:
                self.phase, self.reason = "blocked", "stop_not_proven"
                return False
            self.active = None
            pending = self.pending_replace
            if pending and activate and self._clock() < pending.expires_ns:
                self.active, self.pending_replace = pending, None
                self.phase, self.reason = "active", ""
                return True
            self.pending_replace = None
            self.phase, self.reason = "idle", "new_authorization_required"
            return False

    def finish_stopped(self, goal: GoalRecord, epoch: int) -> bool:
        """One completion CAS permit; never consume a pending replacement."""
        with self._lock:
            terminal = self._terminal_stop
            revoked = (terminal is not None and terminal[:2] == (goal, epoch)
                       and self.phase == "stopping" and self.active == terminal[4])
            if ((self.active != goal and not revoked) or self.gate_epoch != epoch
                    or self.pending_replace is not None or self.revision != goal.revision
                    or self.phase == "blocked"):
                return False
            self.active = None
            self._terminal_stop = None
            self.phase, self.reason = "idle", "new_authorization_required"
            return True

    def block(self, reason: str) -> None:
        with self._lock:
            self._close_gate(reason)
            self.phase = "blocked"

    def reviewed(self, epoch: int) -> bool:
        with self._lock:
            if epoch != self.gate_epoch:
                return False
            self._close_gate("new_authorization_required")
            self.active = self.pending_replace = None
            self.phase = "idle"
            return True

    def awaiting_llm(self, revision: int, reason: str) -> None:
        with self._lock:
            if self.active and self.active.revision == revision and self.phase == "active":
                self._close_gate(reason)
                self.phase = "awaiting_llm"

    def reject_decision(self, revision: int, reason: str, *, goal_id: str) -> tuple | None:
        """Revoke exactly the selected Goal; retirement still requires stop proof."""
        with self._lock:
            if (self.active is None or self.active.revision != revision or self.phase != "active"
                    or self.active.payload()["goal_id"] != goal_id
                    or self.pending_replace is not None or self._clock() >= self.active.expires_ns):
                return None
            self.stop(reason, terminal_status="failed")
            return self._terminal_stop

    def expire(self) -> bool:
        with self._lock:
            goal = self.pending_replace or self.active
            if goal and self._clock() >= goal.expires_ns and self.phase not in {"blocked", "stopping"}:
                self.stop("goal_lease_expired", terminal_status="timed_out")
                return True
            return False

    def cancel(self, raw: dict | GoalCancel) -> dict:
        req = GoalCancel.parse(raw.to_dict() if isinstance(raw, GoalCancel) else raw)
        with self._lock:
            check_ex_session(req.ex_session, self.ex_session)
            digest, replay = self._replay(req.request_id, "cancel", req.to_dict())
            if replay is not None:
                return replay
            self._match(req.goal_id, req.goal_revision)
            self.stop(req.reason_code or "goal_canceled", terminal_status="canceled")
            return self._remember(req.request_id, digest, {"ok": True, "accepted": True, "stopped": False})

    def renew(self, raw: dict | GoalRenew) -> dict:
        req = GoalRenew.parse(raw.to_dict() if isinstance(raw, GoalRenew) else raw)
        with self._lock:
            check_ex_session(req.ex_session, self.ex_session)
            digest, replay = self._replay(req.request_id, "renew", req.to_dict())
            if replay is not None:
                return replay
            goal = self._match(req.goal_id, req.goal_revision)
            if self.phase != "active" or self._clock() >= goal.expires_ns:
                reject("authorization_revoked", "goal_revision", "renew cannot restore revoked/expired authorization")
            self.active = GoalRecord(goal.payload_json, goal.revision, self._clock() + req.lease_ms * 1_000_000)
            return self._remember(req.request_id, digest, {"ok": True, "revision": goal.revision})

    def _match(self, goal_id: str, revision: int) -> GoalRecord:
        goal = self.pending_replace or self.active
        if goal is None or goal.revision != revision or goal.payload()["goal_id"] != goal_id:
            reject("revision_conflict", "goal_revision", "goal/revision is not current")
        return goal

    def current(self) -> tuple[GoalRecord | None, int, str]:
        with self._lock:
            return self.active, self.gate_epoch, self.phase

    def status(self) -> dict:
        with self._lock:
            return {"schema_version": 1, "ex_session": self.ex_session, "revision": self.revision,
                    "active_goal_id": self.active.payload()["goal_id"] if self.active else None,
                    "pending_goal_id": self.pending_replace.payload()["goal_id"] if self.pending_replace else None,
                    "phase": self.phase, "reason_code": self.reason, "gate_epoch": self.gate_epoch,
                    "request_tombstones": len(self._requests), "history": list(self._history)}
