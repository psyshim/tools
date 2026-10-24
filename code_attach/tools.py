"""Tools the model may call. Side effects (apply, run_tests) are gated by approval in session.py."""
import difflib
import errno
import getpass
import hashlib
import fnmatch
import json
import os
import re
import signal
import subprocess
import threading
import uuid
from pathlib import Path
from coding_agent.workflow import Workflow, Conflict
from coding_agent.workspace import Workspace
from coding_agent.file_ops import FileOperations

MAX_OUTPUT = 8000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_DIFF = 64000
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".tox", "dist", "build", ".idea"}
MAX_FILES_SCANNED = 20000
MAX_MATCHES = 50
MAX_PER_FILE = 5
CR, LF = chr(13), chr(10)
BS = chr(92)
CRLF = CR + LF
EFFECT_TOOLS = {"apply_proposal", "run_tests"}
# Environment handed to approved test commands: an allowlist, so host secrets (API keys, tokens) never reach project code.
SAFE_ENV = {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "LANG", "LANGUAGE", "TZ", "CI",
            "PYTHONIOENCODING", "PYTHONUTF8", "VIRTUAL_ENV", "JAVA_HOME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"}

def glob_match(rel, name, pattern):
    """Patterns without '/' match the file name; patterns with '/' match the relative path ('**' spans directories)."""
    if "/" not in pattern:
        return fnmatch.fnmatch(name, pattern)
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch("".join(out), rel) is not None

SCHEMAS = [
    {"name": "list_dir", "description": "List files under a workspace-relative directory.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "depth": {"type": "integer"}}}},
    {"name": "find_files", "description": "Find files by glob (e.g. '*.ini', 'settings*', 'src/**/*.py') anywhere under a directory, skipping build/vendor/VCS folders.",
     "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "search_text", "description": "Case-insensitive substring search across UTF-8 text files up to 8 MiB. Returns at most five hits per file; set start_line to continue after the last returned line.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}, "start_line": {"type": "integer"}}, "required": ["query"]}},
    {"name": "read_file", "description": "Read a UTF-8 file up to 8 MiB (max 200 lines per call). Set start_line to read a later page; end_line is optional.",
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

SCHEMAS.extend([
    {"name": "propose_delete_file", "description": "Propose deleting a regular file after saving a persistent local recovery backup. Apply requires approval.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "propose_move_file", "description": "Propose moving or renaming one file inside the workspace. Never overwrites a destination. Apply requires approval.",
     "parameters": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}, "required": ["source", "destination"]}},
    {"name": "propose_copy_file", "description": "Propose copying one file without replacing existing files. Apply requires approval.",
     "parameters": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}, "required": ["source", "destination"]}},
    {"name": "propose_directory", "description": "Propose creating a directory including parents, or removing an empty directory. No recursive deletion. Apply requires approval.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "operation": {"type": "string", "enum": ["create", "remove_empty"]}}, "required": ["path", "operation"]}},
    {"name": "list_file_backups", "description": "List up to 100 persistent file recovery backups for this workspace, without contents.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "propose_restore_file", "description": "Propose restoring a recovery backup to its original path or an optional different workspace path. Never overwrites. Apply requires approval.",
     "parameters": {"type": "object", "properties": {"backup_id": {"type": "string"}, "path": {"type": "string"}}, "required": ["backup_id"]}},
])

def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

class ToolBox:
    def __init__(self, root, test_commands=None, test_timeout=180, test_env=None, recycle_dir=None):
        self.ws = Workspace(root, max_file_bytes=MAX_FILE_BYTES)
        self.flow = Workflow(root, max_file_bytes=MAX_FILE_BYTES)
        self.files = FileOperations(root, max_file_bytes=MAX_FILE_BYTES, recycle_dir=recycle_dir)
        self.test_commands = dict(test_commands or {})
        self.test_timeout = test_timeout
        self.test_env = dict(test_env or {})
        self.proposals = {}
        self._lock = threading.Lock()

    def scrub(self, text):
        """Remove the workspace path, home directory and OS user name from text that leaves this process.
        Handles plain, forward-slash and escaped-backslash (repr) spellings, case-insensitively."""
        if not isinstance(text, str):
            return text
        home = Path.home()
        for base, label in ((self.ws.root, "<workspace>"), (home, "~")):
            variants = {str(base), base.as_posix(), str(base).replace(BS, BS + BS)}
            pattern = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
            text = re.sub(pattern, lambda m, label=label: label, text, flags=re.IGNORECASE)
        try:
            user = getpass.getuser()
        except Exception:
            user = ""
        if len(user) >= 2:
            sep = "[" + re.escape(BS) + "/]+"
            text = re.sub("(" + sep + "(?:users|home)" + sep + ")" + re.escape(user) + "(?=" + sep + "|['\" ]|$)", lambda m: m.group(1) + "<user>", text, flags=re.IGNORECASE)
        return text

    def schemas(self):
        return [s for s in SCHEMAS if s["name"] != "run_tests" or self.test_commands]

    def describe_effect(self, name, args):
        """Return (summary, fingerprint) shown to the user before approval."""
        if name == "apply_proposal":
            p = self.proposals.get(args.get("proposal_id"))
            if not p:
                raise ValueError("unknown proposal")
            return p.get("summary", {"kind": "apply", "path": p["path"], "diff": p["diff"]}), p["fingerprint"]
        if name == "run_tests":
            argv = self.test_commands.get(args.get("name"))
            if not argv:
                raise ValueError("unknown test command")
            summary = {"kind": "run_tests", "name": args["name"], "argv": argv}
            return {**summary, "argv": [self.scrub(a) for a in argv]}, _digest(summary)
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

    def t_read_file(self, path, start_line=1, end_line=None):
        if type(start_line) is not int or start_line < 1:
            raise ValueError("invalid start_line")
        if end_line is None:
            end_line = start_line + 199
        result = self.ws.read_file(path, start_line, min(end_line, start_line + 199))
        if sum(len(line) for line in result["lines"]) > 32000:
            raise ValueError("requested lines exceed 32000 characters; request a smaller range")
        return result

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
            if glob_match(rel, f.name, pattern):
                if len(hits) >= 100:
                    truncated = True
                    break
                hits.append(rel)
        return {"files": hits, "truncated": truncated}

    def t_search_text(self, query, path=".", glob=None, start_line=1):
        if not isinstance(query, str) or not query or len(query) > 200:
            raise ValueError("invalid query")
        if type(start_line) is not int or start_line < 1:
            raise ValueError("invalid start_line")
        needle, matches, truncated, extra = query.lower(), [], False, {}
        skipped_large = 0
        for f in self._files(path):
            rel = f.relative_to(self.ws.root).as_posix()
            if glob and not glob_match(rel, f.name, glob):
                continue
            try:
                if f.stat().st_size > MAX_FILE_BYTES:
                    skipped_large += 1
                    continue
                data = f.read_bytes()
                if len(data) > MAX_FILE_BYTES:
                    skipped_large += 1
                    continue
            except OSError:
                continue
            if bytes([0]) in data[:4096]:
                continue
            in_file = 0
            for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if n < start_line or needle not in line.lower():
                    continue
                in_file += 1
                if in_file > MAX_PER_FILE:  # a noisy file must not crowd out other files
                    extra[rel] = extra.get(rel, 0) + 1
                elif len(matches) >= MAX_MATCHES:
                    return {"matches": matches, "truncated": True, "more_in_file": extra, "skipped_large_files": skipped_large}
                else:
                    matches.append({"path": rel, "line": n, "text": line.strip()[:200]})
        return {"matches": matches, "truncated": truncated, "more_in_file": extra, "skipped_large_files": skipped_large}

    # --- proposals (no writes) ---
    def _store(self, path, kind, old, new, before, after, sha):
        diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{path}", f"b/{path}"))
        if len(diff) > MAX_DIFF:
            raise ValueError("proposal diff too large; split into smaller edits")
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
        if len(old) > 20000 or len(new) > 20000:
            raise ValueError("invalid edit size; split into edits of at most 20000 characters")
        if not old or text.count(old) != 1:
            raise ValueError("old text must occur exactly once")
        after = text.replace(old, new, 1)
        if len(after.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("updated file too large")
        return self._store(path, "edit", old, new, text, after, info["sha256"])

    def t_propose_new_file(self, path, content):
        target = self.files.resolve(path)
        if target.exists() or target.is_symlink():
            raise ValueError("file already exists")
        if len(content.encode("utf-8")) > 1048576:
            raise ValueError("content too large")
        return self._store(path, "create", "", content, "", content, "")

    def _store_operation(self, plan):
        summary = {"kind": plan["operation"], "path": plan["path"]}
        for field in ("destination", "size", "sha256", "backup_id"):
            if field in plan:
                summary[field] = plan[field]
        if plan["operation"] == "delete_file":
            summary["recovery_backup"] = True
        pid = uuid.uuid4().hex[:12]
        with self._lock:
            self.proposals[pid] = {"kind": "file_operation", "path": plan["path"], "diff": "",
                                   "operation_plan": plan, "summary": summary, "fingerprint": _digest(plan)}
        return {"proposal_id": pid, "summary": summary}

    def t_propose_delete_file(self, path):
        return self._store_operation(self.files.plan("delete_file", path))

    def t_propose_move_file(self, source, destination):
        return self._store_operation(self.files.plan("move_file", source, destination))

    def t_propose_copy_file(self, source, destination):
        return self._store_operation(self.files.plan("copy_file", source, destination))

    def t_propose_directory(self, path, operation):
        mapping = {"create": "create_directory", "remove_empty": "remove_empty_directory"}
        if operation not in mapping:
            raise ValueError("unknown directory operation")
        return self._store_operation(self.files.plan(mapping[operation], path))

    def t_list_file_backups(self):
        return self.files.list_backups()

    def t_propose_restore_file(self, backup_id, path=None):
        return self._store_operation(self.files.plan_restore(backup_id, path))

    # --- effects (called only after approval) ---
    def t_apply_proposal(self, proposal_id):
        p = self.proposals.pop(proposal_id, None)
        if p is None:
            raise ValueError("unknown or already used proposal")
        try:
            if p["kind"] == "file_operation":
                return self.files.apply(p["operation_plan"])
            if p["kind"] == "edit":
                r = self.flow.replace_text(p["path"], p["sha"], p["old"], p["new"])
                return {"status": "APPLIED", "path": p["path"], "sha256": r["sha256"]}
            target = self.files.resolve(p["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            self.files.resolve(p["path"])
            self.files._write_exclusive(target, p["new"].encode("utf-8"))
            return {"status": "APPLIED", "path": p["path"]}
        except Conflict as exc:
            return {"status": "CONFLICT", "detail": str(exc)}
        except FileExistsError:
            return {"status": "CONFLICT", "detail": "file already exists"}
        except (FileNotFoundError, NotADirectoryError):
            return {"status": "CONFLICT", "detail": "source or parent changed since proposal"}
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                return {"status": "CONFLICT", "detail": "directory is no longer empty"}
            raise

    def _env(self):
        env = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV or k.upper().startswith("LC_")}
        env.update(self.test_env)
        return env

    @staticmethod
    def _kill_tree(proc):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            proc.kill()

    def t_run_tests(self, name):
        argv = self.test_commands.get(name)
        if not argv:
            raise ValueError("unknown test command")
        shown = [self.scrub(a) for a in argv]
        kw = {"cwd": self.ws.root, "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "env": self._env()}
        kw.update({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True})
        try:
            proc = subprocess.Popen(argv, **kw)
        except OSError as exc:
            return {"status": "UNKNOWN", "argv": shown, "error": type(exc).__name__}
        try:
            out, _ = proc.communicate(timeout=self.test_timeout)
        except subprocess.TimeoutExpired:
            self._kill_tree(proc)  # the whole tree: a surviving grandchild would keep the pipe open and block us
            try:
                out, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out = b""
            return {"status": "TIMEOUT", "argv": shown, "output": self.scrub(_tail(out or b""))}
        return {"status": "PASS" if proc.returncode == 0 else "FAIL", "exit_code": proc.returncode, "argv": shown, "output": self.scrub(_tail(out))}

def _tail(data):
    return data.decode("utf-8", "replace")[-MAX_OUTPUT:]
