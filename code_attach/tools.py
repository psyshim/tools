"""Tools the model may call. Side effects (apply, run_tests) are gated by approval in session.py."""
import difflib
import hashlib
import fnmatch
import json
import os
import subprocess
import threading
import uuid
from pathlib import Path
from coding_agent.workflow import Workflow, Conflict
from coding_agent.workspace import Workspace

MAX_OUTPUT = 8000
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".tox", "dist", "build", ".idea"}
MAX_FILES_SCANNED = 20000
MAX_MATCHES = 50
MAX_PER_FILE = 5
CR, LF = chr(13), chr(10)
CRLF = CR + LF
EFFECT_TOOLS = {"apply_proposal", "run_tests"}

SCHEMAS = [
    {"name": "list_dir", "description": "List files under a workspace-relative directory.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "depth": {"type": "integer"}}}},
    {"name": "find_files", "description": "Find files by glob (e.g. '*.ini', 'settings*', 'src/**/*.py') anywhere under a directory, skipping build/vendor/VCS folders.",
     "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "search_text", "description": "Case-insensitive substring search across text files under a directory. Use it to locate code, config or docs whose location is unknown.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}}, "required": ["query"]}},
    {"name": "read_file", "description": "Read lines of a text file (max 200 lines per call).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]}},
    {"name": "propose_edit", "description": "Propose replacing one exact, unique text occurrence in a file. Nothing is written until apply_proposal is approved.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, "required": ["path", "old", "new"]}},
    {"name": "propose_new_file", "description": "Propose creating a new file. Nothing is written until apply_proposal is approved.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "apply_proposal", "description": "Apply a proposal by id. Requires user approval.",
     "parameters": {"type": "object", "properties": {"proposal_id": {"type": "string"}}, "required": ["proposal_id"]}},
    {"name": "run_tests", "description": "Run a pre-configured test command by name. Requires user approval.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]

def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

class ToolBox:
    def __init__(self, root, test_commands=None, test_timeout=180):
        self.ws = Workspace(root)
        self.flow = Workflow(root)
        self.test_commands = dict(test_commands or {})
        self.test_timeout = test_timeout
        self.proposals = {}
        self._lock = threading.Lock()

    def schemas(self):
        return [s for s in SCHEMAS if s["name"] != "run_tests" or self.test_commands]

    def describe_effect(self, name, args):
        """Return (summary, fingerprint) shown to the user before approval."""
        if name == "apply_proposal":
            p = self.proposals.get(args.get("proposal_id"))
            if not p:
                raise ValueError("unknown proposal")
            return {"kind": "apply", "path": p["path"], "diff": p["diff"]}, p["fingerprint"]
        if name == "run_tests":
            argv = self.test_commands.get(args.get("name"))
            if not argv:
                raise ValueError("unknown test command")
            summary = {"kind": "run_tests", "name": args["name"], "argv": argv}
            return summary, _digest(summary)
        raise ValueError("not an effect tool")

    def call(self, name, args):
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        fn = getattr(self, "t_" + name, None)
        if fn is None or name not in {s["name"] for s in SCHEMAS}:
            raise ValueError("unknown tool")
        return fn(**args)

    # --- read-only ---
    def t_list_dir(self, path=".", depth=1):
        return {"entries": self.ws.list_directory(path, depth)}

    def t_read_file(self, path, start_line=1, end_line=200):
        return self.ws.read_file(path, start_line, min(end_line, start_line + 199))

    def _files(self, path):
        base = self.ws._resolve(path)
        if not base.is_dir():
            raise ValueError("not a directory")
        scanned = 0
        for dirpath, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (Path(dirpath) / d).is_symlink())
            for name in sorted(files):
                scanned += 1
                if scanned > MAX_FILES_SCANNED:
                    return
                full = Path(dirpath) / name
                if full.is_symlink():
                    continue
                try:
                    self.ws._resolve(full.relative_to(self.ws.root))
                except ValueError:
                    continue
                yield full

    def t_find_files(self, pattern, path="."):
        hits, truncated = [], False
        for f in self._files(path):
            rel = f.relative_to(self.ws.root).as_posix()
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f.name, pattern):
                if len(hits) >= 100:
                    truncated = True
                    break
                hits.append(rel)
        return {"files": hits, "truncated": truncated}

    def t_search_text(self, query, path=".", glob=None):
        if not isinstance(query, str) or not query or len(query) > 200:
            raise ValueError("invalid query")
        needle, matches, truncated, extra = query.lower(), [], False, {}
        for f in self._files(path):
            rel = f.relative_to(self.ws.root).as_posix()
            if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(f.name, glob)):
                continue
            try:
                if f.stat().st_size > 1048576:
                    continue
                data = f.read_bytes()
            except OSError:
                continue
            if bytes([0]) in data[:4096]:
                continue
            in_file = 0
            for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if needle not in line.lower():
                    continue
                in_file += 1
                if in_file > MAX_PER_FILE:  # a noisy file must not crowd out other files
                    extra[rel] = extra.get(rel, 0) + 1
                elif len(matches) >= MAX_MATCHES:
                    return {"matches": matches, "truncated": True, "more_in_file": extra}
                else:
                    matches.append({"path": rel, "line": n, "text": line.strip()[:200]})
        return {"matches": matches, "truncated": truncated, "more_in_file": extra}

    # --- proposals (no writes) ---
    def _store(self, path, kind, old, new, before, after, sha):
        diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{path}", f"b/{path}"))[:MAX_OUTPUT]
        pid = uuid.uuid4().hex[:12]
        body = {"path": path, "kind": kind, "old": old, "new": new, "sha": sha}
        with self._lock:
            self.proposals[pid] = {**body, "diff": diff, "fingerprint": _digest(body)}
        return {"proposal_id": pid, "diff": diff}

    def t_propose_edit(self, path, old, new):
        info = self.flow.inspect(path)
        text = self.ws._resolve(path).read_bytes().decode("utf-8")
        if CRLF in text and CR not in old:  # CRLF file: the model writes LF
            old, new = old.replace(LF, CRLF), new.replace(LF, CRLF)
        if not old or text.count(old) != 1:
            raise ValueError("old text must occur exactly once")
        return self._store(path, "edit", old, new, text, text.replace(old, new, 1), info["sha256"])

    def t_propose_new_file(self, path, content):
        target = self.ws._resolve(path)
        if target.exists() or target.is_symlink():
            raise ValueError("file already exists")
        if len(content.encode("utf-8")) > 1048576:
            raise ValueError("content too large")
        return self._store(path, "create", "", content, "", content, "")

    # --- effects (called only after approval) ---
    def t_apply_proposal(self, proposal_id):
        p = self.proposals.pop(proposal_id, None)
        if p is None:
            raise ValueError("unknown or already used proposal")
        try:
            if p["kind"] == "edit":
                r = self.flow.replace_text(p["path"], p["sha"], p["old"], p["new"])
                return {"status": "APPLIED", "path": p["path"], "sha256": r["sha256"]}
            target = self.ws._resolve(p["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "x", encoding="utf-8", newline="") as f:
                f.write(p["new"])
            return {"status": "APPLIED", "path": p["path"]}
        except Conflict as exc:
            return {"status": "CONFLICT", "detail": str(exc)}
        except FileExistsError:
            return {"status": "CONFLICT", "detail": "file already exists"}

    def t_run_tests(self, name):
        argv = self.test_commands.get(name)
        if not argv:
            raise ValueError("unknown test command")
        try:
            r = subprocess.run(argv, cwd=self.ws.root, capture_output=True, timeout=self.test_timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            return {"status": "TIMEOUT", "argv": argv, "output": _tail((exc.stdout or b"") + (exc.stderr or b""))}
        except OSError as exc:
            return {"status": "UNKNOWN", "argv": argv, "error": type(exc).__name__}
        return {"status": "PASS" if r.returncode == 0 else "FAIL", "exit_code": r.returncode, "argv": argv, "output": _tail(r.stdout + r.stderr)}

def _tail(data):
    return data.decode("utf-8", "replace")[-MAX_OUTPUT:]
