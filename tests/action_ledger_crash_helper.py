"""Subprocess crash harness: process exit is intentional, not a unittest skip."""

from __future__ import annotations

import os
import sqlite3
import sys

from astrbot_ex.core.actions.ledger import ActionLedger, OwnerBinding
from astrbot_ex.core.actions.models import ActionCommand, ActionStatus


def command() -> ActionCommand:
    return ActionCommand(
        command_id="crash-command", ex_session="session", goal_id="goal",
        goal_revision=1, decision_id="decision", owner="arm",
        plugin_generation=3, action_id="arm.move.v2", operation="start",
        params={"meters": 1}, lease_ms=1000,
    )


def marker_open_flags() -> int:
    return os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0)


if __name__ == "__main__":
    db, boundary = sys.argv[1:]
    ledger = ActionLedger(db)
    if boundary == "before_admission":
        os._exit(0)
    if boundary == "during_admission_transaction":
        def partial(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO commands(command_id,canonical,owner,generation,status,event_seq,task_id) "
                         "VALUES (?,?,?,?,?,0,?)",
                         ("crash-command", "partial", "arm", 3, "admitted", "trusted-task"))
            conn.execute("INSERT INTO resources(name,command_id) VALUES (?,?)", ("arm", "crash-command"))
            conn.execute("INSERT INTO events(command_id,status,reason_code,details_json,event_id) "
                         "VALUES (?,?,?,?,?)", ("crash-command", "admitted", "", "{}", "partial-event"))
            os._exit(0)
        ledger._submit(partial).result(timeout=5)
    ledger.admit(command(), ["arm"], OwnerBinding("arm", 3), task_id="trusted-task").result(timeout=5)
    if boundary == "after_commit_before_delivery":
        os._exit(0)
    if boundary == "running_before_terminal_commit":
        ledger.report("crash-command", OwnerBinding("arm", 3), ActionStatus.ACCEPTED).result(timeout=5)
        ledger.report("crash-command", OwnerBinding("arm", 3), ActionStatus.RUNNING).result(timeout=5)
        marker = os.open(db + ".starts", marker_open_flags(), 0o600)
        try:
            os.write(marker, b"start\n")
            os.fsync(marker)
        finally:
            os.close(marker)
        os._exit(0)
    raise ValueError(boundary)
