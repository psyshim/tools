"""Opt-in local-only text edit workflow. No HTTP or arbitrary command execution."""
import ast
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
from .text_encoding import decode_source, encode_source

_LOCK = threading.RLock()

class Conflict(Exception):
    pass

class Workflow:
    def __init__(self, workspace, max_file_bytes=1048576):
        if type(max_file_bytes) is not int or not 1 <= max_file_bytes <= 8 * 1024 * 1024:
            raise ValueError("invalid file size limit")
        self.max_file_bytes = max_file_bytes
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
        if len(data) > self.max_file_bytes:
            raise ValueError("file too large")
        return {"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

    def replace_text(self, relative, expected_sha256, original, replacement, encoding=None):
        if not all(isinstance(x, str) for x in (expected_sha256, original, replacement)):
            raise ValueError("invalid arguments")
        if not original or len(original) > 20000 or len(replacement) > 20000:
            raise ValueError("invalid edit size")
        with _LOCK:
            path = self._file(relative)
            data = path.read_bytes()
            if len(data) > self.max_file_bytes:
                raise ValueError("file too large")
            if hashlib.sha256(data).hexdigest() != expected_sha256:
                raise Conflict("stale SHA-256")
            content, codec, prefix = decode_source(data, encoding)
            if content.count(original) != 1:
                raise Conflict("expected exactly one occurrence")
            updated = encode_source(content.replace(original, replacement, 1), codec, prefix)
            if len(updated) > self.max_file_bytes:
                raise ValueError("updated file too large")
            self._swap(path, data, updated)
            return {"path": relative, "sha256": hashlib.sha256(updated).hexdigest(), "operation_id": str(uuid.uuid4())}

    @staticmethod
    def _swap(path, data, updated):
        """Write `updated` next to `path` and atomically replace it, unless the file changed since `data` was read."""
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
            for attempt in range(6):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    # Windows: an editor, indexer or antivirus may hold the target open for a moment. Retry briefly, never loop forever.
                    if attempt == 5:
                        raise
                    time.sleep(0.05 * (attempt + 1))
            tmp = None
        finally:
            if tmp is not None:
                os.unlink(tmp)

    def replace_bytes(self, relative, expected_sha256, updated):
        """Whole-file compare-and-swap: replace the file with `updated` only if it still hashes to `expected_sha256`.
        The building block for edits computed elsewhere (line ranges, notebook cells, batches, undo)."""
        if not isinstance(expected_sha256, str) or not isinstance(updated, (bytes, bytearray)):
            raise ValueError("invalid arguments")
        updated = bytes(updated)
        with _LOCK:
            path = self._file(relative)
            data = path.read_bytes()
            if len(data) > self.max_file_bytes or len(updated) > self.max_file_bytes:
                raise ValueError("file too large")
            if hashlib.sha256(data).hexdigest() != expected_sha256:
                raise Conflict("stale SHA-256")
            self._swap(path, data, updated)
            return {"path": relative, "sha256": hashlib.sha256(updated).hexdigest(), "operation_id": str(uuid.uuid4()), "before": data}

    def test_python_syntax(self, relative):
        target = self._file(relative)
        source = target.read_bytes()
        if len(source) > self.max_file_bytes or target.suffix != ".py":
            raise ValueError("only small Python files supported")
        try:
            ast.parse(source, filename=relative)
        except (SyntaxError, UnicodeDecodeError) as error:
            return {"status": "FAIL", "error": str(error)}
        return {"status": "PASS"}
