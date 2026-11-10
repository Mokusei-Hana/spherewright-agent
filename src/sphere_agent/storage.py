"""Durable intent-before-commit ledger; uncertain work is never replayed."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path


def redact(value):
    if isinstance(value, dict):
        return {k: ("[redacted]" if k not in ("max_tokens", "api_key_env", "provider_key_present", "remote_authorization_file")
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
        self.db.execute("CREATE TABLE IF NOT EXISTS worlds (session_id TEXT, star_id INTEGER, planet_id INTEGER, data TEXT NOT NULL, PRIMARY KEY(session_id,star_id,planet_id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS action_bindings (session_id TEXT, action_id TEXT, data TEXT NOT NULL, PRIMARY KEY(session_id,action_id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS container_quotas (session_id TEXT, planet_id INTEGER, object_id INTEGER, data TEXT NOT NULL, PRIMARY KEY(session_id,planet_id,object_id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS journeys (session_id TEXT, journey_id TEXT, data TEXT NOT NULL, PRIMARY KEY(session_id,journey_id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS flight_authorities (authority_id TEXT PRIMARY KEY, intent_key TEXT NOT NULL)")
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

    def world(self, identity, star_id, snapshot):
        if type(star_id) is not int or star_id <= 0:
            raise ValueError("Positive native star ID required")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO worlds VALUES (?,?,?,?)",
                            (identity["sessionId"], star_id, identity["planetId"], json.dumps(redact(snapshot))))
        return {**identity, "starId": star_id}

    def container_quota(self, identity, object_id):
        row = self.db.execute("SELECT data FROM container_quotas WHERE session_id=? AND planet_id=? AND object_id=?",
                              (identity["sessionId"], identity["planetId"], object_id)).fetchone()
        return json.loads(row[0]) if row else None

    def set_container_quota(self, identity, object_id, max_items, snapshot):
        if self.pending():
            raise ValueError("Unresolved action freezes local policy changes")
        data = {**identity, "object_id": object_id, "max_items": max_items, "endpoint": snapshot,
                "scope": "client_admission_only_not_native_delivery_control"}
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO container_quotas VALUES (?,?,?,?)",
                            (identity["sessionId"], identity["planetId"], object_id, json.dumps(redact(data))))
        return data

    def journey(self, session_id, journey_id):
        row = self.db.execute("SELECT data FROM journeys WHERE session_id=? AND journey_id=?", (session_id, journey_id)).fetchone()
        if not row:
            raise ValueError("Unknown original journey; never infer a return binding")
        return json.loads(row[0])

    def bind_action(self, pending):
        binding = {key: pending.get(key) for key in ("sessionId", "planetId", "starId", "actionId", "key", "actionKind")}
        row = self.db.execute("SELECT data FROM action_bindings WHERE session_id=? AND action_id=?",
                              (binding["sessionId"], binding["actionId"])).fetchone()
        if row and json.loads(row[0]) != binding:
            raise ValueError("Action ID already bound to another original scope")
        self.db.execute("INSERT OR IGNORE INTO action_bindings VALUES (?,?,?)",
                        (binding["sessionId"], binding["actionId"], json.dumps(binding)))

    def pending(self):
        row = self.db.execute("SELECT data FROM pending WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def set_pending(self, data):
        with self.db:
            original = self.pending()
            if original and any(original.get(key) != data.get(key) for key in ("key", "sessionId", "planetId", "starId")):
                raise ValueError("Cannot replace an unresolved intent with another world/key")
            if original and original.get("actionId") and original["actionId"] != data.get("actionId"):
                raise ValueError("Cannot replace original action ID")
            if not original and data.get("authority_id") and not data.get("return_leg"):
                self.db.execute("INSERT INTO flight_authorities VALUES (?,?)", (data["authority_id"], data["key"]))
            if data.get("actionId"):
                self.bind_action(data)
            self.db.execute("INSERT OR REPLACE INTO pending VALUES (1,?)", (json.dumps(redact(data)),))

    def resolve(self, evidence):
        # Evidence and release of the write gate are atomic.
        with self.db:
            pending = self.pending()
            if not pending or evidence.get("terminal", {}).get("actionId") != pending.get("actionId"):
                raise ValueError("Only original actionId evidence can release pending intent")
            terminal = evidence["terminal"]
            if any(terminal.get(key) != pending.get(key) for key in ("sessionId", "planetId")) or terminal.get("idempotencyKey") != pending["key"]:
                raise ValueError("Original world/idempotency binding required for resolution")
            if "journey" in evidence:
                journey = evidence["journey"]
                self.db.execute("INSERT OR REPLACE INTO journeys VALUES (?,?,?)",
                                (pending["sessionId"], journey["journey_id"], json.dumps(redact(journey))))
            if pending.get("task_id") and evidence.get("scope"):
                task = self.task(pending["task_id"])
                task["scope"] = evidence["scope"]
                task["writes"] = max(task["writes"], pending.get("reserved_writes", 0))
                task["materials"] = max(task["materials"], pending.get("reserved_materials", 0))
                self.db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(redact(task)), task["id"]))
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
