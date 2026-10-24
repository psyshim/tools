"""Consistent local service-state snapshots; never overwrite or replay old work."""
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import math

from .store import TaskStore, SCHEMA_VERSION, SCHEMA_COLUMNS, _event_object, _usage_object
from .errors import LogUnavailable

APPLICATION_ID = 0x43415454
FORMAT = "code-attach-service-backup-v1"

def _deadline(timeout):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError("timeout must be positive and at most 300 seconds")
    return time.monotonic() + timeout


def _check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("backup/restore deadline exceeded")


def _absent(output):
    if os.path.lexists(output):
        raise FileExistsError("destination already exists")


def _cleanup(temporary):
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            Path(str(temporary) + suffix).unlink()
        except FileNotFoundError:
            pass


def _publish(temporary, output):
    if os.name == "nt":
        os.rename(temporary, output)  # Windows refuses an existing destination
    else:
        os.link(temporary, output, follow_symlinks=False)


def _receipt(path, *, deadline=None):
    digest, size = hashlib.sha256(), 0
    with Path(path).open("r+b") as stream:
        while True:
            _check_deadline(deadline)
            chunk = stream.read(65536)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        _check_deadline(deadline)
        os.fsync(stream.fileno())
        _check_deadline(deadline)
    return {"sha256": digest.hexdigest(), "bytes": size}


def validate_backup(path, *, deadline=None):
    """No schema creation or repair while deciding whether an input is a backup."""
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if deadline is not None:
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("not a code-attach service backup")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("backup schema version is not supported")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("backup integrity check failed")
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != SCHEMA_COLUMNS.keys():
            raise ValueError("backup tables are not supported")
        for table, required in SCHEMA_COLUMNS.items():
            if required != {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}:
                raise ValueError("backup schema is not supported")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("backup has orphaned state")
        for table in ("events", "model_usage"):
            if connection.execute(f"SELECT 1 FROM {table} r LEFT JOIN tasks t ON t.id=r.task_id WHERE t.id IS NULL LIMIT 1").fetchone():
                raise ValueError("backup has orphaned state")
            query = "SELECT type,data FROM events" if table == "events" else "SELECT call_id,data FROM model_usage"
            for identity, raw in connection.execute(query):
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("validation deadline exceeded")
                try:
                    if table == "events":
                        _event_object(identity, raw)
                    elif _usage_object(raw)["model_call_id"] != identity:
                        raise ValueError("backup usage identity mismatch")
                except LogUnavailable:
                    raise ValueError("backup contains invalid durable records") from None
    finally:
        connection.close()


def backup_store(store, output, *, timeout=60):
    """SQLite backup API captures live WAL contents, unlike copying the .db file."""
    output = Path(output)
    deadline = _deadline(timeout)
    _absent(output)
    descriptor, temporary = tempfile.mkstemp(prefix=".code-attach-backup-", dir=output.parent)
    os.close(descriptor)
    try:
        target = sqlite3.connect(temporary, isolation_level=None)
        try:
            def progress(status, remaining, total):
                if time.monotonic() >= deadline:
                    raise TimeoutError("backup deadline exceeded")
            with store._lock:
                store._db.backup(target, pages=256, progress=progress, sleep=0.01)
            target.execute("PRAGMA journal_mode=DELETE")
            target.execute(f"PRAGMA application_id={APPLICATION_ID}")
        finally:
            target.close()
        validate_backup(temporary, deadline=deadline)
        receipt = _receipt(temporary, deadline=deadline)
        _publish(temporary, output)
        return {"format": FORMAT, "scope": "coding_service_database", "device_state_included": False, **receipt}
    finally:
        _cleanup(temporary)


def restore_store(source, output, expected_sha256, *, timeout=60):
    """Restore to a NEW path; old unfinished tasks become explicit reconciliation work."""
    source, output = Path(source), Path(output)
    deadline = _deadline(timeout)
    _absent(output)
    descriptor, temporary = tempfile.mkstemp(prefix=".code-attach-restore-", dir=output.parent)
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "wb") as target, source.open("rb") as stream:
            while chunk := stream.read(65536):
                if time.monotonic() >= deadline:
                    raise TimeoutError("restore deadline exceeded")
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        if digest.hexdigest() != expected_sha256:
            raise ValueError("backup SHA-256 mismatch")
        validate_backup(temporary, deadline=deadline)
        restored = TaskStore(temporary)
        try:
            _check_deadline(deadline)
            restored._db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
            pending = restored.incomplete()
            for row in pending:
                _check_deadline(deadline)
                with restored._tx() as database:
                    TaskStore._put(database, row["id"], "notice", {"code": "restored_from_backup", "manual_review_required": True,
                        "detail": "this idempotency key stays consumed; reconcile the actual workspace before sending a new request"})
                    TaskStore._put(database, row["id"], "state", {"state": "NEEDS_RECONCILIATION",
                        "detail": "restored snapshot may predate real model calls or effects; nothing will resume automatically"})
        finally:
            restored.close()
        # Ensure publication does not depend on a sidecar WAL file.
        connection = sqlite3.connect(temporary, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
        finally:
            connection.close()
        validate_backup(temporary, deadline=deadline)
        receipt = _receipt(temporary, deadline=deadline)
        _publish(temporary, output)
        return {"restored": True, "scope": "coding_service_database", "device_state_included": False,
                "unfinished_tasks_held": len(pending), "automatic_resume": False, **receipt}
    finally:
        _cleanup(temporary)
