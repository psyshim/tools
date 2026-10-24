"""Local component administration: usage, log export and explicit retention.

python -m coding_service.manage --db .code-attach/service.db usage
No host adapter or running service is required. Nothing is pruned without --execute.
"""
import argparse
from datetime import datetime
from contextlib import closing
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import math

from .store import TaskStore
from .errors import LogUnavailable


def export_snapshot(store, output, *, task_id=None, include_content=False):
    """Sync a stable snapshot before publishing it without replacing any file."""
    if output.exists():
        raise FileExistsError("export already exists")
    count, metadata, digest = 0, 0, hashlib.sha256()
    descriptor, temporary = tempfile.mkstemp(prefix=".code-attach-export-", dir=output.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream, closing(store.export_events(task_id=task_id, include_content=include_content)) as records:
            for row in records:
                payload = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
                stream.write(payload)
                digest.update(payload)
                if row["type"] == "task_metadata":
                    metadata += 1
                else:
                    count += 1
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            os.rename(temporary, output)
            temporary = None
        else:
            os.link(temporary, output, follow_symlinks=False)
        return {"events_exported": count, "task_metadata_exported": metadata, "content_included": include_content, "sha256": digest.hexdigest()}
    finally:
        if temporary is not None:
            os.unlink(temporary)


def timestamp(text):
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError("timezone required")
        return value.timestamp()
    except ValueError:
        raise argparse.ArgumentTypeError("use an ISO date/time with a timezone, e.g. 2026-10-01T00:00:00+09:00") from None


def timeout_seconds(text):
    value = float(text)
    if not math.isfinite(value) or not 1 <= value <= 300:
        raise argparse.ArgumentTypeError("timeout must be 1 to 300 seconds")
    return value


def sha256(text):
    if len(text) != 64 or any(c not in "0123456789abcdefABCDEF" for c in text):
        raise argparse.ArgumentTypeError("SHA-256 must be 64 hexadecimal characters")
    return text.lower()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    usage = sub.add_parser("usage", help="measured cumulative tokens; unknown calls shown separately")
    usage.add_argument("--task-id")
    usage.add_argument("--principal")
    usage.add_argument("--workspace")
    usage.add_argument("--since", type=timestamp)
    usage.add_argument("--until", type=timestamp)
    export = sub.add_parser("export", help="export log metadata as JSONL (content requires opt-in)")
    export.add_argument("--task-id")
    export.add_argument("--output", required=True, type=Path)
    export.add_argument("--include-content", action="store_true")
    backup = sub.add_parser("backup", help="consistent service database snapshot including usage and logs (not device state)")
    backup.add_argument("--output", required=True, type=Path)
    backup.add_argument("--timeout", type=timeout_seconds, default=60)
    restore = sub.add_parser("restore", help="restore a verified service snapshot to a NEW --db path; never resume unfinished work")
    restore.add_argument("--input", required=True, type=Path)
    restore.add_argument("--sha256", required=True, type=sha256)
    restore.add_argument("--timeout", type=timeout_seconds, default=60)
    prune = sub.add_parser("prune", help="preview old terminal-task log deletion; retain token ledger")
    prune.add_argument("--before", required=True, type=timestamp)
    prune.add_argument("--task-id")
    prune.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.command != "restore" and not args.db.is_file():
        parser.error("database does not exist; refusing to create an empty administration database")
    store = None
    try:
        if args.command == "restore":
            from .backup import restore_store
            print(json.dumps(restore_store(args.input, args.db, args.sha256, timeout=args.timeout), ensure_ascii=False, indent=2))
            return 0
        store = TaskStore(args.db)
        if args.command == "usage":
            result = store.usage_summary(task_id=args.task_id, principal=args.principal, workspace=args.workspace, since=args.since, until=args.until)
        elif args.command == "prune":
            result = store.prune_logs(args.before, execute=args.execute, task_id=args.task_id)
        elif args.command == "backup":
            from .backup import backup_store
            result = backup_store(store, args.output, timeout=args.timeout)
        else:
            result = export_snapshot(store, args.output, task_id=args.task_id, include_content=args.include_content)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (LogUnavailable, sqlite3.Error, OSError, ValueError) as exc:
        print(f"administration failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
