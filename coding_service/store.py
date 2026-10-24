"""Durable task and event state (SQLite, WAL, synchronous=FULL). The service's single source of truth.

Events are append-only with a gap-free per-task sequence assigned inside the transaction that writes them, so a cursor means
the same thing before and after any restart. The task row's state is updated in the same transaction as the event that causes
it, so state and log can never disagree after a crash.
"""
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from protocol import PROTOCOL_VERSION, TERMINAL_STATES

from .errors import KeyConflict, LogUnavailable

STATE_AFTER = {"approval_required": "AWAITING_APPROVAL", "approval_decision": "RUNNING"}
SCHEMA_VERSION = 1
SCHEMA_COLUMNS = {
    "tasks": {"id", "principal", "workspace", "idem_key", "instruction", "digest", "state", "created"},
    "events": {"task_id", "seq", "type", "data"},
    "model_usage": {"task_id", "call_id", "event_seq", "data", "created"},
}


def _check_schema(connection, *, legacy=False):
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if tables - SCHEMA_COLUMNS.keys() or (not legacy and tables != SCHEMA_COLUMNS.keys()):
        raise LogUnavailable("unsupported task store schema")
    for table in tables:
        if {r[1] for r in connection.execute(f"PRAGMA table_info({table})")} != SCHEMA_COLUMNS[table]:
            raise LogUnavailable("unsupported task store columns")

class TaskStore:
    def __init__(self, path):
        self._lock = threading.RLock()
        self.path = Path(path).resolve()
        try:
            self._db = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise LogUnavailable("unsupported task store schema version")
            _check_schema(self._db, legacy=version == 0)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            if version == 0:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    version = self._db.execute("PRAGMA user_version").fetchone()[0]
                    if version not in (0, SCHEMA_VERSION):
                        raise LogUnavailable("unsupported task store schema version")
                    _check_schema(self._db, legacy=version == 0)
                    for statement in (
                        """CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, principal TEXT NOT NULL, workspace TEXT NOT NULL, idem_key TEXT NOT NULL,
                    instruction TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, UNIQUE (principal, idem_key));
                        """,
                        """CREATE TABLE IF NOT EXISTS events (task_id TEXT NOT NULL REFERENCES tasks(id), seq INTEGER NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL,
                            PRIMARY KEY (task_id, seq));""",
                        """CREATE TABLE IF NOT EXISTS model_usage (task_id TEXT NOT NULL REFERENCES tasks(id), call_id TEXT NOT NULL,
                            event_seq INTEGER NOT NULL, data TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY (task_id, call_id));""",
                    ):
                        self._db.execute(statement)
                    self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    self._db.execute("COMMIT")
                except BaseException:
                    if self._db.in_transaction:
                        self._db.execute("ROLLBACK")
                    raise
            else:
                _check_schema(self._db)
        except BaseException as exc:
            if hasattr(self, "_db"):
                self._db.close()
            if isinstance(exc, sqlite3.Error):
                raise LogUnavailable(type(exc).__name__) from None
            raise

    def close(self):
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self):
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    yield self._db
                    self._db.execute("COMMIT")
                except BaseException:
                    # COMMIT may fail while leaving the transaction open (BUSY,
                    # disk/IO errors). Never expose its uncommitted rows on the
                    # shared connection or poison every subsequent BEGIN.
                    if self._db.in_transaction:
                        try:
                            self._db.execute("ROLLBACK")
                        except sqlite3.Error:
                            self._db.close()  # fail closed if even rollback fails
                    raise
            except sqlite3.Error as exc:
                raise LogUnavailable(type(exc).__name__) from None

    @staticmethod
    def _put(db, task_id, kind, data):
        usage = kind == "notice" and data.get("code") in {"model_usage", "model_call_started"}
        update_usage = False
        if usage:
            payload = json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False)
            call_id = data["model_call_id"]
            previous = db.execute("SELECT event_seq,data FROM model_usage WHERE task_id=? AND call_id=?", (task_id, call_id)).fetchone()
            if previous:
                previous_data = json.loads(previous[1])
                if previous[1] == payload or (previous_data.get("code") == "model_usage" and data["code"] == "model_call_started"):
                    return previous[0]
                if previous_data.get("code") == "model_call_started" and data["code"] == "model_usage":
                    update_usage = True
                else:
                    raise ValueError("usage identity reused with different content")
        seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE task_id = ?", (task_id,)).fetchone()[0]
        if usage:
            if update_usage:
                db.execute("UPDATE model_usage SET event_seq=?,data=? WHERE task_id=? AND call_id=?", (seq, payload, task_id, call_id))
            else:
                db.execute("INSERT INTO model_usage VALUES(?,?,?,?,?)", (task_id, call_id, seq, payload, time.time()))
        db.execute("INSERT INTO events (task_id, seq, type, data) VALUES (?, ?, ?, ?)", (task_id, seq, kind, json.dumps(data, ensure_ascii=False)))
        state = data.get("state") if kind == "state" else STATE_AFTER.get(kind)
        if state:
            db.execute("UPDATE tasks SET state = ? WHERE id = ?", (state, task_id))
        return seq

    # --- writes ---
    def create_or_get(self, principal, workspace, key, content):
        """Atomic. Returns (task_id, state, created). The same key with the same content is the same task, forever (restarts included)."""
        digest = hashlib.sha256(json.dumps([workspace, content], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        with self._tx() as db:
            row = db.execute("SELECT id, state, digest FROM tasks WHERE principal = ? AND idem_key = ?", (principal, key)).fetchone()
            if row:
                if row[2] != digest:
                    raise KeyConflict()
                return row[0], row[1], False
            task_id = uuid.uuid4().hex
            db.execute("INSERT INTO tasks (id, principal, workspace, idem_key, instruction, digest, state, created) VALUES (?, ?, ?, ?, ?, ?, 'ACCEPTED', ?)",
                       (task_id, principal, workspace, key, content, digest, time.time()))
            self._put(db, task_id, "notice", {"code": "task_accepted"})   # an accepted task always has at least one event
            return task_id, "ACCEPTED", True

    def append(self, task_id, kind, data):
        with self._tx() as db:
            return self._put(db, task_id, kind, data)

    # --- reads ---
    def get(self, task_id):
        with self._lock:
            try:
                row = self._db.execute("SELECT id, principal, workspace, state, instruction FROM tasks WHERE id = ?", (task_id,)).fetchone()
            except sqlite3.Error as exc:
                raise LogUnavailable(type(exc).__name__) from None
        return {"id": row[0], "principal": row[1], "workspace": row[2], "state": row[3], "instruction": row[4]} if row else None

    def lookup(self, principal, key):
        with self._lock:
            try:
                return self._db.execute("SELECT id FROM tasks WHERE principal = ? AND idem_key = ?", (principal, key)).fetchone()
            except sqlite3.Error as exc:
                raise LogUnavailable(type(exc).__name__) from None

    def snapshot(self, task_id, after=0):
        with self._lock:
            try:
                rows = self._db.execute("SELECT seq, type, data FROM events WHERE task_id = ? AND seq > ? ORDER BY seq LIMIT 2000", (task_id, after)).fetchall()
                state = self._db.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
                last = self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE task_id = ?", (task_id,)).fetchone()[0]
            except sqlite3.Error as exc:
                raise LogUnavailable(type(exc).__name__) from None
        events = [{"protocol": PROTOCOL_VERSION, "task_id": task_id, "event_id": str(seq), "type": kind, "data": json.loads(data)} for seq, kind, data in rows]
        # A long log is served in pages of 2000; next_cursor is always the id of the task's last event, so a client that has not
        # reached it simply asks again.
        return {"events": events, "state": state, "next_cursor": last}

    def all_events(self, task_id):
        with self._lock:
            rows = self._db.execute("SELECT seq, type, data FROM events WHERE task_id = ? ORDER BY seq", (task_id,)).fetchall()
        return [(seq, kind, json.loads(data)) for seq, kind, data in rows]

    def incomplete(self):
        with self._lock:
            marks = ",".join("?" * len(TERMINAL_STATES))
            rows = self._db.execute(f"SELECT id, principal, workspace, state, instruction FROM tasks WHERE state NOT IN ({marks})", tuple(TERMINAL_STATES)).fetchall()
        return [{"id": r[0], "principal": r[1], "workspace": r[2], "state": r[3], "instruction": r[4]} for r in rows]

    def usage_summary(self, *, task_id=None, principal=None, workspace=None, since=None, until=None):
        """Known measured subtotals plus explicit missing/invalid call counts."""
        from .usage import TOKEN_FIELDS
        clauses, params = [], []
        for expression, value in (("u.task_id=?", task_id), ("t.principal=?", principal), ("t.workspace=?", workspace), ("u.created>=?", since), ("u.created<?", until)):
            if value is not None:
                clauses.append(expression)
                params.append(value)
        sql = "SELECT u.task_id,u.data FROM model_usage u JOIN tasks t ON t.id=u.task_id"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        groups, tasks = {}, set()
        for tid, raw in rows:
            data = json.loads(raw)
            key = (data["provider"], data["model"])
            group = groups.setdefault(key, {"provider": key[0], "model": key[1], "calls": 0, "unavailable_calls": 0, "unfinished_calls": 0,
                                           **{"known_" + field: 0 for field in TOKEN_FIELDS}})
            tasks.add(tid)
            group["calls"] += 1
            group["unavailable_calls"] += not data.get("available", False)
            group["unfinished_calls"] += data.get("code") == "model_call_started"
            for field in TOKEN_FIELDS:
                if type(data.get(field)) is int:
                    group["known_" + field] += data[field]
        return {"tasks": len(tasks), "calls": len(rows), "unavailable_calls": sum(g["unavailable_calls"] for g in groups.values()),
                "unfinished_calls": sum(g["unfinished_calls"] for g in groups.values()),
                **{"known_" + field: sum(g["known_" + field] for g in groups.values()) for field in TOKEN_FIELDS},
                "by_model": list(groups.values()), "currency_cost": None,
                "note": "provider-reported tokens; cached/reasoning counts are subsets, not additions; missing usage is unknown, not zero; no price estimate"}

    def export_events(self, *, task_id=None, include_content=False):
        """Local operator export. Content is opt-in; reads are paged to bound memory."""
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        try:
            connection.execute("BEGIN")  # one stable WAL snapshot even during live writes/pruning
            sql = "SELECT e.task_id,e.seq,e.type,e.data FROM events e"
            params = []
            if task_id is not None:
                sql += " WHERE e.task_id=?"
                params.append(task_id)
            sql += " ORDER BY e.task_id,e.seq"
            cursor = connection.execute(sql, params)
            previous_task = None
            while rows := cursor.fetchmany(500):
                for tid, seq, kind, raw in rows:
                    if include_content and tid != previous_task:
                        task = connection.execute("SELECT principal,workspace,instruction,state,created FROM tasks WHERE id=?", (tid,)).fetchone()
                        yield {"task_id": tid, "type": "task_metadata", "data": {
                            "principal": task[0], "workspace": task[1], "instruction": task[2], "state": task[3], "created": task[4]}}
                        previous_task = tid
                    row = {"task_id": tid, "event_id": seq, "type": kind}
                    if include_content:
                        row["data"] = json.loads(raw)
                    yield row
        finally:
            connection.close()

    def prune_logs(self, before, *, execute=False, task_id=None):
        """Prune old terminal-task content, preserving usage, idempotency and final state."""
        marks = ",".join("?" * len(TERMINAL_STATES))
        sql = f"SELECT id,instruction FROM tasks WHERE created<? AND state IN ({marks})"
        params = [before, *TERMINAL_STATES]
        if task_id is not None:
            sql += " AND id=?"
            params.append(task_id)
        report = {"executed": bool(execute), "eligible_tasks": 0, "events_to_remove": 0, "instructions_to_clear": 0,
                  "usage_ledger_preserved": True}
        with self._tx() as db:
            for tid, instruction in db.execute(sql, params).fetchall():
                final = db.execute("SELECT MAX(seq) FROM events WHERE task_id=? AND type='state'", (tid,)).fetchone()[0]
                if final is None:
                    continue  # no trustworthy terminal event: never prune an ambiguous history
                count = db.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND seq!=?", (tid, final)).fetchone()[0]
                if not count and not instruction:
                    continue
                report["eligible_tasks"] += 1
                report["events_to_remove"] += count
                report["instructions_to_clear"] += bool(instruction)
                if execute:
                    db.execute("DELETE FROM events WHERE task_id=? AND seq!=?", (tid, final))
                    db.execute("UPDATE tasks SET instruction='' WHERE id=?", (tid,))
        return report
