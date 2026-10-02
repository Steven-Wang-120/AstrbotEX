"""Fail-closed B04 coordinator. Backend and persistence never run under runtime locks."""
from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from collections import deque

from concurrent.futures import TimeoutError as FutureTimeout

from ..actions.dispatcher import DispatcherBusy
from ..actions.ledger import LedgerBusy, LedgerClosed, LedgerFault, OwnerBinding
from ..actions.models import ActionCommand, ActionStatus, ContractError, TERMINAL_STATUSES, validate_params
from .backends import MockBackend
from .goal_manager import GoalManager
from .models import BackendDecision, DecisionSnapshot, VersionSet, validate_backend_selection
from .observations import ObservationStore


class ShutdownErrors(RuntimeError):
    """Python 3.10-compatible aggregate retaining every original exception."""

    def __init__(self, message: str, exceptions) -> None:
        self.exceptions = tuple(exceptions)
        super().__init__(message + f" ({len(self.exceptions)} errors)")


class _ApplicationIOError(RuntimeError):
    """Application refresh failed, independently of backend choice validation."""


class DecisionService:
    def __init__(self, action_service, *, backend=None, topic_bus=None,
                 environment=None, max_hz: float = 5, backend_timeout: float = 2,
                 max_snapshot_age_ms: int = 2000, io_timeout: float = 1,
                 clock_ns=time.monotonic_ns) -> None:
        if max_hz <= 0 or backend_timeout <= 0 or io_timeout <= 0:
            raise ValueError("positive decision timing limits required")
        self.actions = action_service
        self.catalog = action_service.catalog
        self.backend = backend or MockBackend()
        self.environment = environment
        self._environment_snapshot = environment.snapshot() if environment is not None else None
        self._clock = clock_ns
        self._interval_ns = int(1e9 / max_hz)
        self._timeout_ns = int(backend_timeout * 1e9)
        self._max_age_ms, self._io_timeout = max_snapshot_age_ms, io_timeout
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._closed = False
        self.mode = "disabled"
        self.config_revision = 0
        self.goals = GoalManager(self.catalog, revoke=self._revoke_goal, clock_ns=clock_ns)
        if self.actions.dispatcher.blocked:
            self.goals.block("startup_recovery_requires_explicit_review")
        self.observations = ObservationStore(topic_bus, clock_ns=clock_ns, on_update=self.notify)
        self._dirty = False
        self._stop_pending = False
        self._stop_dispatcher_epoch = -1
        self._stop_attempt_epoch = -1
        self._stop_attempts = 0
        self._next_stop_time = 0.0
        self._stop_error = ""
        self._stop_request_retry = False
        self._review_future = None
        self._backend_input = None
        self._backend_result = None
        self._live_snapshot = None
        self._timed_out = False
        self._next_request_ns = 0
        self._failures = 0
        self._rows = ()
        self._proven_uncertain = set()
        self._last_catalog_revision = -1
        self._last_framework_versions = None
        self._last_error = ""
        self._decisions = deque(maxlen=128)
        self._last_snapshot = None
        self._dispatch_snapshot = None
        self._batches = deque(maxlen=64)
        self._last_poll_ns = 0
        self._last_stop_epoch = -1
        self.completion_hook = None
        self._control = threading.Thread(target=self._run, name="decision-control", daemon=True)
        self._backend_worker = threading.Thread(target=self._backend_loop, name="decision-backend", daemon=True)
        self._backend_worker.start()
        self._control.start()

    def _revoke_goal(self) -> None:
        if getattr(self, "completion_hook", None) is not None and self.actions.control_mode == "decision":
            self.actions.revoke_decision()
        else:
            self.actions.revoke()  # independent B04/SDK retains generic safety behavior

    def notify(self) -> None:
        with self._condition:
            self._dirty = True  # a single coalescing bit, never one future per frame
            snapshot = self._dispatch_snapshot or self._last_snapshot
            if snapshot and self.mode == "execute":
                # Invalidate target-bound queued starts before their Actor guard.
                entries = {e["owner"]: e for e in self.catalog.snapshot().entries}
                for owner in snapshot.owners:
                    entry = entries.get(owner["owner"])
                    if entry is None:
                        continue
                    for candidate in owner["candidates"]:
                        if candidate["kind"] != "start":
                            continue
                        aid = candidate["action_id"]
                        params = snapshot.goal["parameters"][aid]
                        if "target" not in params and "target_ref" not in params:
                            continue
                        action = next(a for a in entry["manifest"]["actions"] if a["action_id"] == aid)
                        _, reason = self.observations.relevant(action, params, dangerous=action["danger"] == "high")
                        if reason:
                            self.request_stop(reason)
                            break
            self._condition.notify_all()

    def submit_goal(self, raw) -> dict:
        with self._condition:
            if self._closed:
                raise RuntimeError("decision service closed")
            result = self.goals.submit(raw)
            if self.goals.status()["phase"] in {"pending_cancel", "blocked"}:
                self._stop_pending = True
            self._dirty = True
            self._condition.notify_all()
            return result

    def cancel_goal(self, raw) -> dict:
        with self._condition:
            epoch = self.goals.gate_epoch
            result = self.goals.cancel(raw)
            if self.goals.gate_epoch != epoch:
                self._stop_pending = True
                self._condition.notify_all()
            return result

    def renew_goal(self, raw) -> dict:
        result = self.goals.renew(raw)
        self.notify()
        return result

    def set_mode(self, mode: str) -> None:
        if mode not in {"disabled", "shadow", "execute"}:
            raise ValueError("decision mode must be disabled, shadow or execute")
        with self._condition:
            if self._closed:
                raise RuntimeError("decision service closed")
            if mode == "execute" and getattr(self.backend, "execution_allowed", False) is not True:
                raise RuntimeError("backend_execute_not_allowed")
            if mode != self.mode:
                self.request_stop("decision_mode_changed")
                self.mode = mode
                self.config_revision += 1
                self._dirty = True
                self._condition.notify_all()

    def configuration_changed(self) -> None:
        with self._condition:
            self.config_revision += 1
            self.request_stop("config_revision_changed")

    def reconfigure_backend(self, config) -> None:
        """Trusted configuration entry: EX revision invalidates results before backend epoch."""
        reconfigure = getattr(self.backend, "reconfigure", None)
        if not callable(reconfigure):
            raise ValueError("backend does not support configuration")
        with self._condition:
            if self._closed:
                raise RuntimeError("decision service closed")
            self.config_revision += 1
            self.request_stop("config_revision_changed")
            reconfigure(config)

    def request_stop(self, reason: str = "decision_stop") -> None:
        with self._condition:
            self.goals.stop(reason)  # closes Dispatcher gate immediately
            with self.actions.dispatcher._lock:
                self._stop_dispatcher_epoch = getattr(self.actions.dispatcher, "_epoch", -1)
            self._stop_pending = True
            self._condition.notify_all()

    def review(self):
        from concurrent.futures import Future
        with self._condition:
            if self._closed:
                raise RuntimeError("decision service closed")
            if self._review_future is not None and not self._review_future.cancelled():
                return self._review_future
            future = self._review_future = Future()
            self._condition.notify_all()
            return future

    def tick(self) -> None:
        # Called under the runtime tick lock: signal only, no DB/backend waits.
        with self._condition:
            self._dirty = True
            self._condition.notify_all()

    def _versions(self, catalog) -> VersionSet:
        state = self.goals.status()
        environment_generation = (self._environment_snapshot["generation"] if self._environment_snapshot is not None
                                  else self.actions._environment_revision)
        with self.actions.dispatcher._lock:
            gate_epoch = self.actions.dispatcher._epoch
        return VersionSet(self.goals.ex_session, state["revision"],
                          self.config_revision + self.actions._config_revision, catalog.revision,
                          environment_generation, gate_epoch,
                          {e["owner"]: e["generation"] for e in catalog.entries})

    def _poll_rows(self) -> None:
        # Ledger reads are bounded, on this worker only; status serves a cache.
        # One total observation budget, independent of the B02 stop budget.
        deadline = time.monotonic() + self._io_timeout
        def read(submit):
            if time.monotonic() >= deadline:
                raise TimeoutError("decision observation read deadline")
            future = submit()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("decision observation read deadline")
            result = future.result(remaining)
            if time.monotonic() >= deadline:
                raise TimeoutError("decision observation read deadline")
            return result
        rows, cursor = [], ""
        for _ in range(20):
            page = read(lambda: self.actions.ledger.list_commands(after_id=cursor, limit=500))
            rows.extend(page)
            if len(page) < 500:
                proven = set()
                for row in rows:
                    if row.status in {ActionStatus.UNKNOWN, ActionStatus.TIMED_OUT, ActionStatus.FAILED}:
                        proof = read(lambda: self.actions.ledger.stop_proof(
                            row.command_id, OwnerBinding(row.owner, row.generation)))
                        if proof is not None and proof.stopped is True and not row.held_resources:
                            proven.add(row.command_id)
                with self._lock:
                    if tuple(rows) != self._rows:
                        self._dirty = True
                    self._rows = tuple(rows)
                    self._proven_uncertain = proven
                return
            cursor = page[-1].command_id
        raise RuntimeError("ledger_scan_capacity: B02 needs terminal retention/tombstones")

    def _current_rows(self, goal):
        result = []
        for row in self._rows:
            command = json.loads(row.canonical_command)["command"]
            if (command["ex_session"] == self.goals.ex_session and
                    command["goal_revision"] == goal.revision and command["goal_id"] == goal.payload()["goal_id"]):
                result.append((row, command))
        return result

    def _hard_start(self, entry, action, params, occupied) -> tuple[list[dict], str]:
        if not entry["available"]:
            return [], entry["unavailable_reason"] or "owner_unavailable"
        if self.mode != "execute" and self.mode != "shadow":
            return [], "decision_disabled"
        if self.actions.control_mode != "decision":
            return [], "legacy_isolation"
        if self.actions._runtime_state not in (action["requires_runtime_state"] or ["running", "ready"]):
            return [], "runtime_state_changed"
        if self.actions.dispatcher.blocked:
            return [], "dispatcher_blocked"
        if validate_params(action["schema"], params):
            return [], "parameter_schema_invalid"
        if any(row.status not in TERMINAL_STATUSES for row in self._rows):
            return [], "live_round_requires_stable_dispatch_context"
        if set(action["resources"]) & occupied:
            return [], "resource_busy"
        if self._environment_snapshot is not None:
            env = self._environment_snapshot
            if env["phase"] != "idle":
                return [], "environment_transition"
        return self.observations.relevant(action, params, dangerous=action["danger"] == "high")

    def build_snapshot(self) -> DecisionSnapshot | None:
        with self._lock:
            goal, _, phase = self.goals.current()
            if goal is None or phase != "active" or self._clock() >= goal.expires_ns or self.mode == "disabled":
                return None
            catalog = self.catalog.snapshot()
            payload = goal.payload()
            rows = self._current_rows(goal)
            occupied = {r for row in self._rows for r in row.held_resources}
            owners, observed = [], {}
            counter = 0
            snapshot_id = uuid.uuid4().hex
            for entry in catalog.entries:
                allowed = [a for a in entry["manifest"]["actions"] if a["action_id"] in payload["allowed_actions"]]
                own = [(r, c) for r, c in rows if r.owner == entry["owner"] and r.status not in TERMINAL_STATUSES]
                if not allowed and not own:
                    continue
                candidates = []
                def option(kind, description, **kw):
                    nonlocal counter
                    counter += 1
                    candidates.append({"option_id": f"{snapshot_id}:{counter}", "kind": kind,
                                       "description": description, "eligible": True, **kw})
                option("wait", "Wait without dispatch or extending an action lease.")
                option("request_replan", "Ask AEB to supply new goal parameters; never choose another goal.")
                for row, command in own[:1]:
                    option("keep", "Keep this existing command; do not replay start.",
                           command_id=row.command_id, action_id=command["action_id"])
                    option("cancel", "Request bounded cancellation and committed stop evidence.",
                           command_id=row.command_id, action_id=command["action_id"])
                if not own:
                    for action in allowed:
                        if any(c["action_id"] == action["action_id"] and r.status == ActionStatus.SUCCEEDED for r, c in rows):
                            continue
                        observations, reason = self._hard_start(entry, action, payload["parameters"][action["action_id"]], occupied)
                        for item in observations:
                            observed[item["source_id"]] = item
                        if not reason:
                            option("start", action["description"], action_id=action["action_id"])
                        else:
                            candidates[1]["reason_code"] = reason
                owners.append({"owner": entry["owner"], "plugin_generation": entry["generation"],
                               "status": entry["state"], "candidates": candidates})
            snapshot = DecisionSnapshot.parse({"schema_version": 1, "snapshot_id": snapshot_id,
                "created_monotonic_ns": self._clock(), "versions": self._versions(catalog).to_dict(),
                "goal": {key: payload[key] for key in ("task_id", "goal_id", "goal_text_en", "allowed_actions", "parameters")},
                "observations": list(observed.values()), "owners": owners})
            self._last_snapshot = snapshot
            return snapshot

    def _record(self, snapshot, outcome, reason="", **details) -> None:
        with self._lock:
            self._decisions.append({"snapshot_id": snapshot.snapshot_id if snapshot else None,
                                    "outcome": outcome, "reason_code": reason[:256], **details})
            self._last_error = reason[:256]

    def _validate_response(self, snapshot, decision):
        current = self._versions(self.catalog.snapshot())
        for key, value in snapshot.versions.to_dict().items():
            if current.to_dict()[key] != value:
                raise RuntimeError(f"{key}_changed")
        goal, _, phase = self.goals.current()
        if goal is None or phase != "active" or self._clock() >= goal.expires_ns:
            raise RuntimeError("goal_authorization_expired")
        age = (self._clock() - snapshot.created_monotonic_ns) / 1e6
        if age < 0 or age > self._max_age_ms:
            raise RuntimeError("snapshot_age_expired")
        live_observations = {o["source_id"]: o for o in self.observations.snapshot(
            o["source_id"] for o in snapshot.observations)}
        for old in snapshot.observations:
            live = live_observations.get(old["source_id"])
            if live is None:
                raise RuntimeError("observation_missing")
            if live["source_epoch"] != old["source_epoch"]:
                raise RuntimeError("observation_source_epoch_changed")
            if live["description_hash"] != old["description_hash"]:
                raise RuntimeError("observation_description_changed")
            if live["health"]["status"] != "ok":
                raise RuntimeError(live["health"]["reason_code"])
        self.observations.request_observations(snapshot.observations)
        return validate_backend_selection(snapshot, decision, current)

    def _apply(self, snapshot, decision) -> None:
        try:
            if self.environment is not None:
                self._environment_snapshot = self.environment.snapshot()
            self._poll_rows()
        except Exception as exc:
            raise _ApplicationIOError(str(exc)) from exc
        with self._lock:
            selected = self._validate_response(snapshot, decision)
            catalog = self.catalog.snapshot()
            entries = {e["owner"]: e for e in catalog.entries}
            occupied = {r for row in self._rows for r in row.held_resources}
            starts, cancels, observed, replans = [], [], {}, []
            owner_by_option = {c["option_id"]: o["owner"] for o in snapshot.owners for c in o["candidates"]}
            for option in selected:
                owner = owner_by_option[option["option_id"]]
                if option["kind"] == "start":
                    entry = entries[owner]
                    action = next(a for a in entry["manifest"]["actions"] if a["action_id"] == option["action_id"])
                    observations, reason = self._hard_start(entry, action, snapshot.goal["parameters"][action["action_id"]], occupied)
                    if reason:
                        raise RuntimeError(reason)
                    occupied.update(action["resources"])  # reserve the whole round before any dispatch
                    starts.append((entry, action))
                    bound = {item["source_id"]: item for item in snapshot.observations}
                    for source in action["requires_observations"]:
                        if source not in bound:
                            raise RuntimeError("request_observation_missing")
                        observed[source] = bound[source]
                elif option["kind"] == "cancel":
                    cancels.append((option["command_id"], OwnerBinding(owner, entries[owner]["generation"])))
                elif option["kind"] == "request_replan":
                    replans.append(option)
            if self.mode == "shadow":
                self._record(snapshot, "shadow", choices=copy.deepcopy(selected))
                return
            if self.mode != "execute" or getattr(self.backend, "execution_allowed", False) is not True:
                raise RuntimeError("backend_execute_not_allowed")
            if replans:
                self.goals.awaiting_llm(snapshot.versions.goal_revision, "backend_requested_replan")
                self._stop_pending = True
                self._record(snapshot, "awaiting_llm", "backend_requested_replan")
                return
            if cancels:
                for cid, binding in cancels:
                    self.actions.cancel(cid, binding, "backend_cancel")
                self._record(snapshot, "cancel_requested")
                return  # mixed cancel/start requires a new snapshot after committed stop
            self._validate_response(snapshot, decision)
            if not starts:
                self._record(snapshot, "no_dispatch", choices=copy.deepcopy(selected))
                return
            goal, _, _ = self.goals.current()
            ttl = min(600000, max(1, (goal.expires_ns - self._clock()) // 1_000_000))
            dispatcher = self.actions.dispatcher
            # Serialize only admission authority, never persistence completion.
            with self.goals._lock, self.observations._lock:
                self._validate_response(snapshot, decision)
                for entry, action in starts:
                    _, reason = self.observations.relevant(action,
                        snapshot.goal["parameters"][action["action_id"]], dangerous=action["danger"] == "high")
                    if reason:
                        raise RuntimeError(reason)
                observed = {item["source_id"]: item for item in self.observations.request_observations(list(observed.values()))}
                dispatcher.update_context(ex_session=self.goals.ex_session, goal_id=snapshot.goal["goal_id"],
                    goal_revision=goal.revision, task_id=snapshot.goal["task_id"],
                    allowed_actions=snapshot.goal["allowed_actions"], bound_params=snapshot.goal["parameters"],
                    runtime_state=self.actions._runtime_state, catalog_revision=catalog.revision,
                    config_revision=self.actions._config_revision, environment_revision=self.actions._environment_revision,
                    ttl_ms=int(ttl), observations={sid: {"topic": self.observations._specs[sid]["topic"],
                        "fields": item["data"], "age_ms": item["age_ms"]} for sid, item in observed.items()})
                admitted = []
                try:
                    for entry, action in starts:
                        command = ActionCommand.parse({"schema_version": 1, "command_id": uuid.uuid4().hex,
                            "ex_session": self.goals.ex_session, "goal_id": snapshot.goal["goal_id"],
                            "goal_revision": goal.revision, "decision_id": snapshot.snapshot_id,
                            "owner": entry["owner"], "plugin_generation": entry["generation"],
                            "action_id": action["action_id"], "operation": "start",
                            "params": snapshot.goal["parameters"][action["action_id"]],
                            "lease_ms": min(int(ttl), action.get("max_duration_ms", 1000))})
                        future = self.actions.start(command)
                        admitted.append((command.command_id, entry["owner"], entry["generation"], future))
                except Exception:
                    self.request_stop("partial_dispatch_failure")
                    self._record(snapshot, "partial_execution", "partial_dispatch_failure",
                                 commands=[item[0] for item in admitted])
                    raise
            batch = {"snapshot_id": snapshot.snapshot_id, "commands": [item[0] for item in admitted], "rolled_back": False}
            self._dispatch_snapshot = snapshot
            self._batches.append(batch)
        # Await the admissions outside every state/runtime lock. An admitted
        # future is not the plugin's later accepted/succeeded event.
        try:
            for cid, owner, generation, future in admitted:
                row = future.result(self._io_timeout)
                if row.status in {ActionStatus.REJECTED, ActionStatus.UNKNOWN, ActionStatus.TIMED_OUT, ActionStatus.FAILED}:
                    raise RuntimeError("owner_rejected_or_uncertain")
        except Exception:
            self.request_stop("partial_dispatch_failure")
            batch["rolled_back"] = True
            self._record(snapshot, "partial_execution", "partial_dispatch_failure", commands=batch["commands"])
            raise
        self._record(snapshot, "admitted", commands=batch["commands"])

    def _settle_observed_stop(self) -> None:
        # Controller/lifecycle may have completed this stop on the synchronous
        # B02 path. Never replay it against a newer SDK context.
        goal, epoch, phase = self.goals.current()
        if (self.mode == "disabled" and goal is None and self.goals.pending_replace is None
                and phase == "stopping" and self._stop_pending
                and self.actions._stop_proof_epoch >= self._stop_dispatcher_epoch >= 0):
            self.goals.resolve_stop(epoch, True, activate=False)
            self._stop_pending = False

    def _owns_control(self) -> bool:
        goal, _, phase = self.goals.current()
        return (self.mode != "disabled" or goal is not None or self.goals.pending_replace is not None
                or phase in {"blocked", "stopping"})

    def _progress(self) -> None:
        with self._condition:
            # First acknowledge only already-proven no-goal stops. The worker
            # may have been polling while B02 completed the synchronous stop.
            # This does not cancel, revoke a context or activate a goal.
            self._settle_observed_stop()
            if not self._owns_control():
                return
            rows = {r.command_id: r for r in self._rows}
            for batch in self._batches:
                if not batch["rolled_back"] and any(rows.get(cid) and rows[cid].status in {
                        ActionStatus.REJECTED, ActionStatus.UNKNOWN, ActionStatus.TIMED_OUT, ActionStatus.FAILED}
                        for cid in batch["commands"]):
                    batch["rolled_back"] = True
                    if self.goals.phase == "active":
                        goal = self.goals.active
                        failed = (self.completion_hook is not None and goal is not None
                            and self.goals.pending_replace is None and any(
                                row.status == ActionStatus.FAILED and row.command_id in self._proven_uncertain
                                and not row.held_resources and row.event_seq > 0
                                and row.details.get("stop_evidence", {}).get("stopped") is True
                                for row, _ in self._current_rows(goal)))
                        if failed:
                            self.goals.stop("partial_owner_rejection", terminal_status="failed")
                            self._stop_pending = True
                            self._condition.notify_all()
                        else:
                            self.request_stop("partial_owner_rejection")
                    # A stop already in progress owns the cancellation. Do not
                    # erase its pending replacement or revoke its epoch again.
                    self._record(None, "partial_execution", "partial_owner_rejection", commands=batch["commands"])
            goal, _, phase = self.goals.current()
            if any(r.status in {ActionStatus.UNKNOWN, ActionStatus.TIMED_OUT, ActionStatus.FAILED}
                   and r.command_id not in self._proven_uncertain for r in self._rows) and phase != "blocked":
                self.goals.block("uncertain_action_requires_explicit_review")
                self._stop_pending = True
                return
            if goal and phase == "active":
                current = self._current_rows(goal)
                required = set(goal.payload()["completion"].get("required_success_actions", []))
                succeeded = {cmd["action_id"] for row, cmd in current if row.status == ActionStatus.SUCCEEDED and row.event_seq > 0}
                if required and required <= succeeded:
                    self.goals.awaiting_llm(goal.revision, "completion_evidence_committed")
                    self._stop_pending = True

    def _backend_loop(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._backend_input is not None)
                if self._closed:
                    return
                snapshot = self._backend_input
                self._backend_input = None
            try:
                result, error = self.backend.decide(DecisionSnapshot.parse(snapshot.to_dict())), None
            except Exception as exc:
                result, error = None, exc
            with self._condition:
                if self._closed:
                    return
                self._backend_result = (result, error)
                self._condition.notify_all()

    def _finish_proven_completion(self, goal, epoch) -> None:
        """Post-stop hook; all refresh/journal I/O is outside safety locks."""
        hook = self.completion_hook
        if hook is None or goal is None:
            return
        self._poll_rows()
        with self._condition, self.goals._lock, self.actions.dispatcher._lock:
            if (self.goals.active != goal or self.goals.revision != goal.revision
                    or self.goals.gate_epoch != epoch or self.goals.pending_replace is not None
                    or self.goals.phase != "awaiting_llm"
                    or self.goals.reason != "completion_evidence_committed"
                    or self._clock() >= goal.expires_ns
                    or self.actions.dispatcher._gate
                    or self.actions._stop_proof_epoch < self.actions.dispatcher._epoch):
                return
            current = self._current_rows(goal)
            required = set(goal.payload()["completion"].get("required_success_actions", []))
            succeeded = {cmd["action_id"] for row, cmd in current
                         if row.status == ActionStatus.SUCCEEDED and row.event_seq > 0}
            if (not required or not required <= succeeded
                    or any(row.status not in TERMINAL_STATUSES or row.held_resources for row, _ in current)):
                return
            summary = {"status": "succeeded", "reason_code": "completion_evidence_committed",
                "details": {"completion_evidence": {"verified": True,
                    "goal_id": goal.payload()["goal_id"], "goal_revision": goal.revision,
                    "required_actions": sorted(required), "succeeded_actions": sorted(succeeded),
                    "stop_proof_epoch": self.actions._stop_proof_epoch,
                    "commands": [{"command_id": row.command_id, "action_id": cmd["action_id"],
                                  "event_seq": row.event_seq, "status": row.status}
                                 for row, cmd in current]}}}
        hook(goal, epoch, summary)

    def _finish_proven_terminal(self, terminal) -> None:
        """Proven revoked goal only, after full stop; no persistence under locks."""
        goal, epoch, status, reason, _ = terminal
        self._poll_rows()
        with self._condition:
            rows = tuple(self._current_rows(goal))
            unresolved = any(row.held_resources or (row.status not in {
                ActionStatus.SUCCEEDED, ActionStatus.REJECTED, ActionStatus.CANCELED}
                and not (row.status in {ActionStatus.FAILED, ActionStatus.UNKNOWN, ActionStatus.TIMED_OUT}
                         and row.command_id in self._proven_uncertain)) for row in self._rows)
            failed_commands = {row.command_id for row, _ in rows
                if row.status == ActionStatus.FAILED and row.command_id in self._proven_uncertain
                and not row.held_resources and row.event_seq > 0
                and row.details.get("stop_evidence", {}).get("stopped") is True}
        if unresolved:
            with self._condition, self.goals._lock:
                if self.goals._terminal_stop == terminal and self.goals.gate_epoch == epoch:
                    self.goals.block("terminal_stop_requires_explicit_review")
            return
        if status == "failed" and not failed_commands:
            return
        commands = []
        deadline = time.monotonic() + self._io_timeout
        for row, command in rows:
            if row.held_resources or (row.status not in {
                    ActionStatus.SUCCEEDED, ActionStatus.REJECTED, ActionStatus.CANCELED}
                    and not (status == "failed" and row.command_id in failed_commands)):
                with self._condition, self.goals._lock:
                    if self.goals._terminal_stop == terminal and self.goals.gate_epoch == epoch:
                        self.goals.block("terminal_stop_requires_explicit_review")
                return
            item = {"command_id": row.command_id, "ex_session": command["ex_session"],
                "goal_id": command["goal_id"],
                "goal_revision": command["goal_revision"], "status": row.status, "event_seq": row.event_seq}
            if row.status in {ActionStatus.CANCELED, ActionStatus.FAILED}:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("terminal stop evidence deadline")
                proof = self.actions.ledger.stop_proof(
                    row.command_id, OwnerBinding(row.owner, row.generation)).result(remaining)
                if proof is None or proof.stopped is not True:
                    return
                item["stop_evidence"] = {"stopped": True, "source": proof.source, "reference": proof.reference}
            commands.append(item)
        failure_evidence = ([item for item in commands if item["command_id"] in failed_commands]
                            if status == "failed" else [])
        with self._condition, self.goals._lock, self.actions.dispatcher._lock:
            if (self._closed or self.completion_hook is None or self.goals._terminal_stop != terminal
                    or self.goals.gate_epoch != epoch or self.goals.revision != goal.revision
                    or self.goals.pending_replace is not None or self.goals.phase != "stopping"
                    or self.actions.dispatcher._gate or self.actions.dispatcher.blocked
                    or self.actions._stop_proof_epoch < self.actions.dispatcher._epoch):
                return
            hook = self.completion_hook
            proof_epoch = self.actions._stop_proof_epoch
            summary = {"status": status, "reason_code": reason,
                "details": {"stop_evidence": {"stopped": True, "source": "ex-framework",
                    "reference": self.goals.ex_session + ":" + str(proof_epoch)},
                    "terminal_evidence": {"verified": True, "goal_id": goal.payload()["goal_id"],
                        "goal_revision": goal.revision, "stop_proof_epoch": proof_epoch,
                        "dispatcher_epoch": self.actions.dispatcher._epoch, "commands": commands}}}
            if status == "failed":
                summary["details"]["failure_evidence"] = {"verified": True,
                    "goal_id": goal.payload()["goal_id"], "goal_revision": goal.revision,
                    "commands": failure_evidence}
        hook(goal, epoch, summary)

    def _process_control(self) -> None:
        """Bounded safety work independent of observation/backend reads."""
        with self._condition:
            self._settle_observed_stop()
            if self.goals.expire():
                self._stop_pending = True
            review, self._review_future = self._review_future, None
            epoch = self.goals.gate_epoch
            stopping = self._stop_pending
            if stopping and self._stop_attempt_epoch != epoch:
                self._stop_attempt_epoch = epoch
                self._stop_attempts = 0
                self._next_stop_time = 0
                self._stop_request_retry = False
            if stopping and time.monotonic() >= self._next_stop_time:
                self._stop_pending = False
            else:
                stopping = False
            reason = self.goals.reason
            stop_dispatcher_epoch = self._stop_dispatcher_epoch
            goal, _, phase = self.goals.current()
            passive_stop = (self.mode == "disabled" and goal is None and self.goals.pending_replace is None
                            and phase == "stopping")
        if review is not None and not review.set_running_or_notify_cancel():
            review = None
        if review is not None:
            try:
                if not self.actions.stop_actions("explicit_review"):
                    raise RuntimeError(self.actions._last_error or "stop_not_proven")
                self.actions.dispatcher.review_stops().result(self._io_timeout)
                if not self.goals.reviewed(epoch):
                    raise RuntimeError("review_superseded")
                with self._condition:
                    self._stop_pending = False
                    self._stop_error = ""
                review.set_result({"ok": True, "new_authorization_required": True})
            except Exception as exc:
                with self._condition:
                    self._stop_error = f"stop review unavailable: {type(exc).__name__}: {exc}"[:256]
                review.set_exception(exc)
                # A failed/canceled review must not consume an independent stop.
                if stopping:
                    with self._condition:
                        self._stop_pending = True
            return
        if not stopping:
            return
        self._stop_attempts += 1
        controlled = self.completion_hook is not None and self.actions.control_mode == "decision"
        try:
            if self._stop_attempts == 1:
                proven = (self.actions.stop_actions(reason, decision_controlled=True) if controlled else
                          self.actions.stop_actions(reason, after_epoch=stop_dispatcher_epoch)
                          if passive_stop else self.actions.stop_actions(reason))
            else:
                if self._stop_request_retry:
                    # A failed request scan may never have reached cancel. Retry
                    # only that request, never on every subsequent poll failure.
                    if controlled:
                        self.actions.request_stops(reason, decision_controlled=True)
                    else:
                        self.actions.request_stops(reason)
                proven = self.actions.await_stop_proof(reason)
            self._stop_request_retry = not proven and "unavailable" in (self.actions._last_error or "")
            error = "" if proven else self.actions._last_error or "stop_not_proven"
        except Exception as exc:
            proven = False
            self._stop_request_retry = True
            error = f"stop request unavailable: {type(exc).__name__}: {exc}"
        with self._condition:
            self._stop_error = error[:256]
            if self.goals.gate_epoch != epoch:
                return  # a new explicit intent keeps its own pending flag
            if controlled and proven and self.actions.dispatcher.blocked:
                if self.goals.phase != "blocked":
                    self.goals.block("action_fault_requires_explicit_review")
                return  # full physical proof never clears an independent fault latch
            phase_before = self.goals.phase
            terminal = self.goals._terminal_stop
            terminal_hook = (self.completion_hook is not None and terminal is not None
                             and terminal[1] == epoch and phase_before == "stopping")
            activated = self.goals.resolve_stop(epoch, proven,
                activate=self.mode != "disabled" and self.actions.control_mode == "decision" and
                self.actions._runtime_state in {"ready", "running"}) if (
                    phase_before != "awaiting_llm" and not terminal_hook) else False
            if not proven:
                # resolve_stop already marks an ordinary failed stop blocked;
                # do not revoke/change goal epoch repeatedly while proving it.
                if self.goals.phase != "blocked":
                    self.goals.block("stop_not_proven")
                self._stop_attempt_epoch = self.goals.gate_epoch
                self._stop_pending = self._stop_attempts < 3
                self._next_stop_time = time.monotonic() + 0.1 * 2 ** (self._stop_attempts - 1)
        if proven and terminal_hook:
            self._finish_proven_terminal(terminal)
        if proven and phase_before == "awaiting_llm":
            self._finish_proven_completion(goal, epoch)
        if activated:
            try:
                self.actions.dispatcher.review_stops().result(self._io_timeout)
                with self._condition, self.goals._lock:
                    if (self.goals.gate_epoch == epoch and self.goals.phase == "active" and self.mode == "execute"
                            and getattr(self.backend, "execution_allowed", False) is True):
                        self.actions.dispatcher.set_gate(True)
                self._dirty = True
            except Exception as exc:
                with self._condition:
                    if self.goals.phase != "blocked":
                        self.goals.block("dispatcher_review_failed")
                    self._stop_error = f"dispatcher review unavailable: {type(exc).__name__}: {exc}"[:256]

    def _step(self) -> None:
        now = self._clock()
        if self.environment is not None:
            self._environment_snapshot = self.environment.snapshot()
        catalog = self.catalog.snapshot()
        if catalog.revision != self._last_catalog_revision:
            self.observations.configure(catalog.entries)
            if self._last_catalog_revision >= 0 and self.goals.current()[0] is not None:
                self.request_stop("catalog_revision_changed")
            self._last_catalog_revision = catalog.revision
            self._dirty = True
        framework = (self.actions._runtime_state, self.actions._config_revision,
                     self.actions._environment_revision, self.actions.control_mode)
        if (self._last_framework_versions is not None and framework != self._last_framework_versions
                and (self.goals.current()[0] is not None or self.goals.pending_replace is not None)):
            self.request_stop("framework_versions_changed")
        self._last_framework_versions = framework
        with self._condition:
            self._settle_observed_stop()
        if now - self._last_poll_ns >= 20_000_000:
            self._poll_rows()
            self._last_poll_ns = now
            self._progress()
        self._process_control()
        with self._condition:
            snapshot = self._live_snapshot
            if snapshot and now - snapshot.created_monotonic_ns >= self._timeout_ns and not self._timed_out:
                self._timed_out = True
                self._record(snapshot, "discarded", "backend_timeout_live_request_retained")
                self._failures = min(8, self._failures + 1)
                self._next_request_ns = now + min(30_000_000_000, self._interval_ns * 2**self._failures)
            response = self._backend_result
            if response is not None:
                self._backend_result = None
                timed_out = self._timed_out
                self._live_snapshot = None
                self._timed_out = False
            else:
                timed_out = False
        if response is not None:
            result, error = response
            if timed_out:
                self._record(snapshot, "discarded", "backend_late_after_timeout")
            elif error is not None:
                self._record(snapshot, "discarded", f"backend_error:{type(error).__name__}")
                self._failures = min(8, self._failures + 1)
                self._next_request_ns = now + min(30_000_000_000, self._interval_ns * 2**self._failures)
            else:
                try:
                    self._apply(snapshot, BackendDecision.parse(result.to_dict()))
                except (_ApplicationIOError, OSError, FutureTimeout, LedgerBusy, LedgerClosed, LedgerFault, DispatcherBusy):
                    # Execution/storage failures are worker faults, not invalid
                    # backend choices. _run revokes authority before publishing
                    # them, then the independent control path requests stop.
                    raise
                except Exception as exc:
                    reason = exc.error.code + ":" + exc.error.path if isinstance(exc, ContractError) else str(exc)
                    self._record(snapshot, "discarded", reason)
                else:
                    self._failures = 0
        with self._condition:
            if self._live_snapshot is None and self._dirty and now >= self._next_request_ns:
                self._dirty = False
                snapshot = self.build_snapshot()
                if snapshot and snapshot.owners:
                    self._live_snapshot = snapshot
                    self._backend_input = snapshot
                    self._next_request_ns = now + self._interval_ns
                    self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._closed:
                    return
            try:
                self._process_control()
                self._step()
            except Exception as exc:
                with self._condition:
                    self._settle_observed_stop()
                    owns_control = self._owns_control()
                    if owns_control and self.goals.phase != "blocked":
                        self.goals.block(f"decision_worker_error:{type(exc).__name__}")
                        self._stop_pending = True
                    self._record(None, "blocked" if owns_control else "observation_error", str(exc))
            with self._condition:
                if not self._closed:
                    self._condition.wait(timeout=0.01)

    def status(self) -> dict:
        with self._lock:
            with self.actions.dispatcher._lock:
                gate = self.actions.dispatcher._gate
            commands = [{"command_id": r.command_id, "owner": r.owner, "generation": r.generation,
                         "status": r.status, "held_resources": list(r.held_resources)}
                        for r in self._rows if r.status not in {ActionStatus.SUCCEEDED, ActionStatus.REJECTED}]
            return {"mode": self.mode, "execution_allowed": getattr(self.backend, "execution_allowed", False) is True,
                    "goals": self.goals.status(), "gate_open": gate,
                    "blocked": self.goals.phase == "blocked" or self.actions.dispatcher.blocked,
                    "control_mode": self.actions.control_mode, "unresolved": commands[:128],
                    "error": self._last_error, "stop_error": self._stop_error,
                    "stop_attempts": self._stop_attempts, "faults": list(self.actions.dispatcher.faults),
                    "backend_live": self._live_snapshot is not None, "backend_timed_out": self._timed_out,
                    "config_revision": self.config_revision, "decisions": copy.deepcopy(list(self._decisions)),
                    "catalog_revision": self.catalog.snapshot().revision,
                    "environment_revision": self.actions._environment_revision,
                    "closed": self._closed}

    def snapshot(self) -> dict | None:
        with self._lock:
            return self._last_snapshot.to_dict() if self._last_snapshot else None

    def close(self, timeout: float = 3) -> None:
        with self._lock:
            if self._closed:
                return
        self.request_stop("decision_service_close")
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        errors = []
        try:
            self.backend.close()
        except Exception as exc:
            errors.append(exc)
        try:
            proven = self.actions.stop_actions("decision_service_close")
            if not proven:
                self.goals.block("close_stop_not_proven")
        except Exception as exc:
            self.goals.block("close_stop_not_proven")
            errors.append(exc)
        for worker in (self._backend_worker, self._control):
            try:
                worker.join(timeout)
            except Exception as exc:
                errors.append(exc)
        try:
            self.observations.close()
        except Exception as exc:
            errors.append(exc)
        with self._condition:
            review, self._review_future = self._review_future, None
        if review is not None and review.set_running_or_notify_cancel():
            review.set_exception(RuntimeError("decision service closed"))
        if self._backend_worker.is_alive() or self._control.is_alive():
            errors.append(TimeoutError("decision worker did not cooperate with close"))
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ShutdownErrors("Decision service shutdown failed", errors)
