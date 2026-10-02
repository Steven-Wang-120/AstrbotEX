"""Durable, no-replay action ledger. All SQLite access lives on one bounded writer thread."""

from __future__ import annotations

import json
import math
import queue
import sqlite3
import threading
import uuid
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from .models import (
    ActionCommand, ActionStatus, CommandOperation, ContractError, ErrorCode,
    LEGAL_TRANSITIONS, MAX_ID_LEN, MAX_SEQUENCE, can_transition, measure_json_budget,
    reject,
)

T = TypeVar("T")


class LedgerClosed(RuntimeError):
    pass


class LedgerBusy(RuntimeError):
    pass


class LedgerFault(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OwnerBinding:
    """Identity supplied by a trusted plugin binding, never by a report payload."""

    owner: str
    generation: int


@dataclass(frozen=True, slots=True)
class StopEvidence:
    """Positive structured evidence supplied by the bound plugin after safe stop."""

    command_id: str
    stopped: bool
    source: str
    reference: str


@dataclass(frozen=True, slots=True)
class LedgerHealth:
    admission_blocked: bool
    closed: bool
    faulted: bool


@dataclass(frozen=True, slots=True)
class ActionSnapshot:
    command_id: str
    canonical_command: str
    status: str
    owner: str
    generation: int
    reason_code: str
    details_json: str
    event_seq: int
    held_resources: tuple[str, ...]

    @property
    def details(self) -> dict[str, Any]:
        return json.loads(self.details_json)


@dataclass(frozen=True, slots=True)
class Admission:
    snapshot: ActionSnapshot
    admitted_new: bool


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    event_seq: int
    command_id: str
    status: str
    reason_code: str
    details_json: str
    acknowledged: bool
    event_id: str
    ex_session: str
    task_id: str
    goal_id: str
    goal_revision: int
    owner: str

    @property
    def complete(self) -> bool:
        """Old rows without trusted task metadata cannot form a B00 event."""
        return bool(self.task_id and self.event_id)

    @property
    def details(self) -> dict[str, Any]:
        return json.loads(self.details_json)

    def to_action_event(self):
        from .models import ActionEvent
        if not self.complete:
            raise ValueError("event lacks trusted task metadata")
        return ActionEvent(self.event_id, self.event_seq, self.ex_session,
                           self.task_id, self.goal_id, self.goal_revision,
                           self.command_id, self.owner, self.status,
                           self.reason_code, self.details)


class ActionLedger:
    """Admission is durable before callers may deliver; restoration never delivers.

    Do not call ``Future.result()`` while holding the runtime lock. A binding is
    obtained from the plugin manager's trusted caller context, not command data.
    """

    def __init__(self, path: str | Path, *, queue_size: int = 128) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._path = str(path)
        self._queue: queue.Queue[tuple[Callable[[sqlite3.Connection], Any], Future[Any]] | None] = queue.Queue(maxsize=queue_size)
        self._guard = threading.Lock()
        self._closed = False
        self._admission_blocked = False
        self._fault: BaseException | None = None
        ready: Future[None] = Future()
        self._thread = threading.Thread(target=self._run, args=(ready,), name="action-ledger", daemon=True)
        self._thread.start()
        ready.result(timeout=15)

    def _run(self, ready: Future[None]) -> None:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self._path, timeout=5, isolation_level=None)
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA foreign_keys=ON")
            self._initialize(conn)
            ready.set_result(None)
            while True:
                item = self._queue.get()
                if item is None:
                    break
                operation, future = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = operation(conn)
                except sqlite3.Error as exc:
                    with self._guard:
                        self._fault = exc
                        self._admission_blocked = True
                    if conn.in_transaction:
                        conn.rollback()
                    future.set_exception(LedgerFault(str(exc)))
                    raise
                except Exception as exc:
                    if conn.in_transaction:
                        conn.rollback()
                    future.set_exception(exc)
                else:
                    future.set_result(result)
        except BaseException as exc:
            with self._guard:
                self._fault = exc
            if not ready.done():
                ready.set_exception(LedgerFault(str(exc)))
            while True:
                try:
                    pending = self._queue.get_nowait()
                except queue.Empty:
                    break
                if pending is not None:
                    future = pending[1]
                    if future.set_running_or_notify_cancel():
                        future.set_exception(LedgerFault(str(exc)))
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _initialize(conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS commands (
                command_id TEXT PRIMARY KEY, canonical TEXT NOT NULL,
                owner TEXT NOT NULL, generation INTEGER NOT NULL,
                status TEXT NOT NULL, reason_code TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}', event_seq INTEGER NOT NULL,
                task_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS resources (
                name TEXT PRIMARY KEY, command_id TEXT NOT NULL
                REFERENCES commands(command_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                command_id TEXT NOT NULL REFERENCES commands(command_id),
                status TEXT NOT NULL, reason_code TEXT NOT NULL,
                details_json TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
                event_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS stop_evidence (
                command_id TEXT PRIMARY KEY REFERENCES commands(command_id),
                source TEXT NOT NULL, reference TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_command ON events(command_id, event_seq);
        """)
        # Legacy rows have no trusted task identity: leave them explicitly incomplete.
        for table, column, ddl in (("commands", "task_id", "TEXT NOT NULL DEFAULT ''"),
                                   ("events", "event_id", "TEXT NOT NULL DEFAULT ''")):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        # Recovery is one transaction: even an admission that was not delivered
        # becomes unknown and cannot be automatically dispatched after restart.
        conn.execute("BEGIN IMMEDIATE")
        try:
            for (command_id,) in conn.execute(
                "SELECT command_id FROM commands WHERE status IN ('admitted','accepted','running') ORDER BY command_id"
            ).fetchall():
                seq = ActionLedger._event(conn, command_id, ActionStatus.UNKNOWN, "resume_review", "{}")
                conn.execute("UPDATE commands SET status=?, reason_code=?, details_json='{}', event_seq=? WHERE command_id=?",
                             (ActionStatus.UNKNOWN, "resume_review", seq, command_id))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    @staticmethod
    def _event(conn: sqlite3.Connection, command_id: str, status: str, reason: str,
               details: str) -> int:
        row = conn.execute(
            "SELECT canonical,task_id,owner FROM commands WHERE command_id=?",
            (command_id,),
        ).fetchone()
        if row is None:
            raise KeyError(command_id)
        canonical, task_id, owner = row
        event_id = uuid.uuid4().hex
        command_data = json.loads(canonical)["command"]
        next_seq = conn.execute(
            "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='events'), 0) + 1"
        ).fetchone()[0]
        envelope = {
            "event_id": event_id,
            "event_seq": next_seq,
            "ex_session": command_data.get("ex_session", ""),
            "task_id": task_id,
            "goal_id": command_data.get("goal_id", ""),
            "goal_revision": command_data.get("goal_revision", 0),
            "command_id": command_id,
            "owner": owner,
            "status": status,
            "reason_code": reason,
            "details": json.loads(details),
        }
        budget = measure_json_budget(envelope)
        if budget is not None:
            reject(budget, "event", "durable event exceeds JSON validation budget")
        cursor = conn.execute(
            "INSERT INTO events(command_id,status,reason_code,details_json,event_id) VALUES (?,?,?,?,?)",
            (command_id, status, reason, details, event_id),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _snapshot(conn: sqlite3.Connection, command_id: str) -> ActionSnapshot | None:
        row = conn.execute("SELECT command_id,canonical,status,owner,generation,reason_code,details_json,event_seq "
                           "FROM commands WHERE command_id=?", (command_id,)).fetchone()
        if row is None:
            return None
        held = tuple(name for (name,) in conn.execute(
            "SELECT name FROM resources WHERE command_id=? ORDER BY name", (command_id,)))
        return ActionSnapshot(*row, held)

    @staticmethod
    def _binding(snapshot: ActionSnapshot, binding: OwnerBinding) -> None:
        if snapshot.owner != binding.owner or snapshot.generation != binding.generation:
            reject(ErrorCode.OWNER_MISMATCH, "binding", "owner or plugin generation mismatch")

    @property
    def health(self) -> LedgerHealth:
        """Cached gate state; does not access or wait for SQLite."""
        with self._guard:
            return LedgerHealth(self._admission_blocked or self._fault is not None or self._closed,
                                self._closed, self._fault is not None)

    @staticmethod
    def _valid_id(value: Any) -> bool:
        return type(value) is str and 0 < len(value) <= MAX_ID_LEN and bool(value.strip())

    @classmethod
    def _validate_binding(cls, binding: OwnerBinding) -> None:
        if (not isinstance(binding, OwnerBinding) or not cls._valid_id(binding.owner)
                or type(binding.generation) is not int or not 0 <= binding.generation <= MAX_SEQUENCE):
            raise ValueError("invalid owner binding")

    def _submit(self, operation: Callable[[sqlite3.Connection], T], *, admission: bool = False) -> Future[T]:
        future: Future[T] = Future()
        with self._guard:
            if self._fault is not None:
                raise LedgerFault(str(self._fault)) from self._fault
            if self._closed:
                raise LedgerClosed("ledger is closed")
            if admission and self._admission_blocked:
                raise LedgerBusy("admission blocked after writer queue saturation")
            try:
                self._queue.put_nowait((operation, future))
            except queue.Full as exc:
                self._admission_blocked = True
                raise LedgerBusy("ledger writer queue is full; admission blocked") from exc
        return future

    def admit(self, command: ActionCommand, resources: tuple[str, ...] | list[str],
              binding: OwnerBinding, *, task_id: str) -> Future[Admission]:
        """Persist trusted task identity before the caller may deliver a command.

        ``task_id`` comes from the trusted goal context, never from command params.
        Canceling the queued Future prevents admission; once running, cancel()
        returns False and the outcome remains observable.
        """
        self._validate_binding(binding)
        if not self._valid_id(task_id):
            raise ValueError("trusted task_id is required")
        if not isinstance(command, ActionCommand) or not self._valid_id(command.command_id):
            raise ValueError("invalid command_id")
        data = command.to_dict()
        budget = measure_json_budget(data)
        if budget is not None:
            reject(budget, "command", "command exceeds JSON budget or is invalid")
        # Serialize before enqueue: neither the command nor nested params survive
        # as mutable references in the writer closure.
        frozen = json.loads(json.dumps(data, allow_nan=False))
        parsed = ActionCommand.parse(frozen)
        if parsed.owner != binding.owner or parsed.plugin_generation != binding.generation:
            reject(ErrorCode.OWNER_MISMATCH, "binding", "owner or plugin generation mismatch")
        for field in ("command_id", "ex_session", "goal_id", "decision_id", "owner", "action_id"):
            if not self._valid_id(frozen[field]):
                raise ValueError(f"invalid {field}")
        command_id = parsed.command_id
        owner, generation = binding.owner, binding.generation
        names = tuple(resources)
        if (len(names) > 256 or len(names) != len(set(names))
                or any(not self._valid_id(n) for n in names)):
            raise ValueError("invalid, excessive or repeated resource")
        if parsed.operation != CommandOperation.START and names:
            raise ValueError("only start may reserve resources")
        canonical = json.dumps({"command": frozen, "resources": sorted(names)}, sort_keys=True,
                               ensure_ascii=False, separators=(",", ":"), allow_nan=False)

        def work(conn: sqlite3.Connection) -> Admission:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._snapshot(conn, command_id)
                if existing is not None:
                    stored_task = conn.execute("SELECT task_id FROM commands WHERE command_id=?", (command_id,)).fetchone()[0]
                    if existing.canonical_command != canonical or stored_task != task_id:
                        reject(ErrorCode.DUPLICATE_REQUEST_ID_CONFLICT, "command_id", "command_id reused with different payload")
                    self._binding(existing, OwnerBinding(owner, generation))
                    conn.commit()
                    return Admission(existing, False)
                for name in names:
                    holder = conn.execute("SELECT command_id FROM resources WHERE name=?", (name,)).fetchone()
                    if holder is not None:
                        reject(ErrorCode.RESOURCE_CONFLICT, "resources", f"{name!r} held by {holder[0]!r}")
                conn.execute("INSERT INTO commands(command_id,canonical,owner,generation,status,event_seq,task_id) VALUES (?,?,?,?,?,0,?)",
                             (command_id, canonical, owner, generation, ActionStatus.ADMITTED, task_id))
                for name in names:
                    conn.execute("INSERT INTO resources(name,command_id) VALUES (?,?)", (name, command_id))
                seq = self._event(conn, command_id, ActionStatus.ADMITTED, "", "{}")
                conn.execute("UPDATE commands SET event_seq=? WHERE command_id=?", (seq, command_id))
                snapshot = self._snapshot(conn, command_id)
                conn.commit()
                return Admission(snapshot, True)
            except BaseException:
                conn.rollback()
                raise
        return self._submit(work, admission=True)

    def report(self, command_id: str, binding: OwnerBinding, status: str,
               *, reason_code: str = "", details: dict[str, Any] | None = None,
               stop_evidence: StopEvidence | None = None) -> Future[ActionSnapshot]:
        self._validate_binding(binding)
        if not self._valid_id(command_id):
            raise ValueError("invalid command_id")
        if status not in LEGAL_TRANSITIONS:
            raise ValueError("unknown action status")
        payload = {} if details is None else details
        if not isinstance(payload, dict) or measure_json_budget(payload) is not None:
            raise ValueError("invalid report details")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if "stop_evidence" in payload:
            raise ValueError("stop_evidence is reserved for trusted proof")
        if not isinstance(reason_code, str) or len(reason_code) > MAX_ID_LEN:
            raise ValueError("invalid reason_code")
        proven_failure = status == ActionStatus.FAILED and stop_evidence is not None
        if status == ActionStatus.CANCELED or proven_failure:
            self._validate_stop(command_id, stop_evidence)
            payload = json.loads(encoded)
            payload["stop_evidence"] = {"stopped": True, "source": stop_evidence.source,
                                        "reference": stop_evidence.reference}
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

        def work(conn: sqlite3.Connection) -> ActionSnapshot:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._snapshot(conn, command_id)
                if current is None:
                    raise KeyError(command_id)
                self._binding(current, binding)
                if not can_transition(current.status, status):
                    reject(ErrorCode.ILLEGAL_TRANSITION, "status", f"{current.status} -> {status}")
                if current.status == status:
                    conn.commit()
                    return current
                if status == ActionStatus.CANCELED or proven_failure:
                    conn.execute("INSERT INTO stop_evidence(command_id,source,reference) VALUES (?,?,?)",
                                 (command_id, stop_evidence.source, stop_evidence.reference))
                seq = self._event(conn, command_id, status, reason_code, encoded)
                conn.execute("UPDATE commands SET status=?,reason_code=?,details_json=?,event_seq=? WHERE command_id=?",
                             (status, reason_code, encoded, seq, command_id))
                # Unproved failure/timeout/unknown still retain reservations.
                if status in (ActionStatus.REJECTED, ActionStatus.SUCCEEDED, ActionStatus.CANCELED) or proven_failure:
                    conn.execute("DELETE FROM resources WHERE command_id=?", (command_id,))
                snapshot = self._snapshot(conn, command_id)
                conn.commit()
                return snapshot
            except BaseException:
                conn.rollback()
                raise
        return self._submit(work)

    @staticmethod
    def _validate_stop(command_id: str, evidence: StopEvidence | None) -> None:
        if (not isinstance(evidence, StopEvidence) or not ActionLedger._valid_id(command_id)
                or evidence.command_id != command_id or evidence.stopped is not True
                or any(not ActionLedger._valid_id(value)
                       for value in (evidence.source, evidence.reference))):
            raise ValueError("positive structured stop evidence required")

    def reconcile_stop(self, command_id: str, binding: OwnerBinding,
                       evidence: StopEvidence) -> Future[ActionSnapshot]:
        """Release uncertain reservations, without changing immutable terminal state."""
        self._validate_binding(binding)
        self._validate_stop(command_id, evidence)

        def work(conn: sqlite3.Connection) -> ActionSnapshot:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._snapshot(conn, command_id)
                if current is None:
                    raise KeyError(command_id)
                self._binding(current, binding)
                if current.status not in (ActionStatus.UNKNOWN, ActionStatus.TIMED_OUT, ActionStatus.FAILED):
                    raise ValueError("reconciliation requires an uncertain terminal status")
                proof = conn.execute("SELECT source,reference FROM stop_evidence WHERE command_id=?", (command_id,)).fetchone()
                if proof is None:
                    conn.execute("INSERT INTO stop_evidence(command_id,source,reference) VALUES (?,?,?)",
                                 (command_id, evidence.source, evidence.reference))
                elif proof != (evidence.source, evidence.reference):
                    raise ValueError("conflicting stop evidence")
                if current.held_resources:
                    conn.execute("DELETE FROM resources WHERE command_id=?", (command_id,))
                snapshot = self._snapshot(conn, command_id)
                conn.commit()
                return snapshot
            except BaseException:
                conn.rollback()
                raise
        return self._submit(work)

    def stop_proof(self, command_id: str, binding: OwnerBinding) -> Future[StopEvidence | None]:
        """Query committed positive proof, including commands without resources."""
        self._validate_binding(binding)
        if not self._valid_id(command_id):
            raise ValueError("invalid command_id")
        def work(conn: sqlite3.Connection) -> StopEvidence | None:
            current = self._snapshot(conn, command_id)
            if current is None:
                raise KeyError(command_id)
            self._binding(current, binding)
            row = conn.execute("SELECT source,reference FROM stop_evidence WHERE command_id=?",
                               (command_id,)).fetchone()
            return StopEvidence(command_id, True, *row) if row is not None else None
        return self._submit(work)

    def get(self, command_id: str) -> Future[ActionSnapshot | None]:
        if not self._valid_id(command_id):
            raise ValueError("invalid command_id")
        return self._submit(lambda conn: self._snapshot(conn, command_id))

    def list_commands(self, *, after_id: str = "", limit: int = 100) -> Future[tuple[ActionSnapshot, ...]]:
        self._page(limit)
        def work(conn: sqlite3.Connection) -> tuple[ActionSnapshot, ...]:
            ids = conn.execute("SELECT command_id FROM commands WHERE command_id>? ORDER BY command_id LIMIT ?",
                               (after_id, limit)).fetchall()
            return tuple(self._snapshot(conn, row[0]) for row in ids)
        return self._submit(work)

    def events(self, *, after_seq: int = 0, limit: int = 100,
               unacknowledged_only: bool = False) -> Future[tuple[LedgerEvent, ...]]:
        self._page(limit)
        if type(after_seq) is not int or not 0 <= after_seq <= MAX_SEQUENCE:
            raise ValueError("invalid event cursor")
        if type(unacknowledged_only) is not bool:
            raise ValueError("unacknowledged_only must be a boolean")
        def work(conn: sqlite3.Connection) -> tuple[LedgerEvent, ...]:
            rows = conn.execute("SELECT e.event_seq,e.command_id,e.status,e.reason_code,e.details_json,e.acknowledged,"
                                "e.event_id,c.ex_session,c.task_id,c.goal_id,c.goal_revision,c.owner "
                                "FROM (SELECT event_seq,command_id,status,reason_code,details_json,acknowledged,event_id "
                                "FROM events WHERE event_seq>? AND (?=0 OR acknowledged=0) "
                                "ORDER BY event_seq LIMIT ?) AS e JOIN "
                                "(SELECT command_id,task_id,owner,"
                                "json_extract(canonical,'$.command.ex_session') AS ex_session,"
                                "json_extract(canonical,'$.command.goal_id') AS goal_id,"
                                "json_extract(canonical,'$.command.goal_revision') AS goal_revision "
                                "FROM commands) AS c ON c.command_id=e.command_id ORDER BY e.event_seq",
                                (after_seq, int(unacknowledged_only), limit)).fetchall()
            return tuple(LedgerEvent(*row) for row in rows)
        return self._submit(work)

    def ack(self, event_seq: int) -> Future[bool]:
        if type(event_seq) is not int or not 0 < event_seq <= MAX_SEQUENCE:
            raise ValueError("invalid event sequence")
        def work(conn: sqlite3.Connection) -> bool:
            conn.execute("BEGIN IMMEDIATE")
            try:
                updated = conn.execute("UPDATE events SET acknowledged=1 WHERE event_seq=?", (event_seq,)).rowcount
                conn.commit()
                return bool(updated)
            except BaseException:
                conn.rollback()
                raise
        return self._submit(work)

    @staticmethod
    def _page(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("page limit must be between 1 and 500")

    def close(self, *, timeout: float = 10) -> None:
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or timeout <= 0):
            raise ValueError("timeout must be finite and positive")
        with self._guard:
            self._admission_blocked = True
            if not self._closed:
                try:
                    self._queue.put_nowait(None)
                except queue.Full as exc:
                    raise LedgerBusy("writer queue full; close not yet accepted") from exc
                self._closed = True
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise LedgerFault("writer did not shut down within timeout")
        if self._fault is not None:
            raise LedgerFault(str(self._fault)) from self._fault

    def __enter__(self) -> ActionLedger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
