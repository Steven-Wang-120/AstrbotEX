"""Bounded, fail-closed B02 action dispatcher. Framework wiring supplies all authority."""
from __future__ import annotations

import json
import math
import queue
import threading
import time
from collections import deque
from concurrent.futures import CancelledError, Future, InvalidStateError
from dataclasses import dataclass
from typing import Any, Callable

from .ledger import ActionLedger, ActionSnapshot, OwnerBinding, StopEvidence
from .models import (
    ActionCommand, ActionManifestV2, ActionStatus, CommandOperation, ContractError,
    ErrorCode, KNOWN_RUNTIME_STATES, MAX_ID_LEN, MAX_LEASE_MS, MAX_SEQUENCE,
    TERMINAL_STATUSES, measure_json_budget, parse_action_manifest, reject,
    require_id, require_sequence, validate_command_against_manifest, validate_params,
)
from ..plugin_actor import ActionGuardRejected, ActionMailboxFull, PluginActor


class DispatcherBusy(RuntimeError):
    pass


def _fail_report(future: Future[Any], error: Exception) -> None:
    if future.done():
        return
    try:
        future.set_exception(error)
    except InvalidStateError:
        # Cancellation/completion can race the check; do not strand later reports.
        if not future.done():
            raise


@dataclass(frozen=True)
class Observation:
    source: str
    topic: str
    fields_json: str
    received_ns: int
    age_ms: float


@dataclass(frozen=True)
class TrustedContext:
    ex_session: str
    goal_id: str
    goal_revision: int
    task_id: str
    allowed_actions: frozenset[str]
    bound_params: tuple[tuple[str, str], ...]
    runtime_state: str
    catalog_revision: int
    config_revision: int
    environment_revision: int
    gate_epoch: int
    received_ns: int
    expires_ns: int
    observations: tuple[Observation, ...]


@dataclass
class _Owner:
    binding: OwnerBinding
    actor: PluginActor
    manifest: ActionManifestV2


@dataclass
class _Live:
    command: ActionCommand
    binding: OwnerBinding
    context: TrustedContext
    owner: _Owner
    deadline_ns: int
    cancel_deadline_ns: int = 0
    status: str = ActionStatus.ADMITTED
    delivered: bool = False
    actor_future: Future[Any] | None = None
    callback_started_ns: int = 0
    callback_finished_ns: int = 0
    callback_slow: bool = False
    stop_requested: bool = False
    budget_baseline: int = 0
    deferred_reports: list[_ControlItem] = None


@dataclass
class _PendingStart:
    command: ActionCommand
    binding: OwnerBinding
    deadline_ns: int
    cancel_requested: bool = False


@dataclass
class _ControlItem:
    work: Callable[[], Any]
    future: Future[Any] | None
    start_id: str = ""


class ActionDispatcher:
    """Explicitly enabled core; no runtime/TopicBus coupling or automatic replay.

    The caller owns ledger and actors. Framework-only methods register_owner,
    update_context and set_gate must not be exposed to plugins or wire requests.
    A single bounded worker serializes ledger writes and callback outcomes; its
    Future callbacks only enqueue work, never wait for the ledger writer.
    """

    def __init__(self, ledger: ActionLedger, *, queue_size: int = 512,
                 watchdog_interval: float = 0.01,
                 emergency_stop: Callable[[str], None] | None = None) -> None:
        if type(queue_size) is not int or queue_size < 1:
            raise ValueError("queue_size must be positive")
        self.ledger = ledger
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._queue: deque[_ControlItem] = deque()
        self._priority: deque[_ControlItem] = deque()
        self._queue_size = queue_size
        self._priority_size = max(8, min(128, queue_size))
        self._pending: dict[str, list[_PendingStart]] = {}
        self._faults: deque[str] = deque(maxlen=64)
        self._stop_reasons: queue.Queue[str] = queue.Queue(maxsize=16)
        self._owners: dict[str, _Owner] = {}
        self._context: TrustedContext | None = None
        self._versions: tuple[int, int, int] | None = None
        self._runtime_state: str | None = None
        self._live: dict[str, _Live] = {}
        self._timeout_pending: dict[str, str] = {}
        self._timeout_enqueued: set[str] = set()
        self._gate = False
        self._epoch = 0
        self._blocked = False
        self._closed = False
        self._recovery_complete = False
        self._review_required = False
        self._emergency_stop = emergency_stop
        self._wake = threading.Event()
        self._notifier = threading.Thread(target=self._notify_stop, name="action-dispatch-stop-notify", daemon=True)
        self._worker = threading.Thread(target=self._work, name="action-dispatch-control", daemon=True)
        self._watchdog = threading.Thread(target=self._watch, args=(watchdog_interval,),
                                          name="action-dispatch-watchdog", daemon=True)
        self._notifier.start()
        self._worker.start()
        self._watchdog.start()
        try:
            self.recover().result(timeout=15)
        except Exception:
            self._block("startup_recovery_failed")
            self.close()
            raise

    @property
    def blocked(self) -> bool:
        with self._lock:
            return self._blocked or self.ledger.health.admission_blocked

    @property
    def faults(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._faults)

    def _notify_stop(self) -> None:
        while True:
            try:
                reason = self._stop_reasons.get(timeout=0.05)
            except queue.Empty:
                if self._closed:
                    return
                continue
            try:
                if self._emergency_stop is not None:
                    self._emergency_stop(reason)
            except Exception as exc:
                with self._lock:
                    self._faults.append(f"emergency_stop_failed:{type(exc).__name__}: {exc}")

    def set_gate(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise TypeError("enabled must be bool")
        with self._lock:
            if enabled and (self._closed or self._blocked or not self._recovery_complete or
                            self._review_required or self.ledger.health.admission_blocked):
                raise RuntimeError("action gate blocked; review required")
            self._gate = enabled
            self._epoch += 1
            self._context = None  # never carry stale authorization across a gate change
            active = not enabled and any(live.status not in TERMINAL_STATUSES
                                         for live in self._live.values())
            self._wake.set()
        if active:
            self._block("gate_revoked")

    def revoke_decision_gate(self) -> None:
        """Trusted controlled stop: revoke now, retain every existing fault latch."""
        with self._lock:
            self._gate = False
            self._epoch += 1
            self._context = None
            self._wake.set()

    def update_versions(self, *, catalog_revision: int, config_revision: int,
                        environment_revision: int, runtime_state: str) -> None:
        """Framework notification: revoke every older goal snapshot immediately."""
        versions = tuple(require_sequence(v, n) for n, v in
                         (("catalog_revision", catalog_revision), ("config_revision", config_revision),
                          ("environment_revision", environment_revision)))
        if runtime_state not in KNOWN_RUNTIME_STATES:
            reject(ErrorCode.ENUM_VIOLATION, "runtime_state", "unknown runtime state")
        with self._lock:
            if self._versions != versions or self._runtime_state != runtime_state:
                self._gate = False
                self._epoch += 1
                self._context = None
                self._versions = versions
                self._runtime_state = runtime_state
                active = any(live.status not in TERMINAL_STATUSES
                             for live in self._live.values())
                self._wake.set()
            else:
                active = False
        if active:
            self._block("framework_version_revoked")

    def register_owner(self, binding: OwnerBinding, actor: PluginActor,
                       manifest: ActionManifestV2 | dict[str, Any]) -> None:
        self.ledger._validate_binding(binding)
        if not isinstance(actor, PluginActor):
            raise TypeError("owner must have a PluginActor")
        source = manifest.to_dict() if isinstance(manifest, ActionManifestV2) else manifest
        parsed = parse_action_manifest(source, owner=binding.owner)
        with self._lock:
            if binding.owner in self._owners:
                raise ValueError("remove existing owner before replacing generation")
            owner = _Owner(binding, actor, parsed)
            self._owners[binding.owner] = owner
            actor.set_action_guard(lambda cmd, identity=owner: self._execution_guard(cmd, identity))

    def remove_owner(self, binding: OwnerBinding) -> None:
        with self._lock:
            owner = self._owners.get(binding.owner)
            if owner is None or owner.binding != binding:
                raise ValueError("owner generation mismatch")
            self._gate = False
            self._epoch += 1
            self._context = None
            owner.actor.set_action_guard(None)
            del self._owners[binding.owner]
            active = any(live.owner is owner and live.status not in TERMINAL_STATUSES
                         for live in self._live.values())
        if active:
            self._block("owner_removed_during_action")

    def update_context(self, *, ex_session: str, goal_id: str, goal_revision: int,
                       task_id: str, allowed_actions: list[str] | tuple[str, ...],
                       bound_params: dict[str, dict[str, Any]], runtime_state: str,
                       catalog_revision: int, config_revision: int,
                       environment_revision: int, ttl_ms: int,
                       observations: dict[str, dict[str, Any]] | None = None) -> None:
        """Install a framework-origin snapshot; age/receive timestamps are sampled here."""
        session, goal, task = (require_id(v, n) for n, v in
                               (("ex_session", ex_session), ("goal_id", goal_id), ("task_id", task_id)))
        revision = require_sequence(goal_revision, "goal_revision")
        versions = tuple(require_sequence(v, n) for n, v in
                         (("catalog_revision", catalog_revision), ("config_revision", config_revision),
                          ("environment_revision", environment_revision)))
        if runtime_state not in KNOWN_RUNTIME_STATES:
            reject(ErrorCode.ENUM_VIOLATION, "runtime_state", "unknown runtime state")
        if type(ttl_ms) is not int or not 0 < ttl_ms <= MAX_LEASE_MS:
            reject(ErrorCode.RANGE_VIOLATION, "ttl_ms", "invalid TTL")
        if not isinstance(allowed_actions, (list, tuple)) or len(allowed_actions) > 256:
            reject(ErrorCode.INVALID_TYPE, "allowed_actions", "bounded array required")
        allowed = frozenset(require_id(value, "allowed_actions") for value in allowed_actions)
        if len(allowed) != len(allowed_actions) or not isinstance(bound_params, dict) or set(bound_params) != allowed:
            reject(ErrorCode.INVALID_TYPE, "bound_params", "exact action bindings required")
        frozen: list[tuple[str, str]] = []
        for action, params in bound_params.items():
            budget = measure_json_budget(params)
            if budget is not None or not isinstance(params, dict):
                reject(budget or ErrorCode.INVALID_TYPE, "bound_params", "invalid bounded JSON")
            frozen.append((action, json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                               allow_nan=False)))
        observed: list[Observation] = []
        now = time.monotonic_ns()
        for source, value in (observations or {}).items():
            require_id(source, "observations.source")
            if not isinstance(value, dict) or set(value) != {"topic", "fields", "age_ms"}:
                reject(ErrorCode.INVALID_TYPE, "observations", "topic, fields and age_ms required")
            topic = require_id(value["topic"], "observations.topic")
            fields, age = value["fields"], value["age_ms"]
            budget = measure_json_budget(fields)
            if not isinstance(fields, dict) or budget is not None:
                reject(budget or ErrorCode.INVALID_TYPE, "observations.fields", "invalid bounded JSON")
            if type(age) not in (int, float) or not math.isfinite(age) or age < 0 or age > MAX_LEASE_MS:
                reject(ErrorCode.RANGE_VIOLATION, "observations.age_ms", "invalid observation age")
            observed.append(Observation(source, topic, json.dumps(fields, sort_keys=True, ensure_ascii=False,
                                                                   allow_nan=False), now, float(age)))
        with self._lock:
            if self._versions is not None and (versions != self._versions or
                                                runtime_state != self._runtime_state):
                reject(ErrorCode.REVISION_CONFLICT, "context", "framework revisions differ")
            self._versions = versions
            self._runtime_state = runtime_state
            self._context = TrustedContext(session, goal, revision, task, allowed, tuple(sorted(frozen)),
                                           runtime_state, *versions, self._epoch, now,
                                           now + ttl_ms * 1_000_000, tuple(observed))

    def _check(self, command: ActionCommand, context: TrustedContext, owner: _Owner,
               now: int) -> None:
        if now >= context.expires_ns or now < context.received_ns:
            raise TimeoutError("trusted context expired")
        observed = {item.source: item for item in context.observations}
        for source in (owner.manifest.by_id(command.action_id).requires_observations
                       if owner.manifest.by_id(command.action_id) is not None else ()):
            item = observed.get(source)
            spec = owner.manifest.observation_sources[source]
            if item is None or item.age_ms + (now - item.received_ns) / 1_000_000 > spec["max_age_ms"]:
                raise TimeoutError(f"observation {source} expired")
        if (self._closed or self._blocked or not self._gate or
                self.ledger.health.admission_blocked or context is not self._context or
                context.gate_epoch != self._epoch or self._owners.get(command.owner) is not owner or
                self._versions != (context.catalog_revision, context.config_revision,
                                   context.environment_revision) or
                self._runtime_state != context.runtime_state):
            raise RuntimeError("action gate, version or ledger unavailable")
        if command.goal_id != context.goal_id:
            reject(ErrorCode.REVISION_CONFLICT, "goal_id", "goal identity mismatch")
        if command.plugin_generation != owner.binding.generation:
            reject(ErrorCode.OWNER_MISMATCH, "plugin_generation", "stale owner generation")
        declaration = validate_command_against_manifest(
            command, owner.manifest, current_ex_session=context.ex_session,
            current_revision=context.goal_revision, runtime_state=context.runtime_state,
            trusted_owner=owner.binding.owner, granted_operations=frozenset({CommandOperation.START}),
        )
        if command.action_id not in context.allowed_actions:
            reject(ErrorCode.UNKNOWN_ACTION, "action_id", "action not granted by trusted goal")
        params = dict(context.bound_params)[command.action_id]
        if json.dumps(command.params, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False) != params:
            reject(ErrorCode.ENUM_VIOLATION, "params", "params differ from trusted binding")
        if declaration.max_duration_ms is not None and command.lease_ms > declaration.max_duration_ms:
            reject(ErrorCode.RANGE_VIOLATION, "lease_ms", "lease exceeds action maximum")
        observed = {item.source: item for item in context.observations}
        for source in declaration.requires_observations:
            spec = owner.manifest.observation_sources[source]
            item = observed.get(source)
            if item is None or item.topic != spec["topic"] or any(
                    field not in json.loads(item.fields_json) for field in spec["required_fields"]):
                reject(ErrorCode.MISSING_FIELD, f"observations.{source}", "missing matching source/topic/fields")
            if item.age_ms + (now - item.received_ns) / 1_000_000 > spec["max_age_ms"]:
                raise TimeoutError(f"observation {source} expired")

    def _execution_guard(self, command: ActionCommand, owner: _Owner) -> bool:
        with self._lock:
            live = self._live.get(command.command_id)
            if (live is None or live.owner is not owner or not live.delivered or
                    live.status in TERMINAL_STATUSES or live.cancel_deadline_ns or
                    live.stop_requested or self._closed or self._blocked or
                    not self._gate or self.ledger.health.admission_blocked or
                    live.context is not self._context or live.context.gate_epoch != self._epoch or
                    self._owners.get(command.owner) is not owner or
                    self._versions != (live.context.catalog_revision, live.context.config_revision,
                                       live.context.environment_revision) or
                    self._runtime_state != live.context.runtime_state or
                    command.plugin_generation != owner.binding.generation):
                return False
            now = time.monotonic_ns()
            if now >= live.deadline_ns or now >= live.context.expires_ns or live.cancel_deadline_ns:
                return False
            declaration = owner.manifest.by_id(command.action_id)
            for item in live.context.observations:
                if (item.source in declaration.requires_observations and
                        item.age_ms + (now - item.received_ns) / 1_000_000 >
                        owner.manifest.observation_sources[item.source]["max_age_ms"]):
                    return False
            return True

    def _enqueue(self, work: Callable[[], Any], *, priority: bool = False,
                 start_id: str = "") -> Future[Any]:
        future: Future[Any] = Future()
        with self._condition:
            if self._closed:
                raise RuntimeError("dispatcher closed")
            lane = self._priority if priority else self._queue
            if len(lane) >= (self._priority_size if priority else self._queue_size):
                self._block("control_queue_full")
                raise DispatcherBusy("control queue full; action gate closed")
            lane.append(_ControlItem(work, future, start_id))
            self._condition.notify()
        return future

    def _block(self, reason: str) -> None:
        with self._lock:
            first = not self._blocked
            self._blocked = True
            self._review_required = True
            self._gate = False
            self._faults.append(reason)
            self._wake.set()
        if first and self._emergency_stop is not None:
            try:
                self._stop_reasons.put_nowait(reason)
            except queue.Full:
                with self._lock:
                    self._faults.append("emergency_stop_notification_full")

    def start(self, raw: ActionCommand | dict[str, Any]) -> Future[ActionSnapshot]:
        received_ns = time.monotonic_ns()
        command = ActionCommand.parse(raw.to_dict() if isinstance(raw, ActionCommand) else raw)
        if command.operation != CommandOperation.START:
            reject(ErrorCode.UNSUPPORTED_OPERATION, "operation", "use cancel() for existing commands")
        with self._lock:
            owner = self._owners.get(command.owner)
            context = self._context
            if owner is None or context is None:
                raise RuntimeError("no trusted owner/context")
            self._check(command, context, owner, time.monotonic_ns())
            pending = _PendingStart(command, owner.binding,
                                    received_ns + command.lease_ms * 1_000_000)
            # This mapping only revokes unadmitted work. Ledger is the canonical
            # identity and conflict arbiter for every admitted command.
            self._pending.setdefault(command.command_id, []).append(pending)
            try:
                future = self._enqueue(lambda: self._start(command, context, owner, pending),
                                       start_id=command.command_id)
            except Exception:
                self._discard_pending(pending)
                raise
        future.add_done_callback(lambda done, item=pending: self._discard_pending(item) if done.cancelled() else None)
        return future

    def _discard_pending(self, pending: _PendingStart) -> None:
        with self._lock:
            items = self._pending.get(pending.command.command_id)
            if items is not None and pending in items:
                items.remove(pending)
                if not items:
                    del self._pending[pending.command.command_id]

    def _start(self, command: ActionCommand, context: TrustedContext,
               owner: _Owner, pending: _PendingStart) -> ActionSnapshot:
        try:
            with self._lock:
                if pending.cancel_requested:
                    raise RuntimeError("start revoked by cancel")
                if time.monotonic_ns() >= pending.deadline_ns:
                    raise TimeoutError("command lease expired before admission")
                self._check(command, context, owner, time.monotonic_ns())
            declaration = owner.manifest.by_id(command.action_id)
            admission = self.ledger.admit(command, declaration.resources, owner.binding,
                                          task_id=context.task_id).result()
            if not admission.admitted_new:
                return admission.snapshot
            live = _Live(command, owner.binding, context, owner, pending.deadline_ns)
            live.deferred_reports = []
            live.budget_baseline = owner.actor.action_mailbox_stats()["action_budget_exceeded"]
            with self._lock:
                self._live[command.command_id] = live
                for item in self._pending.get(command.command_id, ()):
                    if item.cancel_requested:
                        live.stop_requested = True
                try:
                    if live.stop_requested or time.monotonic_ns() >= pending.deadline_ns:
                        raise TimeoutError("command revoked or lease expired")
                    self._check(command, context, owner, time.monotonic_ns())
                except (ContractError, RuntimeError, TimeoutError):
                    refused = True
                else:
                    refused = False
                    live.delivered = True
                    try:
                        result = owner.actor.submit_action(command)
                        live.actor_future = result
                    except ActionMailboxFull:
                        busy = True
                    except Exception:
                        busy = False
                        uncertain = True
                    else:
                        busy = uncertain = False
            if refused:
                return self._persist(command.command_id, ActionStatus.REJECTED, "execution_gate_closed")
            if busy:
                return self._persist(command.command_id, ActionStatus.REJECTED, "actor_busy")
            if uncertain:
                self._block("actor_submit_uncertain")
                return self._persist(command.command_id, ActionStatus.UNKNOWN, "actor_submit_uncertain")
            result.add_done_callback(lambda done, cid=command.command_id: self._actor_done(cid, done))
            return admission.snapshot
        finally:
            self._discard_pending(pending)

    def _timeout_reason(self, live: _Live, now: int) -> str:
        """Called under _lock; expiry must not depend on watchdog queue order."""
        if live.status in TERMINAL_STATUSES:
            return ""
        if live.actor_future is not None and live.actor_future.running() and not live.callback_started_ns:
            live.callback_started_ns = now
        if (live.callback_started_ns and not live.callback_finished_ns and
                now - live.callback_started_ns > 20_000_000):
            live.callback_slow = True
        if live.callback_slow:
            return "actor_callback_budget_exceeded"
        pending = self._timeout_pending.get(live.command.command_id)
        if pending:
            return pending
        if now >= live.deadline_ns or (live.cancel_deadline_ns and now >= live.cancel_deadline_ns):
            return "action_deadline"
        return ""

    def _persist(self, command_id: str, status: str, reason: str = "",
                 details: dict[str, Any] | None = None, evidence: StopEvidence | None = None) -> ActionSnapshot:
        with self._lock:
            live = self._live.get(command_id)
            if live is None:
                raise KeyError(command_id)
            timeout_reason = self._timeout_reason(live, time.monotonic_ns())
        if timeout_reason and status != ActionStatus.TIMED_OUT:
            self._block(timeout_reason)
            self._persist(command_id, ActionStatus.TIMED_OUT, timeout_reason)
        elif timeout_reason:
            reason = timeout_reason
        with self._lock:
            terminal = live.status in TERMINAL_STATUSES
            if terminal and not (status == ActionStatus.CANCELED and evidence is not None and
                                 live.status in (ActionStatus.UNKNOWN, ActionStatus.TIMED_OUT,
                                                 ActionStatus.FAILED)):
                if status != live.status:
                    reject(ErrorCode.ILLEGAL_TRANSITION, "status", "terminal status cannot change")
                unchanged = True
            else:
                unchanged = False
            if status == ActionStatus.CANCELED and not live.cancel_deadline_ns and not terminal:
                raise ValueError("canceled requires a requested stop")
        if unchanged:
            return self.ledger.get(command_id).result()
        if terminal:
            return self.ledger.reconcile_stop(command_id, live.binding, evidence).result()
        if status == ActionStatus.FAILED:
            with self._lock:
                self._gate = False
                self._epoch += 1
                self._context = None  # revoke queued starts before persistence waits
                self._wake.set()
        try:
            snapshot = self.ledger.report(command_id, live.binding, status, reason_code=reason,
                                          details=details, stop_evidence=evidence).result()
            stopped_failure = False
            if snapshot.status == ActionStatus.FAILED and not snapshot.held_resources:
                proof = self.ledger.stop_proof(command_id, live.binding).result()
                stopped_failure = (proof is not None and proof.stopped is True
                    and snapshot.details.get("stop_evidence") == {
                        "stopped": True, "source": proof.source, "reference": proof.reference})
        except Exception:
            self._block("ledger_report_failed")
            raise
        with self._lock:
            live.status = snapshot.status
            if snapshot.status in TERMINAL_STATUSES:
                self._timeout_pending.pop(command_id, None)
                self._timeout_enqueued.discard(command_id)
            uncertain = (snapshot.status in (ActionStatus.UNKNOWN, ActionStatus.TIMED_OUT,
                                              ActionStatus.FAILED) and not stopped_failure)
            deferred = list(live.deferred_reports) if snapshot.status in TERMINAL_STATUSES else []
            if deferred:
                live.deferred_reports.clear()
            self._wake.set()
        for item in deferred:
            _fail_report(item.future, RuntimeError("action became terminal before callback completed"))
        if uncertain:
            self._block("uncertain_action")
        return snapshot

    def _actor_done(self, command_id: str, done: Future[Any]) -> None:
        with self._lock:
            live = self._live.get(command_id)
            if live is not None:
                live.callback_finished_ns = time.monotonic_ns()
                if (live.callback_started_ns and
                        live.callback_finished_ns - live.callback_started_ns > 20_000_000):
                    live.callback_slow = True
                if not done.cancelled() and not isinstance(done.exception(), ActionGuardRejected):
                    duration = live.owner.actor.action_mailbox_stats()
                    if duration["action_budget_exceeded"] > live.budget_baseline and (
                            duration["last_action_duration_ms"] or 0) > 20:
                        live.callback_slow = True
        if live is not None and live.callback_slow:
            self._block("actor_callback_budget_exceeded")
        with self._condition:
            if self._closed or len(self._priority) >= self._priority_size:
                if live is not None:
                    for item in live.deferred_reports:
                        _fail_report(item.future, DispatcherBusy("actor result queue full"))
                    live.deferred_reports.clear()
                self._block("actor_result_not_queued")
            else:
                self._priority.append(_ControlItem(lambda: self._handle_actor(command_id, done), None))
                if live is not None:
                    for item in live.deferred_reports:
                        if live.callback_slow:
                            _fail_report(item.future, RuntimeError("actor callback exceeded budget"))
                        elif len(self._priority) < self._priority_size:
                            self._priority.append(item)
                        else:
                            _fail_report(item.future, DispatcherBusy("priority report queue full"))
                            self._block("deferred_report_delivery_full")
                    live.deferred_reports.clear()
                self._condition.notify_all()

    def _handle_actor(self, command_id: str, done: Future[Any]) -> None:
        with self._lock:
            live = self._live.get(command_id)
            if live is None or live.status in TERMINAL_STATUSES:
                return
            timeout_reason = self._timeout_reason(live, time.monotonic_ns())
        if timeout_reason:
            self._block(timeout_reason)
            self._persist(command_id, ActionStatus.TIMED_OUT, timeout_reason)
            return
        if live.status != ActionStatus.ADMITTED:
            return
        try:
            result = done.result()
        except CancelledError:
            # The Actor start was removed before invocation. The stop request
            # still needs proof (or a visible cancellation timeout).
            return
        except ActionGuardRejected:
            self._persist(command_id, ActionStatus.REJECTED, "execution_guard_rejected")
        except Exception:
            self._persist(command_id, ActionStatus.UNKNOWN, "actor_callback_failed")
        else:
            if result == "accepted" or (isinstance(result, dict) and result.get("status") == "accepted"):
                self._persist(command_id, ActionStatus.ACCEPTED)
            elif result == "rejected" or (isinstance(result, dict) and result.get("status") == "rejected"):
                self._persist(command_id, ActionStatus.REJECTED, "plugin_rejected")
            else:
                self._persist(command_id, ActionStatus.UNKNOWN, "invalid_actor_response")

    def report(self, command_id: str, binding: OwnerBinding, status: str, *,
               details: dict[str, Any] | None = None, reason_code: str = "",
               stop_evidence: StopEvidence | None = None) -> Future[ActionSnapshot]:
        require_id(command_id, "command_id")
        self.ledger._validate_binding(binding)
        if status not in (ActionStatus.RUNNING, ActionStatus.SUCCEEDED, ActionStatus.FAILED,
                          ActionStatus.CANCELED, ActionStatus.UNKNOWN):
            raise ValueError("plugin report status unsupported")
        payload = {} if details is None else details
        budget = measure_json_budget(payload)
        if not isinstance(payload, dict) or budget is not None or "stop_evidence" in payload:
            reject(budget or ErrorCode.INVALID_TYPE, "details", "invalid details")
        frozen = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        if type(reason_code) is not str or len(reason_code) > MAX_ID_LEN:
            raise ValueError("invalid reason_code")
        with self._lock:
            live = self._live.get(command_id)
            if live is None or live.binding != binding or self._owners.get(binding.owner) is not live.owner:
                reject(ErrorCode.OWNER_MISMATCH, "binding", "unbound report")
            if status == ActionStatus.CANCELED and not live.cancel_deadline_ns:
                raise ValueError("canceled requires requested stop")
            if (live.delivered and not live.callback_finished_ns and
                    status in (ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.UNKNOWN)):
                if len(live.deferred_reports) >= 64:
                    self._block("deferred_report_full")
                    raise DispatcherBusy("pending plugin reports full")
                future: Future[ActionSnapshot] = Future()
                live.deferred_reports.append(_ControlItem(
                    lambda: self._plugin_report(command_id, status, reason_code,
                                                frozen, stop_evidence), future))
                return future
        return self._enqueue(lambda: self._plugin_report(command_id, status, reason_code,
                                                          frozen, stop_evidence), priority=True)

    def _plugin_report(self, command_id: str, status: str, reason: str,
                       details: dict[str, Any], evidence: StopEvidence | None) -> ActionSnapshot:
        with self._lock:
            live = self._live[command_id]
            promote = live.status == ActionStatus.ADMITTED and status in (
                ActionStatus.RUNNING, ActionStatus.SUCCEEDED, ActionStatus.FAILED)
        if promote:
            self._persist(command_id, ActionStatus.ACCEPTED)
        return self._persist(command_id, status, reason, details, evidence)

    def cancel(self, command_id: str, binding: OwnerBinding, reason: str = "stop") -> Future[ActionSnapshot]:
        require_id(command_id, "command_id")
        self.ledger._validate_binding(binding)
        if not isinstance(reason, str) or len(reason) > MAX_ID_LEN:
            raise ValueError("invalid cancel reason")
        with self._lock:
            pendings = self._pending.get(command_id, ())
            live = self._live.get(command_id)
            if pendings:
                if any(item.binding != binding for item in pendings):
                    reject(ErrorCode.OWNER_MISMATCH, "binding", "unbound cancel")
                for item in pendings:
                    item.cancel_requested = True
            if live is None:
                if pendings:
                    raise RuntimeError("unadmitted start revoked; no durable command to cancel")
                raise KeyError(command_id)
            if live.binding != binding or self._owners.get(binding.owner) is not live.owner:
                reject(ErrorCode.OWNER_MISMATCH, "binding", "unbound cancel")
            declaration = live.owner.manifest.by_id(live.command.action_id)
            if CommandOperation.CANCEL not in declaration.operations:
                reject(ErrorCode.CANCEL_UNSUPPORTED, "operation", "cancel not declared")
            if not live.stop_requested:
                live.stop_requested = True  # actor guard sees this before any control queue work
                live.cancel_deadline_ns = time.monotonic_ns() + declaration.cancel_timeout_ms * 1_000_000
                self._wake.set()
                actor_future = live.actor_future
                if actor_future is not None:
                    actor_future.cancel()  # queued start is removed by Actor; running calls cannot be killed
                try:
                    stop = live.owner.actor.submit_action_cancel(command_id, reason)
                except Exception:
                    stop = None
            else:
                stop = False
        if stop is None:
            self._block("cancel_delivery_failed")
            return self._enqueue(lambda: self._persist(command_id, ActionStatus.UNKNOWN,
                                                         "cancel_delivery_failed"), priority=True)
        if stop is not False:
            stop.add_done_callback(lambda done, cid=command_id: self._cancel_done(cid, done))
        return self._enqueue(lambda: self.ledger.get(command_id).result(), priority=True)

    def _cancel_done(self, command_id: str, done: Future[Any]) -> None:
        try:
            self._enqueue(lambda: self._check_cancel_result(command_id, done), priority=True)
        except (DispatcherBusy, RuntimeError):
            self._block("cancel_result_not_queued")

    def _check_cancel_result(self, command_id: str, done: Future[Any]) -> None:
        try:
            done.result()
        except Exception:
            self._persist(command_id, ActionStatus.UNKNOWN, "cancel_callback_failed")
        # Acknowledging a stop request is not evidence that the device stopped.

    def reconcile_stop(self, command_id: str, binding: OwnerBinding,
                       evidence: StopEvidence) -> Future[ActionSnapshot]:
        with self._lock:
            live = self._live.get(command_id)
            if live is None:
                raise KeyError(command_id)
            if live.binding != binding or self._owners.get(binding.owner) is not live.owner:
                reject(ErrorCode.OWNER_MISMATCH, "binding", "unbound stop proof")
        return self._enqueue(lambda: self.ledger.reconcile_stop(command_id, binding, evidence).result(),
                             priority=True)

    def reconcile_recovered_stop(self, command_id: str, binding: OwnerBinding,
                                 evidence: StopEvidence) -> Future[ActionSnapshot]:
        """Framework-supplied proof for an owner no longer in memory after restart."""
        self.ledger._validate_binding(binding)
        self.ledger._validate_stop(command_id, evidence)
        with self._lock:
            if not self._recovery_complete or not self._review_required:
                raise RuntimeError("recovered stop requires blocked recovery review")
            if command_id in self._live:
                raise ValueError("use reconcile_stop for live dispatcher command")
        return self._enqueue(lambda: self.ledger.reconcile_stop(command_id, binding, evidence).result(),
                             priority=True)

    def query(self, command_id: str) -> Future[ActionSnapshot | None]:
        return self.ledger.get(command_id)

    def recover(self) -> Future[tuple[ActionSnapshot, ...]]:
        """Inspect durable records after restart; never redispatch or open gate."""
        def load() -> tuple[ActionSnapshot, ...]:
            rows: list[ActionSnapshot] = []
            cursor = ""
            while True:
                page = self.ledger.list_commands(after_id=cursor, limit=500).result()
                rows.extend(page)
                if len(page) < 500:
                    break
                cursor = page[-1].command_id
            with self._lock:
                uncertain = any(row.status in (ActionStatus.UNKNOWN, ActionStatus.TIMED_OUT,
                                                ActionStatus.FAILED) for row in rows)
                self._recovery_complete = True
                if uncertain:
                    self._review_required = True
            if uncertain:
                self._block("recovery_review")
            return tuple(rows)
        return self._enqueue(load)

    def review_stops(self) -> Future[tuple[ActionSnapshot, ...]]:
        """Explicit framework review after physical stop; never opens the gate."""
        def review() -> tuple[ActionSnapshot, ...]:
            rows: list[ActionSnapshot] = []
            cursor = ""
            while True:
                page = self.ledger.list_commands(after_id=cursor, limit=500).result()
                rows.extend(page)
                if len(page) < 500:
                    break
                cursor = page[-1].command_id
            for row in rows:
                if row.status in (ActionStatus.ADMITTED, ActionStatus.ACCEPTED, ActionStatus.RUNNING):
                    raise RuntimeError("live action remains")
                if row.status in (ActionStatus.UNKNOWN, ActionStatus.TIMED_OUT, ActionStatus.FAILED):
                    proof = self.ledger.stop_proof(row.command_id,
                                                   OwnerBinding(row.owner, row.generation)).result()
                    if proof is None or row.held_resources:
                        raise RuntimeError("uncertain action lacks committed stop proof")
            if self.ledger.health.admission_blocked:
                raise RuntimeError("ledger fault requires new recovered ledger")
            with self._lock:
                self._blocked = False
                self._review_required = False
                self._gate = False
                self._context = None
            return tuple(rows)
        return self._enqueue(review)

    def _watch(self, interval: float) -> None:
        while not self._closed:
            self._wake.wait(max(0.001, interval))
            self._wake.clear()
            now = time.monotonic_ns()
            with self._lock:
                for cid, live in self._live.items():
                    if live.status in TERMINAL_STATUSES:
                        self._timeout_pending.pop(cid, None)
                        continue
                    reason = self._timeout_reason(live, now)
                    if reason:
                        self._timeout_pending[cid] = reason
                retry = [(cid, reason) for cid, reason in self._timeout_pending.items()
                         if cid not in self._timeout_enqueued]
            for cid, reason in retry:
                with self._lock:
                    live = self._live.get(cid)
                    if (self._timeout_pending.get(cid) != reason or cid in self._timeout_enqueued
                            or live is None or live.status in TERMINAL_STATUSES):
                        continue
                    # A captured retry must not close a gate reviewed after its timeout.
                    self._block(reason)
                    self._timeout_enqueued.add(cid)
                try:
                    self._enqueue(lambda name=cid, why=reason: self._timeout(name, why),
                                  priority=True)
                except (DispatcherBusy, RuntimeError):
                    with self._lock:
                        self._timeout_enqueued.discard(cid)
                    # Retry on the next tick; gate remains closed.

    def _timeout(self, command_id: str, reason: str) -> None:
        with self._lock:
            live = self._live.get(command_id)
            pending = live is not None and live.status not in TERMINAL_STATUSES
        try:
            if pending:
                self._persist(command_id, ActionStatus.TIMED_OUT, reason)
        finally:
            with self._lock:
                self._timeout_enqueued.discard(command_id)
                if not pending:
                    self._timeout_pending.pop(command_id, None)
                self._wake.set()

    def _work(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._priority or self._queue or self._closed,
                                         timeout=0.05)
                if self._priority:
                    item = self._priority.popleft()
                elif self._queue:
                    item = self._queue.popleft()
                elif self._closed:
                    break
                else:
                    continue
            if item.future is not None and not item.future.set_running_or_notify_cancel():
                continue
            try:
                result = item.work()
            except BaseException as exc:
                if item.future is not None:
                    item.future.set_exception(exc)
                else:
                    self._block(f"control_callback_failed:{type(exc).__name__}")
            else:
                if item.future is not None:
                    item.future.set_result(result)

    def close(self, timeout: float = 5) -> None:
        with self._condition:
            self._gate = False
            self._closed = True
            self._wake.set()
            self._condition.notify_all()
            for owner in self._owners.values():
                owner.actor.set_action_guard(None)
        self._worker.join(timeout)
        self._watchdog.join(timeout)
        self._notifier.join(timeout)
        if self._worker.is_alive() or self._watchdog.is_alive() or self._notifier.is_alive():
            raise TimeoutError("dispatcher did not drain")
