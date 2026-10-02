"""Durable EX facts, separate from instance profiles/plugins snapshots.

Only committed Ledger events and the service's post-stop goal summaries enter
this journal. ACK marks one durable receipt, never a cumulative cursor.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path

from ..contracts import ActionEvent, EventsReply, Feedback, measure_json_budget


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class FeedbackJournal:
    def __init__(self, path: Path, ex_session: str, *, retention: int = 1024,
                 page_size: int = 64, goal_limit: int = 512) -> None:
        if not 1 <= page_size <= retention <= 10000 or not 1 <= goal_limit <= 4096:
            raise ValueError("invalid journal bounds")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.session = ex_session
        self.retention, self.page_size, self.goal_limit = retention, page_size, goal_limit
        self._lock = threading.RLock()
        self._collector_lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(str(path), timeout=1, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session TEXT PRIMARY KEY, head INTEGER NOT NULL DEFAULT 0,
                trimmed INTEGER NOT NULL DEFAULT 0, ledger_cursor INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS goals (
                session TEXT NOT NULL, goal_id TEXT NOT NULL, revision INTEGER NOT NULL,
                payload TEXT NOT NULL, summary TEXT,
                PRIMARY KEY(session, goal_id, revision));
            CREATE TABLE IF NOT EXISTS source_tombstones (
                session TEXT NOT NULL, source_key TEXT NOT NULL, seq INTEGER NOT NULL,
                PRIMARY KEY(session, source_key));
            CREATE TABLE IF NOT EXISTS drafts (
                session TEXT NOT NULL, goal_id TEXT NOT NULL, revision INTEGER NOT NULL,
                epoch INTEGER NOT NULL, summary TEXT NOT NULL,
                PRIMARY KEY(session, goal_id, revision, epoch));
            CREATE TABLE IF NOT EXISTS admissions (
                session TEXT NOT NULL, request_id TEXT NOT NULL, payload TEXT NOT NULL,
                revision INTEGER NOT NULL, result TEXT,
                PRIMARY KEY(session, request_id));
            CREATE TABLE IF NOT EXISTS facts (
                session TEXT NOT NULL, seq INTEGER NOT NULL, source_key TEXT NOT NULL,
                event TEXT NOT NULL, feedback TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(session, seq), UNIQUE(session, source_key));
        """)
        with self._db:
            self._db.execute("INSERT OR IGNORE INTO sessions(session) VALUES (?)", (ex_session,))

    def register_goal(self, payload: dict, revision: int) -> None:
        with self._lock, self._db:
            self._insert_goal(payload, revision)

    def _insert_goal(self, payload: dict, revision: int) -> None:
        # Caller owns the lock and transaction; never commit an admission early.
        row = self._db.execute("SELECT payload FROM goals WHERE session=? AND goal_id=? AND revision=?",
                               (self.session, payload["goal_id"], revision)).fetchone()
        encoded = canonical(payload)
        if row:
            if row[0] != encoded:
                raise RuntimeError("journal_goal_conflict")
            return
        count = self._db.execute("SELECT count(*) FROM goals WHERE session=?", (self.session,)).fetchone()[0]
        if count >= self.goal_limit:
            raise RuntimeError("journal_goal_capacity")
        self._db.execute("INSERT INTO goals(session,goal_id,revision,payload) VALUES (?,?,?,?)",
                         (self.session, payload["goal_id"], revision, encoded))

    def prepare_admission(self, payload: dict, revision: int) -> None:
        """Persist intent/capacity before CAS; intent alone is not acceptance."""
        encoded = canonical(payload)
        with self._lock, self._db:
            old = self._db.execute("SELECT payload,revision FROM admissions WHERE session=? AND request_id=?",
                                   (self.session, payload["request_id"])).fetchone()
            if old:
                if old != (encoded, revision):
                    raise RuntimeError("journal_admission_conflict")
                return
            self._insert_goal(payload, revision)
            self._db.execute("INSERT INTO admissions(session,request_id,payload,revision) VALUES (?,?,?,?)",
                             (self.session, payload["request_id"], encoded, revision))

    def confirm_admission(self, payload: dict, result: dict) -> None:
        with self._lock, self._db:
            updated = self._db.execute("UPDATE admissions SET result=? WHERE session=? AND request_id=? AND payload=?",
                (canonical(result), self.session, payload["request_id"], canonical(payload))).rowcount
            if updated != 1:
                raise RuntimeError("journal_admission_missing")

    def registered(self, goal_id, revision) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM goals WHERE session=? AND goal_id=? AND revision=?",
                                    (self.session, goal_id, revision)).fetchone() is not None

    def _append(self, source_key, fact, *, command_id, owner):
        old = self._db.execute("SELECT feedback FROM facts WHERE session=? AND source_key=?",
                               (self.session, source_key)).fetchone()
        if old:
            return json.loads(old[0])
        if self._db.execute("SELECT 1 FROM source_tombstones WHERE session=? AND source_key=?",
                            (self.session, source_key)).fetchone():
            return None  # Trimmed sources remain consumed forever in this session.
        head = self._db.execute("SELECT head FROM sessions WHERE session=?", (self.session,)).fetchone()[0] + 1
        feedback = Feedback.parse({"schema_version": 1, **fact, "event_seq": head}).to_dict()
        event = ActionEvent.parse({"event_id": uuid.uuid5(uuid.NAMESPACE_URL, self.session + ":" + source_key).hex,
            "command_id": command_id, "owner": owner,
            **{k: v for k, v in feedback.items() if k != "schema_version"}}).to_dict()
        # Bound individual facts so each recovery page can fit the frozen JSON budget.
        if len(canonical(event).encode("utf-8")) > 8192:
            raise RuntimeError("journal_fact_capacity")
        self._db.execute("INSERT INTO facts(session,seq,source_key,event,feedback) VALUES (?,?,?,?,?)",
                         (self.session, head, source_key, canonical(event), canonical(feedback)))
        self._db.execute("INSERT INTO source_tombstones(session,source_key,seq) VALUES (?,?,?)",
                         (self.session, source_key, head))
        trim = max(0, head - self.retention)
        self._db.execute("UPDATE sessions SET head=?,trimmed=? WHERE session=?", (head, trim, self.session))
        self._db.execute("DELETE FROM facts WHERE session=? AND seq<=?", (self.session, trim))
        return feedback

    def collect(self, ledger, timeout=1) -> None:
        # Serialize the read/wait/write cycle; no runtime/safety locks held.
        with self._collector_lock:
            self._collect(ledger, timeout)

    def _collect(self, ledger, timeout) -> None:
        with self._lock:
            cursor = self._db.execute("SELECT ledger_cursor FROM sessions WHERE session=?", (self.session,)).fetchone()[0]
        page = ledger.events(after_seq=cursor, limit=128).result(timeout)
        with self._lock, self._db:
            for event in page:
                if event.complete and event.ex_session == self.session:
                    details = {"command_id": event.command_id, "owner": event.owner,
                               "ledger_event_id": event.event_id, "ledger_event_seq": event.event_seq,
                               "action_status": event.status}
                    # Preserve actual stop references, not plugin/provider free-form text.
                    if event.status == "canceled":
                        details["stop_evidence"] = event.details["stop_evidence"]
                    self._append("ledger:" + event.event_id, {
                        "ex_session": self.session, "task_id": event.task_id, "goal_id": event.goal_id,
                        "goal_revision": event.goal_revision,
                        "status": event.status if event.status in {"admitted", "accepted", "running"} else "running",
                        "reason_code": "ledger_" + event.status, "details": details},
                        command_id=event.command_id, owner=event.owner)
                cursor = event.event_seq
            self._db.execute("UPDATE sessions SET ledger_cursor=MAX(ledger_cursor,?) WHERE session=?", (cursor, self.session))

    def prepare_completion(self, goal, epoch, summary, ledger, timeout=1) -> tuple:
        # Flush predecessor facts before the goal summary; polling is idempotent.
        for _ in range(80):
            with self._lock:
                before = self._db.execute("SELECT ledger_cursor FROM sessions WHERE session=?", (self.session,)).fetchone()[0]
            self.collect(ledger, timeout)
            with self._lock:
                after = self._db.execute("SELECT ledger_cursor FROM sessions WHERE session=?", (self.session,)).fetchone()[0]
            if after == before:
                break
        else:
            raise RuntimeError("journal_ledger_backlog")
        payload = goal.payload()
        if not self.registered(payload["goal_id"], goal.revision):
            raise RuntimeError("journal_goal_unregistered")
        encoded = canonical(summary)
        # Validate draft bounds before safety CAS, not after clearing authority.
        if len(encoded.encode("utf-8")) > 7500 or measure_json_budget(summary) is not None:
            raise RuntimeError("journal_summary_capacity")
        token = (self.session, payload["goal_id"], goal.revision, epoch)
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO drafts(session,goal_id,revision,epoch,summary) VALUES (?,?,?,?,?)",
                             (*token, encoded))
        return token

    def publish_completion(self, token) -> dict:
        # Only called with a successful framework CAS permit. On restart drafts
        # never become facts: a lost in-memory permit requires manual review.
        session, goal_id, revision, epoch = token
        with self._lock, self._db:
            row = self._db.execute("SELECT summary FROM drafts WHERE session=? AND goal_id=? AND revision=? AND epoch=?",
                                   token).fetchone()
            if row is None or session != self.session:
                raise RuntimeError("journal_draft_missing")
            summary = json.loads(row[0])
            payload = json.loads(self._db.execute("SELECT payload FROM goals WHERE session=? AND goal_id=? AND revision=?",
                                                 (session, goal_id, revision)).fetchone()[0])
            old = self._db.execute("SELECT summary FROM goals WHERE session=? AND goal_id=? AND revision=?",
                                  (session, goal_id, revision)).fetchone()[0]
            if old:
                return json.loads(old)
            key = "summary:" + goal_id + ":" + str(revision)
            fact = self._append(key, {
                "ex_session": session, "task_id": payload["task_id"], "goal_id": goal_id,
                "goal_revision": revision, "status": summary["status"],
                "reason_code": summary["reason_code"], "details": summary["details"]},
                command_id="goal-summary:" + uuid.uuid5(uuid.NAMESPACE_URL, key).hex, owner="ex-framework")
            self._db.execute("UPDATE goals SET summary=? WHERE session=? AND goal_id=? AND revision=?",
                             (canonical(fact), session, goal_id, revision))
            return fact

    def ack(self, feedback: dict) -> bool:
        with self._lock, self._db:
            row = self._db.execute("SELECT feedback FROM facts WHERE session=? AND seq=?",
                                   (self.session, feedback["event_seq"])).fetchone()
            if row is None or json.loads(row[0]) != feedback:
                return False
            self._db.execute("UPDATE facts SET acknowledged=1 WHERE session=? AND seq=?",
                             (self.session, feedback["event_seq"]))
            return True

    def pending(self) -> list[dict]:
        with self._lock:
            return [json.loads(row[0]) for row in self._db.execute(
                "SELECT feedback FROM facts WHERE session=? AND acknowledged=0 ORDER BY seq LIMIT ?",
                (self.session, self.page_size))]

    def events(self, since: int) -> dict:
        with self._lock:
            head, trimmed = self._db.execute("SELECT head,trimmed FROM sessions WHERE session=?", (self.session,)).fetchone()
            resync = since < trimmed or since > head
            rows = [] if resync else self._db.execute(
                "SELECT event FROM facts WHERE session=? AND seq>? ORDER BY seq LIMIT ?",
                (self.session, since, self.page_size)).fetchall()
            selected = [json.loads(row[0]) for row in rows]
            # Page horizon, NOT unreturned head: AEB must not diagnose a false event gap.
            horizon = head if resync or not selected else selected[-1]["event_seq"]
            return EventsReply.parse({"schema_version": 1, "ex_session": self.session,
                "events": selected, "oldest_available_seq": trimmed + 1,
                "latest_event_seq": horizon, "resync_required": resync}).to_dict()

    def snapshot(self) -> dict:
        with self._lock:
            head = self._db.execute("SELECT head FROM sessions WHERE session=?", (self.session,)).fetchone()[0]
            summaries = [json.loads(row[0]) for row in self._db.execute(
                "SELECT summary FROM goals WHERE session=? AND summary IS NOT NULL ORDER BY revision DESC LIMIT 16",
                (self.session,))]
            previous = self._db.execute("SELECT 1 FROM goals WHERE session<>? LIMIT 1", (self.session,)).fetchone() is not None
            result = {"event_seq": head, "feedback": summaries[0] if summaries else None,
                      "goal_summaries": summaries, "previous_session_manual_review": previous}
            if measure_json_budget(result) is not None:
                raise RuntimeError("journal_state_capacity")
            return result

    def completion(self, task_id, evidence_seq):
        with self._lock:
            for row in self._db.execute("SELECT summary FROM goals WHERE session=? AND summary IS NOT NULL", (self.session,)):
                fact = json.loads(row[0])
                if (fact["task_id"] == task_id and fact["event_seq"] == evidence_seq
                        and fact["status"] == "succeeded"
                        and fact["details"].get("completion_evidence", {}).get("verified") is True):
                    return fact
        return None

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._db.close()
