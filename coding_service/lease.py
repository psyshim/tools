"""Process ownership of one service database; administration remains concurrent."""
import os
from pathlib import Path


# Keep descriptors alive even when a blocked worker outlives server shutdown.
_HELD = set()


class ServiceAlreadyRunning(OSError):
    pass


class ServiceLease:
    def __init__(self, database):
        database = Path(database).resolve()
        database.parent.mkdir(parents=True, exist_ok=True)
        self.path = database.with_name(database.name + ".service.lock")
        self._file = open(self.path, "a+b")
        try:
            # Do not write through a competing Windows byte lock.
            if self.path.stat().st_size == 0:
                self._file.write(b"\0")
                self._file.flush()
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            raise ServiceAlreadyRunning("another service owns this task database") from exc
        _HELD.add(self)

    def close(self):
        if not self._file.closed:
            # Closing releases the OS lock, including after an abrupt process exit.
            self._file.close()
        _HELD.discard(self)
