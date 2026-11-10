"""Single SQLite writer with explicit transactions; no network dependencies."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading


class ChatRepository:
    def __init__(self, database: Path):
        database.parent.mkdir(parents=True, exist_ok=True)
        self.database = database
        self._lock = threading.RLock()
        self._closed = False
        self.connection = sqlite3.connect(database, check_same_thread=False, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=DELETE")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            self.connection.close()
            raise RuntimeError("Unsupported chat database version")
        if version == 0:
            self.connection.executescript((Path(__file__).parent / "migrations/001_initial.sql").read_text(encoding="utf-8"))

    @contextmanager
    def transaction(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Chat repository is closed")
            with self.connection:
                yield self.connection

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self.connection.close()
                self._closed = True
