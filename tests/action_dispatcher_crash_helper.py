"""Real Dispatcher/Actor subprocess crash boundaries, without hardware."""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

from astrbot_ex.core.actions.dispatcher import ActionDispatcher
from astrbot_ex.core.actions.ledger import ActionLedger, OwnerBinding
from astrbot_ex.core.actions.models import ActionStatus
from astrbot_ex.core.plugin_actor import PluginActor
from tests.test_action_dispatcher import command, manifest


class CrashPlugin:
    id = "arm"

    def __init__(self, marker: Path, boundary: str):
        self.marker = marker
        self.boundary = boundary
        self.started = threading.Event()

    def on_action_command(self, action):
        with self.marker.open("ab") as stream:
            stream.write(b"start\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.started.set()
        if self.boundary == "after_business_start_before_terminal":
            os._exit(0)
        return "accepted"

    def on_action_cancel(self, command_id, reason):
        return "requested"


def main(db: Path, boundary: str) -> None:
    ledger = ActionLedger(db)
    dispatcher = ActionDispatcher(ledger)
    plugin = CrashPlugin(Path(str(db) + ".starts"), boundary)
    actor = PluginActor(plugin)
    actor.start()
    dispatcher.register_owner(OwnerBinding("arm", 3), actor,
                              manifest("arm", resource="joint", max_duration=30_000))
    dispatcher.set_gate(True)
    dispatcher.update_context(
        ex_session="session", goal_id="goal", goal_revision=1, task_id="trusted-task",
        allowed_actions=["arm.move.v2"], bound_params={"arm.move.v2": {"meters": 1}},
        runtime_state="ready", catalog_revision=1, config_revision=1,
        environment_revision=1, ttl_ms=30_000)

    if boundary == "before_admission":
        def interrupted(*args, **kwargs):
            os._exit(0)
        ledger.admit = interrupted
        dispatcher.start(command("arm", "crash-command", lease=30_000)).result(10)
        raise AssertionError("before-admission exit did not occur")
    if boundary == "inside_admission_transaction":
        def interrupted(*args, **kwargs):
            def partial(conn: sqlite3.Connection):
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO commands(command_id,canonical,owner,generation,status,event_seq,task_id) "
                             "VALUES (?,?,?,?,?,0,?)",
                             ("crash-command", "partial", "arm", 3, "admitted", "trusted-task"))
                conn.execute("INSERT INTO resources(name,command_id) VALUES (?,?)", ("joint", "crash-command"))
                os._exit(0)
            return ledger._submit(partial)
        ledger.admit = interrupted
        dispatcher.start(command("arm", "crash-command", lease=30_000)).result(10)
        raise AssertionError("inside-transaction exit did not occur")
    if boundary == "after_commit_before_delivery":
        def interrupt_delivery(action):
            assert ledger.get("crash-command").result(5).status == ActionStatus.ADMITTED
            os._exit(0)
        actor.submit_action = interrupt_delivery
        dispatcher.start(command("arm", "crash-command", lease=30_000)).result(10)
        raise AssertionError("after-commit exit did not occur")
    if boundary == "after_business_start_before_terminal":
        dispatcher.start(command("arm", "crash-command", lease=30_000)).result(10)
        if not plugin.started.wait(10):
            raise AssertionError("business start did not run")
        time.sleep(1)
        raise AssertionError("post-start exit did not occur")
    raise ValueError(boundary)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
