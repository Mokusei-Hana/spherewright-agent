"""Durable intent-before-commit ledger; uncertain work is never replayed."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path


def redact(value):
    if isinstance(value, dict):
        return {k: ("[redacted]" if k not in ("max_tokens", "api_key_env", "provider_key_present")
                    and any(s in k.lower() for s in ("token", "secret", "authorization", "api_key"))
                    else redact(v)) for k, v in value.items()}
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
        self.db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.commit()

    def create_task(self, goal, config):
        task = {"id": str(uuid.uuid4()), "goal": goal, "state": "pending", "config": redact(config),
                "steps": 0, "writes": 0, "materials": 0, "read_retries": 0}
        self.save_task(task)
        return task

    def save_task(self, task):
        transitions = {
            "pending": {"pending", "running", "paused", "blocked", "failed"},
            "running": {"running", "waiting_confirmation", "paused", "blocked", "completed", "failed"},
            "waiting_confirmation": {"waiting_confirmation", "running", "paused", "blocked", "failed"},
            "paused": {"paused", "running", "blocked", "failed"},
            "blocked": {"blocked", "paused", "running", "failed"},
            "completed": {"completed"},
            "failed": {"failed", "paused", "blocked"},
        }
        row = self.db.execute("SELECT data FROM tasks WHERE id=?", (task["id"],)).fetchone()
        if task["state"] not in transitions or row and task["state"] not in transitions[json.loads(row[0])["state"]]:
            raise ValueError("Invalid task state transition")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?)", (task["id"], json.dumps(redact(task))))

    def task(self, task_id):
        row = self.db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("Unknown task ID")
        return json.loads(row[0])

    def tasks(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM tasks ORDER BY rowid DESC")]

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
            pending = self.pending()
            if not pending or evidence.get("terminal", {}).get("actionId") != pending.get("actionId"):
                raise ValueError("Only original actionId evidence can release pending intent")
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
