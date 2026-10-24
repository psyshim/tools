"""Experimental local replacement helper; NOT exposed over HTTP."""
import hashlib
import os
import tempfile
import threading

_LOCK = threading.RLock()

class PatchConflict(Exception):
    pass

class SafeEditor:
    def __init__(self, workspace):
        self.workspace = workspace

    def replace(self, path, expected_sha256, content):
        if not isinstance(content, str) or not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            raise ValueError("invalid content or hash")
        payload = content.encode("utf-8")
        if len(payload) > 1024 * 1024:
            raise ValueError("content too large")
        with _LOCK:
            target = self.workspace._resolve(path)
            if target.is_symlink() or not target.is_file():
                raise ValueError("existing regular file required")
            before = target.read_bytes()
            if hashlib.sha256(before).hexdigest() != expected_sha256:
                raise PatchConflict("file changed since observation")
            mode = target.stat().st_mode
            fd, temporary = tempfile.mkstemp(prefix=".agent-", dir=str(target.parent))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, mode)
                if target.read_bytes() != before:
                    raise PatchConflict("concurrent edit")
                os.replace(temporary, target)
                temporary = None
                return {"path": target.relative_to(self.workspace.root).as_posix(), "sha256": hashlib.sha256(payload).hexdigest()}
            finally:
                if temporary is not None:
                    os.unlink(temporary)
