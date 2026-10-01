"""Framework-owned action lifecycle. No goal authorization is derived from wire payloads."""
from __future__ import annotations

import time
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from .dispatcher import ActionDispatcher
from .ledger import ActionLedger, OwnerBinding
from .models import ActionStatus


class _StopProofDeadline(TimeoutError):
    """The service budget expired, not a Ledger I/O failure."""


class ActionService:
    def __init__(self, ledger: ActionLedger, dispatcher: ActionDispatcher, catalog: Any,
                 *, stop_timeout: float = 2.0) -> None:
        self.ledger = ledger
        self.dispatcher = dispatcher
        self.catalog = catalog
        self.stop_timeout = stop_timeout
        self.control_mode = "legacy"
        self._environment_revision = 1
        self._runtime_state = "idle"
        self._config_revision = 0
        self._catalog_revision = catalog.snapshot().revision
        self._last_error: str | None = None
        self._stop_proof_epoch = -1
        self.update_versions()

    def revoke(self) -> None:
        self.dispatcher.set_gate(False)

    def update_versions(self, *, environment_revision: int | None = None,
                        runtime_state: str | None = None,
                        config_revision: int | None = None) -> None:
        if environment_revision is not None:
            self._environment_revision = environment_revision
        if runtime_state is not None:
            self._runtime_state = runtime_state
        if config_revision is not None:
            self._config_revision = config_revision
        self._catalog_revision = self.catalog.snapshot().revision
        self.dispatcher.update_versions(
            catalog_revision=self._catalog_revision,
            config_revision=self._config_revision,
            environment_revision=self._environment_revision,
            runtime_state=self._runtime_state,
        )

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _StopProofDeadline("stop proof deadline expired")
        return remaining

    def _await_read(self, future, deadline: float):
        try:
            return future.result(timeout=max(0.0, deadline - time.monotonic()))
        except FutureTimeout as exc:
            # An unfinished Future exhausted the total wait budget. A Future
            # that completed with TimeoutError is an actual I/O error instead.
            if not future.done() and time.monotonic() >= deadline:
                raise _StopProofDeadline("stop proof deadline expired") from exc
            raise

    def _commands(self, deadline: float):
        cursor = ""
        while True:
            self._remaining(deadline)
            future = self.ledger.list_commands(after_id=cursor, limit=500)
            page = self._await_read(future, deadline)
            yield from page
            if len(page) < 500:
                break
            cursor = page[-1].command_id

    def _proven(self, row, deadline: float) -> bool:
        if row.status in (ActionStatus.SUCCEEDED, ActionStatus.REJECTED):
            return True
        if row.status in (ActionStatus.CANCELED, ActionStatus.UNKNOWN,
                          ActionStatus.TIMED_OUT, ActionStatus.FAILED):
            self._remaining(deadline)
            future = self.ledger.stop_proof(row.command_id, OwnerBinding(row.owner, row.generation))
            proof = self._await_read(future, deadline)
            return proof is not None and proof.stopped is True and not row.held_resources
        return False

    def _bounded_reason(self, reason: str) -> str:
        text = str(reason or "framework stop")
        return text[:256]

    def request_stops(self, reason: str, *, binding: OwnerBinding | None = None) -> tuple[str, ...]:
        """Revoke admission and initiate cancellation without awaiting proof."""
        self.revoke()
        bounded = self._bounded_reason(reason)
        requested: list[str] = []
        deadline = time.monotonic() + self.stop_timeout
        try:
            rows = [row for row in self._commands(deadline)
                    if binding is None or (row.owner, row.generation) == (binding.owner, binding.generation)]
            for row in rows:
                if row.status in (ActionStatus.ADMITTED, ActionStatus.ACCEPTED, ActionStatus.RUNNING,
                                   ActionStatus.UNKNOWN, ActionStatus.TIMED_OUT, ActionStatus.FAILED):
                    try:
                        self.dispatcher.cancel(row.command_id, OwnerBinding(row.owner, row.generation), bounded)
                    except (KeyError, RuntimeError, ValueError):
                        # Recovered rows have no live owner. They remain blocked until
                        # the framework supplies explicit reconcile_recovered_stop proof.
                        continue
                    requested.append(row.command_id)
        except Exception as exc:
            self._last_error = f"stop request unavailable: {type(exc).__name__}: {exc}"
        return tuple(requested)

    def await_stop_proof(self, reason: str = "framework stop", *, binding: OwnerBinding | None = None) -> bool:
        deadline = time.monotonic() + self.stop_timeout
        with self.dispatcher._lock:
            proof_epoch = getattr(self.dispatcher, "_epoch", -1)
        unproven = {}
        try:
            rows = [row for row in self._commands(deadline)
                    if binding is None or (row.owner, row.generation) == (binding.owner, binding.generation)]
            while True:
                self._remaining(deadline)
                unproven = dict.fromkeys(row.command_id for row in rows)
                for row in rows:
                    # Keep previously pending and not-yet-checked IDs if this
                    # pass reaches the deadline; neither is positive proof.
                    self._remaining(deadline)
                    future = self.ledger.get(row.command_id)
                    current = self._await_read(future, deadline)
                    if current is None:
                        raise RuntimeError(f"command record missing: {row.command_id}")
                    self._remaining(deadline)
                    proven = self._proven(current, deadline)
                    self._remaining(deadline)
                    if proven:
                        del unproven[row.command_id]
                if not unproven:
                    self._remaining(deadline)
                    self._last_error = None
                    if binding is None:
                        with self.dispatcher._lock:
                            if proof_epoch == getattr(self.dispatcher, "_epoch", -1):
                                self._stop_proof_epoch = proof_epoch
                    return True
                time.sleep(min(0.01, self._remaining(deadline)))
        except _StopProofDeadline:
            self._last_error = "stop proof pending: " + (", ".join(unproven) or "ledger scan incomplete")
            return False
        except Exception as exc:
            self._last_error = f"stop proof unavailable: {type(exc).__name__}: {exc}"
            return False

    def stop_actions(self, reason: str, *, binding: OwnerBinding | None = None,
                     after_epoch: int | None = None) -> bool:
        """Stop now, or acknowledge an already-proven trusted gate-revocation epoch."""
        if binding is None and after_epoch is not None and after_epoch >= 0:
            with self.dispatcher._lock:
                if self._stop_proof_epoch >= after_epoch:
                    return True
        self.request_stops(reason, binding=binding)
        return self.await_stop_proof(reason, binding=binding)

    def prove_owner_stop(self, slot: Any, reason: str) -> bool:
        self.request_stops(reason, binding=OwnerBinding(slot.id, slot.generation))
        return self.await_stop_proof(reason, binding=OwnerBinding(slot.id, slot.generation)) is True

    def change_mode(self, mode: str) -> None:
        if mode not in ("legacy", "decision"):
            raise ValueError("control_mode must be legacy or decision")
        self.revoke()
        if mode == self.control_mode:
            return
        if not self.stop_actions("control mode change"):
            raise RuntimeError(self._last_error or "action stop not proven")
        self.control_mode = mode
        self.update_versions()
        # Caller must explicitly start the runtime and reinstall goal authorization.

    def reconcile_recovered_stop(self, command_id: str, binding: OwnerBinding, evidence) -> Any:
        return self.dispatcher.reconcile_recovered_stop(command_id, binding, evidence)

    def start(self, command):
        if self.control_mode != "decision":
            raise RuntimeError("direct actions require decision control mode")
        return self.dispatcher.start(command)

    def cancel(self, command_id: str, binding: OwnerBinding, reason: str = "stop"):
        return self.dispatcher.cancel(command_id, binding, reason)

    def query(self, command_id: str):
        return self.dispatcher.query(command_id)

    def status(self) -> dict[str, Any]:
        deadline = time.monotonic() + self.stop_timeout
        uncertain = []
        try:
            unresolved = [row for row in self._commands(deadline)
                         if row.status not in (ActionStatus.SUCCEEDED, ActionStatus.REJECTED)]
            uncertain = [row for row in unresolved if row.status in (
                ActionStatus.UNKNOWN, ActionStatus.TIMED_OUT, ActionStatus.FAILED)
                         and not self._proven(row, deadline)]
            commands = [{"command_id": row.command_id, "owner": row.owner,
                         "generation": row.generation, "status": row.status,
                         "held_resources": list(row.held_resources)} for row in unresolved]
        except Exception as exc:
            commands = []
            self._last_error = f"action status unavailable: {type(exc).__name__}: {exc}"
        with self.dispatcher._lock:
            gate_open = self.dispatcher._gate
        return {"control_mode": self.control_mode, "gate_open": gate_open,
                "blocked": self.dispatcher.blocked or bool(uncertain) or bool(self._last_error),
                "unresolved": commands, "error": self._last_error,
                "faults": list(self.dispatcher.faults),
                "catalog_revision": self.catalog.snapshot().revision,
                "environment_revision": self._environment_revision,
                "config_revision": self._config_revision}

    def close(self) -> None:
        self.revoke()
        try:
            self.dispatcher.close()
        finally:
            self.ledger.close()
