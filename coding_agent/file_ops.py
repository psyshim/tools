"""Bounded workspace housekeeping with no-clobber destinations and recoverable deletion.

Plans are data only. The attachment's existing approval gate invokes apply().
Checks coordinate in-process callers; hostile external filesystem races still
require an OS sandbox or an editor-coordinated writer.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import tempfile
import uuid

from .workspace import Workspace
from .workflow import Conflict

_LOCK = threading.RLock()
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}


class FileOperations:
    def __init__(self, root, max_file_bytes=8 * 1024 * 1024, recycle_dir=None):
        self.ws = Workspace(root, max_file_bytes=max_file_bytes)
        identity = os.path.normcase(str(self.ws.root))
        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        self.recycle_dir = (Path(recycle_dir).expanduser().resolve() if recycle_dir is not None
                            else Path.home() / ".code-attach" / "recycle" / key)
        if self.recycle_dir.is_relative_to(self.ws.root):
            raise ValueError("recycle directory must be outside workspace")

    @staticmethod
    def _link(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)

    def resolve(self, name):
        if not isinstance(name, str) or not name or "\x00" in name:
            raise ValueError("invalid path")
        relative = Path(name)
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            raise ValueError("workspace-relative path required")
        for part in relative.parts:
            if ":" in part or part.endswith((".", " ")) or part.split(".")[0].upper() in _RESERVED:
                raise ValueError("ambiguous or reserved path")
        target = self.ws._resolve(relative)
        if target == self.ws.root:
            raise ValueError("workspace root is not an operation target")
        current = self.ws.root
        for part in relative.parts:
            current = current / part
            if self._link(current):
                raise ValueError("symlink or reparse path rejected")
        return target

    def _read(self, name):
        target = self.resolve(name)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(target, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("regular file required")
            data = stream.read(self.ws.max_file_bytes + 1)
        if len(data) > self.ws.max_file_bytes:
            raise ValueError("file too large")
        return target, data, {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                              "identity": [info.st_dev, info.st_ino], "mode": stat.S_IMODE(info.st_mode)}

    @staticmethod
    def _absent(target):
        if target.exists() or target.is_symlink():
            raise FileExistsError("destination already exists")

    def plan(self, operation, path, destination=None):
        target = self.resolve(path)
        relative = target.relative_to(self.ws.root).as_posix()
        if operation in {"delete_file", "move_file", "copy_file"}:
            _, _, info = self._read(path)
            result = {"operation": operation, "path": relative, **info}
            if operation != "delete_file":
                dest = self.resolve(destination)
                self._absent(dest)
                result["destination"] = dest.relative_to(self.ws.root).as_posix()
            return result
        if operation == "create_directory":
            self._absent(target)
            return {"operation": operation, "path": relative}
        if operation == "remove_empty_directory":
            if not target.is_dir() or any(target.iterdir()):
                raise ValueError("empty directory required")
            info = target.stat()
            return {"operation": operation, "path": relative, "identity": [info.st_dev, info.st_ino]}
        raise ValueError("unknown file operation")

    def _backup(self, data, metadata):
        backup_id = uuid.uuid4().hex
        directory = self.recycle_dir / backup_id
        directory.mkdir(parents=True, mode=0o700)
        for name, payload in (("content", data), ("metadata.json", json.dumps(metadata, ensure_ascii=False).encode("utf-8"))):
            descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        return backup_id

    def _load_backup(self, backup_id):
        if not isinstance(backup_id, str) or not re.fullmatch(r"[a-f0-9]{32}", backup_id):
            raise ValueError("invalid backup id")
        directory = self.recycle_dir / backup_id
        if any(self._link(p) for p in (directory, directory / "metadata.json", directory / "content")):
            raise ValueError("linked backup rejected")
        if (directory / "metadata.json").stat().st_size > 8192:
            raise ValueError("invalid backup metadata")
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        with (directory / "content").open("rb") as stream:
            data = stream.read(self.ws.max_file_bytes + 1)
        if len(data) > self.ws.max_file_bytes or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
            raise ValueError("backup content mismatch")
        return data, metadata

    def list_backups(self):
        results = []
        if self.recycle_dir.exists():
            for directory in sorted(self.recycle_dir.iterdir(), key=lambda p: p.name):
                if len(results) >= 100:
                    break
                try:
                    _, info = self._load_backup(directory.name)
                    results.append({"backup_id": directory.name, **{k: info[k] for k in ("path", "sha256", "size")}})
                except (OSError, ValueError, KeyError):
                    continue
        return {"backups": results}

    def plan_restore(self, backup_id, path=None):
        _, metadata = self._load_backup(backup_id)
        target = self.resolve(path if path is not None else metadata["path"])
        self._absent(target)
        return {"operation": "restore_file", "path": target.relative_to(self.ws.root).as_posix(),
                "backup_id": backup_id, "sha256": metadata["sha256"], "size": metadata["size"]}

    def _verified(self, plan):
        target, data, info = self._read(plan["path"])
        if any(info[k] != plan[k] for k in ("sha256", "size", "identity")):
            raise Conflict("source changed since proposal")
        return target, data, info

    @staticmethod
    def _write_exclusive(target, data, mode=0o600):
        # Publish only fully written bytes. Opening the destination with O_EXCL
        # directly leaves a partial file behind on disk-full/write/fsync errors.
        descriptor, temporary = tempfile.mkstemp(prefix=".agent-new-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            if os.name == "nt":
                os.rename(temporary, target)  # Windows rename refuses an existing destination
                temporary = None
            else:
                os.link(temporary, target, follow_symlinks=False)  # atomic no-clobber publication
        finally:
            if temporary is not None:
                os.unlink(temporary)

    def apply(self, plan):
        with _LOCK:
            operation = plan["operation"]
            target = self.resolve(plan["path"])
            if operation == "create_directory":
                self._absent(target)
                target.mkdir(parents=True, exist_ok=False)
            elif operation == "remove_empty_directory":
                info = target.stat()
                if [info.st_dev, info.st_ino] != plan["identity"] or not target.is_dir() or any(target.iterdir()):
                    raise Conflict("directory changed since proposal")
                target.rmdir()
            elif operation == "restore_file":
                self._absent(target)
                data, metadata = self._load_backup(plan["backup_id"])
                if metadata["sha256"] != plan["sha256"] or len(data) != plan["size"]:
                    raise Conflict("backup changed since proposal")
                target.parent.mkdir(parents=True, exist_ok=True)
                self.resolve(plan["path"])
                self._write_exclusive(target, data, metadata.get("mode", 0o600))
            elif operation == "delete_file":
                target, data, info = self._verified(plan)
                backup_id = self._backup(data, {"path": plan["path"], **info})
                self._verified(plan)
                target.unlink()
                return {"status": "APPLIED", "operation": operation, "path": plan["path"], "backup_id": backup_id}
            elif operation in {"move_file", "copy_file"}:
                target, data, info = self._verified(plan)
                dest = self.resolve(plan["destination"])
                self._absent(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                self.resolve(plan["destination"])
                if operation == "copy_file":
                    self._write_exclusive(dest, data, info["mode"])
                else:
                    # A hard link creates the destination without replacing it,
                    # unlike POSIX rename(). Only then remove the old name.
                    os.link(target, dest, follow_symlinks=False)
                    try:
                        self._verified(plan)
                    except BaseException:
                        # Keep both names on uncertainty; never delete user data.
                        raise Conflict("source changed during move; both paths retained")
                    target.unlink()
                return {"status": "APPLIED", "operation": operation, "path": plan["path"], "destination": plan["destination"]}
            else:
                raise ValueError("unknown file operation")
            return {"status": "APPLIED", "operation": operation, "path": plan["path"]}
