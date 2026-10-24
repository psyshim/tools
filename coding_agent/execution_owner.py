"""One local executor per workspace. Not a multi-writer task scheduler.

The lock file lives in a private state directory outside the workspace, keyed by
a hash of the canonical root path. It is never created inside the enrolled tree
(ADR-001: app-private state outside the worktree).

State directory: $CODING_AGENT_STATE_DIR, else ~/.coding-agent/locks.
"""
import hashlib
import os
from pathlib import Path

class OwnerBusy(RuntimeError):
    pass

def _state_dir():
    configured = os.environ.get("CODING_AGENT_STATE_DIR")
    base = Path(configured) if configured else Path.home() / ".coding-agent" / "locks"
    base.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(base, 0o700)
    return base

def lock_path_for(root):
    digest = hashlib.sha256(str(Path(root).resolve(strict=True)).encode("utf-8")).hexdigest()
    return _state_dir() / f"{digest}.lock"

class SingleExecutor:
    def __init__(self, workspace):
        root = Path(workspace).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace must be a directory")
        self._file = open(lock_path_for(root), "a+b")
        try:
            self._file.seek(0)
            self._file.write(b"\x00")
            self._file.flush()
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise OwnerBusy("workspace already has an executor") from exc
            else:
                import fcntl
                try:
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise OwnerBusy("workspace already has an executor") from exc
        except BaseException:
            self._file.close()
            raise

    def close(self):
        if not self._file.closed:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
