"""Append-only local task evidence; independent of any host framework or model."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading


def default_log_dir(workspace):
    """Keep logs outside the project, in the current user's persistent state directory."""
    if os.name == "nt":
        # Store Python virtualizes AppData writes, so the displayed AppData path
        # may not exist in Explorer. A home-directory path is stable for users.
        base = Path.home() / ".code-attach"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    identity = os.path.normcase(str(Path(workspace).resolve()))
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return (base / "logs" / key if os.name == "nt"
            else base / "code-attach" / "logs" / key)


class ActivityLog:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()

    def record(self, task_id, event):
        if not re.fullmatch(r"[a-f0-9]{32}", task_id):
            raise ValueError("invalid task id")
        record = {"schema": 1, "timestamp": datetime.now(timezone.utc).isoformat(),
                  "task_id": task_id, **event}
        payload = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8", "backslashreplace")
        # One task file per UUID; flush each event before returning to the agent.
        with self._lock:
            path = self.directory / (task_id + ".jsonl")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "ab") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())


def display_path(path):
    """Home-relative spelling (~/...) for anything shown to a person or a browser; never the user name."""
    resolved, home = Path(path).expanduser().resolve(), Path.home().resolve()
    try:
        return "~/" + resolved.relative_to(home).as_posix()
    except ValueError:
        return resolved.name
