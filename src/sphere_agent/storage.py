"""Durable intent-before-commit ledger; uncertain work is never replayed."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def redact(value):
    if isinstance(value, dict):
        return {k: ("[redacted]" if "token" in k.lower() else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, kind TEXT, data TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT)")
        self.db.commit()

    def event(self, kind, data):
        with self.db:
            self.db.execute("INSERT INTO events(kind,data) VALUES (?,?)", (kind, json.dumps(redact(data))))

    def pending(self):
        row = self.db.execute("SELECT data FROM pending WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def set_pending(self, data):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO pending VALUES (1,?)", (json.dumps(redact(data)),))

    def resolve(self, evidence):
        # Evidence and release of the write gate are atomic.
        with self.db:
            self.db.execute(
                "INSERT INTO events(kind,data) VALUES ('verified',?)", (json.dumps(redact(evidence)),)
            )
            self.db.execute("DELETE FROM pending")


@contextmanager
def writer_lock(path):
    """OS lock survives process crashes safely; lock the same DB across all runs."""
    handle = Path(str(path) + ".writer.lock").open("a+b")
    handle.seek(0)
    handle.write(b"0")
    handle.flush()
    handle.seek(0)
    try:
        if __import__("os").name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()
