"""One local executor per workspace. Not a multi-writer task scheduler."""
import os
from pathlib import Path

class OwnerBusy(RuntimeError):
    pass

class SingleExecutor:
    def __init__(self, workspace):
        root = Path(workspace).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace must be a directory")
        self._file = open(root / ".coding-agent-owner.lock", "a+b")
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
