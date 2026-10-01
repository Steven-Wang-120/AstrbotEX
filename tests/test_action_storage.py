from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_ex.core.actions.ledger import ActionLedger, OwnerBinding, StopEvidence
from astrbot_ex.core.actions.models import ActionCommand, ActionStatus
from astrbot_ex.core.actions.storage import ActionStorageError, prepare_action_ledger
from astrbot_ex.core.backup import SNAPSHOT_ROOTS


BINDING = OwnerBinding("storage-owner", 1)


def command(command_id):
    return ActionCommand.parse({
        "schema_version": 1, "command_id": command_id, "ex_session": "storage-session",
        "goal_id": "storage-goal", "goal_revision": 1, "decision_id": "storage-decision",
        "owner": BINDING.owner, "plugin_generation": BINDING.generation,
        "action_id": "storage-owner.move.v2", "operation": "start", "params": {}, "lease_ms": 1000,
    })


class ActionStorageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.legacy = self.root / "profiles/default/actions.sqlite3"
        self.target = self.root / "execution/actions.sqlite3"
        self.legacy.parent.mkdir(parents=True)

    def seed_legacy(self):
        ledger = ActionLedger(self.legacy)
        try:
            for name in ("completed", "uncertain", "proof"):
                ledger.admit(command(name), (name + "-resource",), BINDING, task_id="trusted-task").result(2)
                ledger.report(name, BINDING, ActionStatus.ACCEPTED).result(2)
                ledger.report(name, BINDING, ActionStatus.RUNNING).result(2)
                ledger.report(name, BINDING, ActionStatus.SUCCEEDED if name == "completed"
                              else ActionStatus.UNKNOWN).result(2)
            ledger.reconcile_stop("proof", BINDING, StopEvidence("proof", True, "mock", "parked")).result(2)
            events = ledger.events(limit=100).result(2)
            ledger.ack(events[0].event_seq).result(2)
        finally:
            ledger.close()

    @staticmethod
    def facts(path):
        connection = sqlite3.connect(path)
        try:
            return {table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                    for table in ("commands", "resources", "events", "stop_evidence", "sqlite_sequence")}
        finally:
            connection.close()

    def test_fresh_initialized_database_is_outside_snapshot_roots(self):
        self.assertEqual(prepare_action_ledger(self.root), self.target)
        self.assertNotIn(self.target.relative_to(self.root).parts[0], SNAPSHOT_ROOTS)
        self.assertFalse(self.legacy.exists())
        self.assertEqual(self.facts(self.target)["commands"], [])
        self.assertEqual(prepare_action_ledger(self.root), self.target)

    def test_wal_committed_facts_migrate_once_and_original_is_retained(self):
        self.seed_legacy()
        connection = sqlite3.connect(self.legacy)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        original_main = self.legacy.read_bytes()
        row = list(connection.execute("SELECT * FROM commands WHERE command_id='completed'").fetchone())
        canonical = json.loads(row[1])
        canonical["command"]["command_id"] = "wal-only"
        row[0], row[1], row[7] = "wal-only", json.dumps(canonical), 100
        connection.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?,?,?)", row)
        connection.execute("INSERT INTO events(event_seq,command_id,status,reason_code,details_json,acknowledged,event_id) "
                           "VALUES (100,'wal-only','succeeded','','{}',0,'wal-event')")
        connection.commit()
        self.assertEqual(self.legacy.read_bytes(), original_main)
        self.assertGreater(Path(str(self.legacy) + "-wal").stat().st_size, 0)
        expected = self.facts(self.legacy)
        self.assertEqual(prepare_action_ledger(self.root), self.target)
        self.assertEqual(self.facts(self.target), expected)
        self.assertEqual(self.legacy.read_bytes(), original_main)
        self.assertEqual(self.facts(self.legacy), expected)
        with patch("astrbot_ex.core.actions.storage._backup_database", side_effect=AssertionError("reimport")):
            self.assertEqual(prepare_action_ledger(self.root), self.target)
        ledger = ActionLedger(self.target)
        try:
            self.assertFalse(ledger.admit(command("completed"), ("completed-resource",), BINDING,
                                          task_id="trusted-task").result(2).admitted_new)
            self.assertEqual(ledger.get("uncertain").result(2).held_resources, ("uncertain-resource",))
            self.assertEqual(ledger.stop_proof("proof", BINDING).result(2),
                             StopEvidence("proof", True, "mock", "parked"))
            self.assertTrue(ledger.events(limit=100).result(2)[0].acknowledged)
            self.assertEqual(ledger.get("wal-only").result(2).status, ActionStatus.SUCCEEDED)
        finally:
            ledger.close()

    def test_existing_execution_database_wins_over_older_profile(self):
        prepare_action_ledger(self.root)
        ledger = ActionLedger(self.target)
        ledger.admit(command("authoritative"), (), BINDING, task_id="current-task").result(2)
        ledger.close()
        before = hashlib.sha256(self.target.read_bytes()).hexdigest()
        self.seed_legacy()
        with patch("astrbot_ex.core.actions.storage._backup_database", side_effect=AssertionError("reimport")):
            prepare_action_ledger(self.root)
        self.assertEqual(hashlib.sha256(self.target.read_bytes()).hexdigest(), before)
        self.assertEqual([row[0] for row in self.facts(self.target)["commands"]], ["authoritative"])

    def test_empty_or_invalid_target_fails_closed_without_legacy_fallback(self):
        self.seed_legacy()
        self.target.parent.mkdir()
        for data in (b"", b"not sqlite"):
            with self.subTest(data=data):
                self.target.write_bytes(data)
                with self.assertRaises(ActionStorageError):
                    prepare_action_ledger(self.root)
                self.assertEqual(self.target.read_bytes(), data)
                self.assertTrue(self.legacy.exists())

    def test_concurrent_initializer_fails_closed_and_only_complete_target_is_published(self):
        self.seed_legacy()
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        from astrbot_ex.core.actions.storage import _backup_database

        def delayed(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("backup latch")
            _backup_database(*args)

        def initialize():
            try:
                results.append(prepare_action_ledger(self.root))
            except Exception as exc:
                errors.append(exc)

        with patch("astrbot_ex.core.actions.storage._backup_database", side_effect=delayed):
            thread = threading.Thread(target=initialize)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(self.target.exists())
                with self.assertRaisesRegex(ActionStorageError, "locked"):
                    prepare_action_ledger(self.root)
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [self.target])
        self.assertEqual(self.facts(self.target), self.facts(self.legacy))

    def test_failed_publication_leaves_no_empty_target_and_retry_migrates(self):
        self.seed_legacy()
        with patch("astrbot_ex.core.actions.storage.os.link", side_effect=OSError("publication failed")):
            with self.assertRaisesRegex(ActionStorageError, "publication failed"):
                prepare_action_ledger(self.root)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.iterdir()), [])
        prepare_action_ledger(self.root)
        self.assertEqual(self.facts(self.target), self.facts(self.legacy))

    def test_interrupted_initialization_lock_requires_offline_review(self):
        self.target.parent.mkdir()
        lock = self.target.parent / ".actions-init.lock"
        lock.write_text("interrupted", encoding="utf-8")
        stage = self.target.parent / ".actions-init-interrupted.sqlite3"
        stage.write_bytes(b"partial")
        with self.assertRaisesRegex(ActionStorageError, "inspect offline"):
            prepare_action_ledger(self.root)
        self.assertFalse(self.target.exists())
        self.assertEqual(lock.read_text(), "interrupted")
        self.assertEqual(stage.read_bytes(), b"partial")

    def test_restored_old_profile_cannot_replace_newer_execution_facts_on_restart(self):
        from astrbot_ex.core.backup import SnapshotService
        self.seed_legacy()
        snapshots = SnapshotService(self.root, application_version="0.1.0")
        created = snapshots.create()
        archive = snapshots.download_path(created["filename"]).read_bytes()
        prepare_action_ledger(self.root)
        ledger = ActionLedger(self.target)
        try:
            ledger.admit(command("after-snapshot"), (), BINDING, task_id="post-snapshot-task").result(2)
            ledger.report("after-snapshot", BINDING, ActionStatus.ACCEPTED).result(2)
            ledger.report("after-snapshot", BINDING, ActionStatus.SUCCEEDED).result(2)
            before = self.facts(self.target)
            snapshots.restore_upload("old-profile.zip", archive)
            self.assertFalse(ledger.health.closed)
            self.assertEqual(self.facts(self.target), before)
        finally:
            ledger.close()
        with patch("astrbot_ex.core.actions.storage._backup_database", side_effect=AssertionError("reimport")):
            prepare_action_ledger(self.root)
        reopened = ActionLedger(self.target)
        try:
            self.assertEqual(reopened.get("after-snapshot").result(2).status, ActionStatus.SUCCEEDED)
            self.assertFalse(reopened.admit(command("after-snapshot"), (), BINDING,
                                            task_id="post-snapshot-task").result(2).admitted_new)
            self.assertEqual(self.facts(self.target), before)
            self.assertEqual(reopened.get("uncertain").result(2).held_resources, ("uncertain-resource",))
            self.assertIsNotNone(reopened.stop_proof("proof", BINDING).result(2))
        finally:
            reopened.close()
        self.assertTrue(self.legacy.exists())
        self.assertNotIn("after-snapshot", [row[0] for row in self.facts(self.legacy)["commands"]])

    def test_incomplete_sqlite_target_is_not_treated_as_initialized(self):
        self.target.parent.mkdir()
        connection = sqlite3.connect(self.target)
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.close()
        before = self.target.read_bytes()
        self.seed_legacy()
        with self.assertRaisesRegex(ActionStorageError, "incomplete"):
            prepare_action_ledger(self.root)
        self.assertEqual(self.target.read_bytes(), before)

    def test_live_legacy_commands_are_copied_then_recovered_without_release(self):
        ledger = ActionLedger(self.legacy)
        try:
            for name in ("admitted", "running"):
                ledger.admit(command(name), (name + "-resource",), BINDING, task_id="legacy-task").result(2)
            ledger.report("running", BINDING, ActionStatus.ACCEPTED).result(2)
            ledger.report("running", BINDING, ActionStatus.RUNNING).result(2)
        finally:
            ledger.close()
        expected = self.facts(self.legacy)
        prepare_action_ledger(self.root)
        self.assertEqual(self.facts(self.target), expected)
        reopened = ActionLedger(self.target)
        try:
            for name in ("admitted", "running"):
                row = reopened.get(name).result(2)
                self.assertEqual((row.status, row.reason_code), (ActionStatus.UNKNOWN, "resume_review"))
                self.assertEqual(row.held_resources, (name + "-resource",))
                self.assertFalse(reopened.admit(command(name), (name + "-resource",), BINDING,
                                                task_id="legacy-task").result(2).admitted_new)
            self.assertTrue(all(event.to_action_event().task_id == "legacy-task"
                                for event in reopened.events().result(2)))
        finally:
            reopened.close()
        self.assertEqual(self.facts(self.legacy), expected)

    def test_process_exit_during_backup_never_publishes_empty_database(self):
        import subprocess
        import sys
        self.seed_legacy()
        code = (
            "import os, sys\n"
            "from astrbot_ex.core.actions import storage\n"
            "storage._backup_database = lambda *args: os._exit(17)\n"
            "storage.prepare_action_ledger(sys.argv[1])\n"
        )
        result = subprocess.run([sys.executable, "-B", "-c", code, str(self.root)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        self.assertFalse(self.target.exists())
        self.assertTrue((self.target.parent / ".actions-init.lock").is_file())
        with self.assertRaisesRegex(ActionStorageError, "locked"):
            prepare_action_ledger(self.root)
        self.assertEqual(len(self.facts(self.legacy)["commands"]), 3)

    def test_publication_refuses_database_created_by_competing_writer(self):
        self.seed_legacy()
        import os
        link = os.link
        raced_facts = []

        def publish_with_race(source, target):
            competitor = ActionLedger(target)
            try:
                competitor.admit(command("competing"), (), BINDING, task_id="competing-task").result(2)
            finally:
                competitor.close()
            raced_facts.append(self.facts(target))
            link(source, target)

        with patch("astrbot_ex.core.actions.storage.os.link", side_effect=publish_with_race):
            with self.assertRaises(ActionStorageError):
                prepare_action_ledger(self.root)
        self.assertEqual(self.facts(self.target), raced_facts[0])
        self.assertEqual([row[0] for row in self.facts(self.target)["commands"]], ["competing"])
        self.assertEqual(prepare_action_ledger(self.root), self.target)
        self.assertEqual(self.facts(self.target), raced_facts[0])

    def test_invalid_legacy_does_not_initialize_empty_execution_database(self):
        self.legacy.write_bytes(b"not sqlite")
        with self.assertRaises(ActionStorageError):
            prepare_action_ledger(self.root)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.legacy.read_bytes(), b"not sqlite")


if __name__ == "__main__":
    unittest.main()
