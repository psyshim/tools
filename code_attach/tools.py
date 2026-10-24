"""Tools the model may call. Side effects (apply, run_tests) are gated by approval in session.py."""
import difflib
import errno
import hashlib
import json
import threading
import uuid
from coding_agent.workflow import Workflow, Conflict
from coding_agent.workspace import Workspace
from coding_agent.file_ops import FileOperations
from coding_agent.git_ops import GitOperations
from coding_agent.text_encoding import decode_source, encode_source, encode_new_source, normalize

from .globs import glob_match  # noqa: F401  (re-exported)
from .privacy import Scrubber
from .schemas import SCHEMAS
from .testrun import CommandRunner

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_DIFF = 64000
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".tox", "dist", "build", ".idea"}
MAX_FILES_SCANNED = 20000
MAX_MATCHES = 50
MAX_PER_FILE = 5
CR, LF = chr(13), chr(10)
BS = chr(92)
CRLF = CR + LF

def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

class ToolBox:
    """Everything the model can ask for, bound to one workspace. Proposals are created here; effects happen only in
    t_apply_proposal / t_run_tests, which Task calls after an approval. See docs/MAINTAINING_CODE_ATTACH.md."""
    def __init__(self, root, test_commands=None, test_timeout=180, test_env=None, recycle_dir=None):
        self.ws = Workspace(root, max_file_bytes=MAX_FILE_BYTES)
        self.flow = Workflow(root, max_file_bytes=MAX_FILE_BYTES)
        self.files = FileOperations(root, max_file_bytes=MAX_FILE_BYTES, recycle_dir=recycle_dir)
        self._git = None
        self.test_commands = dict(test_commands or {})
        self.test_timeout = test_timeout
        self.test_env = dict(test_env or {})
        self._scrubber = Scrubber(self.ws.root)
        self.runner = CommandRunner(self.ws.root, self.test_commands, test_timeout, self.test_env, self.scrub)
        self.proposals = {}
        self._lock = threading.Lock()

    def scrub(self, text):
        """See privacy.Scrubber: nothing identifying the person or machine may leave the process."""
        return self._scrubber.scrub(text)

    def request_cancel(self):
        """Stop a running test command promptly (called by Task.cancel)."""
        self.runner.request_cancel()

    def schemas(self):
        return [s for s in SCHEMAS if s["name"] != "run_tests" or self.test_commands]

    def describe_effect(self, name, args):
        """Return (summary, fingerprint) shown to the user before approval."""
        if name == "apply_proposal":
            p = self.proposals.get(args.get("proposal_id"))
            if not p:
                raise ValueError("unknown proposal")
            return p.get("summary", {"kind": "apply", "path": p["path"], "diff": p["diff"], "encoding": p.get("encoding", "utf-8")}), p["fingerprint"]
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
        result = fn(**args)
        if name in {"find_files", "search_text"} and isinstance(result, dict):
            result["scan_truncated"] = bool(getattr(self.ws, "scan_truncated", False))  # the walk hit its file limit
        return result

    # --- read-only ---
    def t_list_dir(self, path=".", depth=1):
        return {"entries": self.ws.list_directory(path, depth)}

    def t_read_file(self, path, start_line=1, end_line=None, encoding=None):
        if type(start_line) is not int or start_line < 1:
            raise ValueError("invalid start_line")
        if end_line is None:
            end_line = start_line + 199
        result = self.ws.read_file(path, start_line, min(end_line, start_line + 199), encoding=encoding)
        if sum(len(line) for line in result["lines"]) > 32000:
            raise ValueError("requested lines exceed 32000 characters; request a smaller range")
        return result

    def _files(self, path):
        return self.ws.iter_files(path, SKIP_DIRS, MAX_FILES_SCANNED)

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

    def t_search_text(self, query, path=".", glob=None, start_line=1, encoding=None):
        if not isinstance(query, str) or not query or len(query) > 200:
            raise ValueError("invalid query")
        if type(start_line) is not int or start_line < 1:
            raise ValueError("invalid start_line")
        if encoding is not None:
            normalize(encoding)
        needle, matches, truncated, extra = query.lower(), [], False, {}
        skipped_large = 0
        skipped_encoding = 0
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
            try:
                text, _, _ = decode_source(data, encoding)
            except (UnicodeError, ValueError):
                skipped_encoding += 1
                continue
            in_file = 0
            for n, line in enumerate(text.splitlines(), 1):
                if n < start_line or needle not in line.lower():
                    continue
                in_file += 1
                if in_file > MAX_PER_FILE:  # a noisy file must not crowd out other files
                    extra[rel] = extra.get(rel, 0) + 1
                elif len(matches) >= MAX_MATCHES:
                    return {"matches": matches, "truncated": True, "more_in_file": extra, "skipped_large_files": skipped_large, "skipped_encoding_files": skipped_encoding}
                else:
                    matches.append({"path": rel, "line": n, "text": line.strip()[:200]})
        return {"matches": matches, "truncated": truncated, "more_in_file": extra, "skipped_large_files": skipped_large, "skipped_encoding_files": skipped_encoding}

    # --- proposals (no writes) ---
    def _store(self, path, kind, old, new, before, after, sha, encoding=None):
        diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{path}", f"b/{path}"))
        if len(diff) > MAX_DIFF:
            raise ValueError("proposal diff too large; split into smaller edits")
        pid = uuid.uuid4().hex[:12]
        body = {"path": path, "kind": kind, "old": old, "new": new, "sha": sha}
        if encoding is not None:
            body["encoding"] = encoding
        with self._lock:
            self.proposals[pid] = {**body, "diff": diff, "fingerprint": _digest(body)}
        return {"proposal_id": pid, "diff": diff}

    def t_propose_edit(self, path, old, new, encoding=None):
        info = self.flow.inspect(path)
        text, codec, prefix = decode_source(self.ws.resolve(path).read_bytes(), encoding)
        if CRLF in text and CR not in old:  # CRLF file: the model writes LF
            old, new = old.replace(LF, CRLF), new.replace(LF, CRLF)
        if len(old) > 20000 or len(new) > 20000:
            raise ValueError("invalid edit size; split into edits of at most 20000 characters")
        if not old or text.count(old) != 1:
            raise ValueError("old text must occur exactly once")
        after = text.replace(old, new, 1)
        if len(encode_source(after, codec, prefix)) > MAX_FILE_BYTES:
            raise ValueError("updated file too large")
        return self._store(path, "edit", old, new, text, after, info["sha256"], encoding=codec)

    def t_propose_new_file(self, path, content, encoding="utf-8"):
        target = self.files.resolve(path)
        if target.exists() or target.is_symlink():
            raise ValueError("file already exists")
        if len(encode_new_source(content, encoding)) > 1048576:
            raise ValueError("content too large")
        return self._store(path, "create", "", content, "", content, "", encoding=encoding)

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

    def _git_tools(self):
        if self._git is None:
            self._git = GitOperations(self.ws.root)
        return self._git

    def t_git_inspect(self, action="status", path=None):
        try:
            return self._git_tools().inspect(action, path)
        except RuntimeError as exc:
            raise ValueError(self.scrub(str(exc))) from None

    def t_propose_git_commit(self, paths, message):
        try:
            plan = self._git_tools().plan_commit(paths, message)
        except RuntimeError as exc:
            raise ValueError(self.scrub(str(exc))) from None
        summary = {"kind": "git_commit", **{key: plan[key] for key in ("paths", "message", "head", "branch", "diff", "policy")}}
        pid = uuid.uuid4().hex[:12]
        with self._lock:
            self.proposals[pid] = {"kind": "git_commit", "path": "<git>", "diff": plan["diff"],
                                   "git_plan": plan, "summary": summary, "fingerprint": _digest(plan)}
        return {"proposal_id": pid, "summary": summary}

    # --- effects (called only after approval) ---
    def t_apply_proposal(self, proposal_id):
        p = self.proposals.pop(proposal_id, None)
        if p is None:
            raise ValueError("unknown or already used proposal")
        try:
            if p["kind"] == "git_commit":
                return self._git_tools().apply(p["git_plan"])
            if p["kind"] == "file_operation":
                return self.files.apply(p["operation_plan"])
            if p["kind"] == "edit":
                r = self.flow.replace_text(p["path"], p["sha"], p["old"], p["new"], encoding=p.get("encoding"))
                return {"status": "APPLIED", "path": p["path"], "sha256": r["sha256"]}
            target = self.files.resolve(p["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            self.files.resolve(p["path"])
            self.files._write_exclusive(target, encode_new_source(p["new"], p.get("encoding", "utf-8")))
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
            self.proposals[proposal_id] = p  # transient failure (e.g. editor lock): the same proposal may be retried
            raise

    def t_run_tests(self, name):
        return self.runner.run(name)
