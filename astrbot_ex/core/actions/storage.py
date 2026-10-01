"""Instance execution state is authoritative and never part of configuration snapshots."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

from .ledger import ActionLedger


class ActionStorageError(RuntimeError):
    pass


_REQUIRED_COLUMNS = {
    "commands": {"command_id", "canonical", "owner", "generation", "status", "reason_code",
                 "details_json", "event_seq"},
    "resources": {"name", "command_id"},
    "events": {"event_seq", "command_id", "status", "reason_code", "details_json", "acknowledged"},
    "stop_evidence": {"command_id", "source", "reference"},
}


def _validate_database(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise ActionStorageError(f"action database is not a complete regular file: {path}")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ActionStorageError(f"action database integrity check failed: {path}")
        for table, required in _REQUIRED_COLUMNS.items():
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not required <= columns:
                raise ActionStorageError(f"action database is incomplete: {path}: {table}")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ActionStorageError(f"action database references are invalid: {path}")


def _backup_database(source: Path, destination: Path) -> None:
    _validate_database(source)
    deadline = time.monotonic() + 5

    def progress(status: int, remaining: int, total: int) -> None:
        if time.monotonic() >= deadline:
            raise ActionStorageError("legacy action database backup did not finish within 5 seconds")

    # SQLite's backup API includes committed WAL pages; copying the main file does not.
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)) as old:
        with closing(sqlite3.connect(destination, timeout=5)) as new:
            old.backup(new, pages=128, progress=progress, sleep=0.01)
            new.execute("PRAGMA journal_mode=DELETE")


def prepare_action_ledger(data_root: str | Path) -> Path:
    """Publish a complete execution/actions.sqlite3 once, without replacing any database.

    A legacy profiles/default/actions.sqlite3 is read only on first initialization.
    An existing execution database wins even when an older profile is restored.
    Exclusive initialization locks fail closed on concurrent or interrupted setup;
    a stale lock requires offline inspection, never automatic deletion/reimport.
    This is an initialization guard, not permission to run multiple live runtimes.
    """
    root = Path(data_root).resolve()
    directory = root / "execution"
    directory.mkdir(parents=True, exist_ok=True)
    if directory.resolve() != directory or directory.is_symlink():
        raise ActionStorageError("execution directory must not redirect into snapshot roots")
    target = directory / "actions.sqlite3"
    legacy = root / "profiles" / "default" / "actions.sqlite3"
    lock = directory / ".actions-init.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ActionStorageError(f"action storage initialization locked; inspect offline: {lock}") from exc
    stage: Path | None = None
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(f"pid={os.getpid()}\n".encode("ascii"))
            output.flush()
            os.fsync(output.fileno())
        if target.exists() or target.is_symlink():
            _validate_database(target)
            return target
        descriptor, filename = tempfile.mkstemp(prefix=".actions-init-", suffix=".sqlite3", dir=directory)
        os.close(descriptor)
        stage = Path(filename)
        if legacy.exists() or legacy.is_symlink():
            _backup_database(legacy, stage)
        else:
            ledger = ActionLedger(stage)
            ledger.close()
        _validate_database(stage)
        with stage.open("r+b") as database:
            os.fsync(database.fileno())
        # A hard-link publishes atomically and refuses an existing target on every OS.
        # No replace(), no main-file copy, and no visible empty target during setup.
        os.link(stage, target)
        if os.name != "nt":
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return target
    except (OSError, sqlite3.Error) as exc:
        raise ActionStorageError(f"action storage initialization failed: {exc}") from exc
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)
        lock.unlink()
