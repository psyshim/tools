"""Workspace-scoped read operations."""
import hashlib
from pathlib import Path

class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("workspace must be a directory")

    def _resolve(self, name):
        path = Path(name)
        if path.is_absolute():
            raise ValueError("absolute path rejected")
        candidate = (self.root / path).resolve(strict=False)
        if not candidate.is_relative_to(self.root):
            raise ValueError("outside workspace")
        return candidate

    def list_directory(self, path=".", depth=1):
        if type(depth) is not int or not 1 <= depth <= 3:
            raise ValueError("invalid depth")
        base = self._resolve(path)
        if not base.is_dir():
            raise ValueError("not a directory")
        results = []
        def walk(directory, remaining):
            for child in sorted(directory.iterdir()):
                if child.is_symlink():
                    continue
                results.append(child.relative_to(self.root).as_posix() + ("/" if child.is_dir() else ""))
                if len(results) > 500:
                    raise ValueError("too many entries")
                if child.is_dir() and remaining > 1 and child.name not in {".git", "node_modules", ".venv"}:
                    walk(child, remaining - 1)
        walk(base, depth)
        return results

    def read_file(self, path, start_line=1, end_line=200):
        if type(start_line) is not int or type(end_line) is not int or start_line < 1 or end_line < start_line or end_line - start_line >= 200:
            raise ValueError("invalid line range")
        target = self._resolve(path)
        if target.is_symlink() or not target.is_file():
            raise ValueError("not a regular file")
        if target.stat().st_size > 1024 * 1024:
            raise ValueError("file too large")
        payload = target.read_bytes()
        return {"path": target.relative_to(self.root).as_posix(), "sha256": hashlib.sha256(payload).hexdigest(), "start_line": start_line, "lines": payload.decode("utf-8").splitlines()[start_line - 1:end_line]}
