"""Process ownership of one service database; administration remains concurrent."""
import os
from pathlib import Path


# Keep descriptors alive even when a blocked worker outlives server shutdown.
_HELD = set()


class LeaseError(OSError):
    """The service must not start on this database. The command line exits with code 5 (code 4 stays for a store that cannot be read)."""


class ServiceAlreadyRunning(LeaseError):
    pass


class LeaseUnsupported(LeaseError):
    """The database sits somewhere an OS lock cannot protect: a network volume, or a file with more than one name."""


def _is_remote(path):
    """True for a Windows network location. The caller passes a RESOLVED path: Windows resolves a mapped network drive to its UNC
    form, so a path string is enough and the service needs no system-call access (its isolation test forbids ctypes).
    Other platforms: not detected (documented)."""
    if os.name != "nt":
        return False
    text = str(path)
    bs = chr(92)
    if text.upper().startswith(bs * 2 + "?" + bs + "UNC" + bs):
        return True
    return text.startswith(bs * 2) and not text.startswith((bs * 2 + "?" + bs, bs * 2 + "." + bs))


def check_location(database):
    if _is_remote(database):
        raise LeaseUnsupported("the task database is on a network location; an OS lease cannot protect it. Use a local disk")
    try:
        links = database.stat().st_nlink
    except OSError:
        return
    if links > 1:
        raise LeaseUnsupported("the task database has more than one name (hard link); the lease is per path and would not protect it. Use one canonical file")


class ServiceLease:
    def __init__(self, database):
        database = Path(database).resolve()
        database.parent.mkdir(parents=True, exist_ok=True)
        check_location(database)
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
