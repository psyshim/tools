"""Opt-in local-only text edit workflow. No HTTP or arbitrary command execution."""
import ast
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import uuid

_LOCK = threading.RLock()

class Conflict(Exception):
    pass

class Workflow:
    def __init__(self, workspace):
        self.root = Path(workspace).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("workspace must be a directory")

    def _file(self, relative):
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ValueError("invalid path")
        candidate = self.root / relative
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.root) or candidate.is_symlink() or not resolved.is_file():
            raise ValueError("outside workspace or not a file")
        if ".git" in candidate.relative_to(self.root).parts:
            raise ValueError("git metadata not editable")
        return resolved

    def inspect(self, relative):
        target = self._file(relative)
        data = target.read_bytes()
        if len(data) > 1048576:
            raise ValueError("file too large")
        return {"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

    def replace_text(self, relative, expected_sha256, original, replacement):
        if not all(isinstance(x, str) for x in (expected_sha256, original, replacement)):
            raise ValueError("invalid arguments")
        if not original or len(original) > 20000 or len(replacement) > 20000:
            raise ValueError("invalid edit size")
        with _LOCK:
            path = self._file(relative)
            data = path.read_bytes()
            if len(data) > 1048576:
                raise ValueError("file too large")
            if hashlib.sha256(data).hexdigest() != expected_sha256:
                raise Conflict("stale SHA-256")
            content = data.decode("utf-8")
            if content.count(original) != 1:
                raise Conflict("expected exactly one occurrence")
            updated = content.replace(original, replacement, 1).encode("utf-8")
            if len(updated) > 1048576:
                raise ValueError("updated file too large")
            mode = path.stat().st_mode
            fd, tmp = tempfile.mkstemp(prefix=".agent-", dir=str(path.parent))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(updated)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp, mode)
                # Cooperative in-process only: external editor races remain possible.
                if path.read_bytes() != data:
                    raise Conflict("file changed during edit")
                os.replace(tmp, path)
                tmp = None
            finally:
                if tmp is not None:
                    os.unlink(tmp)
            return {"path": relative, "sha256": hashlib.sha256(updated).hexdigest(), "operation_id": str(uuid.uuid4())}

    def test_python_syntax(self, relative):
        target = self._file(relative)
        source = target.read_bytes()
        if len(source) > 1048576 or target.suffix != ".py":
            raise ValueError("only small Python files supported")
        try:
            ast.parse(source, filename=relative)
        except (SyntaxError, UnicodeDecodeError) as error:
            return {"status": "FAIL", "error": str(error)}
        return {"status": "PASS"}
