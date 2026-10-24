"""Tools the model may call. Side effects (apply, run_tests) are gated by approval in session.py.

ToolBox composes: this core (reading, searching, single-edit proposals, file operations, Git, applying, test runs),
NavTools (outline / symbols / project overview) and EditTools (line ranges, batches, notebooks, undo).
"""
import collections
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

from .globs import SKIP_DIRS, glob_match  # noqa: F401  (re-exported)
from .matching import adapt_eol, locate
from .outline import word_pattern
from .privacy import Scrubber
from .schemas import EFFECT_TOOLS, SCHEMAS
from .testrun import CommandRunner, normalize_commands
from .tools_edit import EditTools
from .tools_nav import NavTools

GRANTS = frozenset({"apply", "git_commit", "test"})
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_DIFF = 64000
MAX_LINE_CHARS = 2000
MAX_FILES_SCANNED = 20000
MAX_MATCHES = 50
MAX_PER_FILE = 5
MAX_TERMS = 5

def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

def _excerpt(line, at):
    """Up to 200 characters of a (possibly enormous) line around column `at`."""
    stripped = line.strip()
    if len(stripped) <= 200:
        return stripped
    start = max(0, at - 80)
    return ("..." if start else "") + line[start:start + 200].strip() + "..."

class ToolBox(NavTools, EditTools):
    """Everything the model can ask for, bound to one workspace. Proposals are created here; effects happen only in
    t_apply_proposal / t_run_tests, which Task calls after an approval. See docs/MAINTAINING_CODE_ATTACH.md."""
    def __init__(self, root, test_commands=None, test_timeout=180, test_env=None, recycle_dir=None, allow_mutation=True):
        self.ws = Workspace(root, max_file_bytes=MAX_FILE_BYTES)
        self.flow = Workflow(root, max_file_bytes=MAX_FILE_BYTES)
        self.files = FileOperations(root, max_file_bytes=MAX_FILE_BYTES, recycle_dir=recycle_dir)
        self._git = None
        # Capability grants for effects. True = all of apply / git_commit / test (the prototype default); False = read/propose
        # only; an iterable names exactly the grants (a device grants them per workspace and per kind of effect).
        if allow_mutation is True:
            self.grants = frozenset(GRANTS)
        elif not allow_mutation:
            self.grants = frozenset()
        else:
            self.grants = frozenset(allow_mutation)
            if not self.grants <= GRANTS:
                raise ValueError("unknown capability grant: " + ", ".join(sorted(self.grants - GRANTS)))
        self.allow_mutation = bool(self.grants)
        self.command_specs = normalize_commands(test_commands)
        self.test_commands = {name: spec["argv"] for name, spec in self.command_specs.items()}  # name -> argv view
        self.test_timeout = test_timeout
        self.test_env = dict(test_env or {})
        self._scrubber = Scrubber(self.ws.root)
        self.runner = CommandRunner(self.ws.root, self.command_specs, test_timeout, self.test_env, self.scrub)
        self.proposals = {}
        self.applied = collections.OrderedDict()   # undo history (EditTools)
        self._appliers = {"write": self._apply_write, "write_batch": self._apply_batch}
        self._lock = threading.Lock()

    def scrub(self, text):
        """See privacy.Scrubber: nothing identifying the person or machine may leave the process."""
        return self._scrubber.scrub(text)

    def is_effect(self, name):
        """True for tools whose execution must wait for an approval (the loop asks the toolbox; it does not know tool names)."""
        return name in EFFECT_TOOLS

    def request_cancel(self):
        """Stop a running test command promptly (called by Task.cancel)."""
        self.runner.request_cancel()

    def schemas(self):
        out = []
        for schema in SCHEMAS:
            if not self._tool_granted(schema["name"]):
                continue
            if schema["name"] == "run_tests":
                if not self.command_specs:
                    continue
                schema = json.loads(json.dumps(schema))  # per-workspace copy: the model sees the commands that really exist
                schema["parameters"]["properties"]["name"]["enum"] = list(self.command_specs)
                listing = "; ".join(f"{n}: {s['description']}" if s["description"] else n for n, s in self.command_specs.items())
                schema["description"] += " Available: " + listing
            out.append(schema)
        return out

    def describe_effect(self, name, args):
        """Return (summary, fingerprint) shown to the user before approval."""
        self._require_mutation(name)
        if name == "apply_proposal":
            p = self.proposals.get(args.get("proposal_id"))
            if not p:
                raise ValueError("unknown proposal")
            return p.get("summary", {"kind": "apply", "path": p["path"], "diff": p["diff"], "encoding": p.get("encoding", "utf-8")}), p["fingerprint"]
        if name == "run_tests":
            spec = self.command_specs.get(args.get("name"))
            if not spec:
                raise ValueError("unknown test command; available: " + ", ".join(self.command_specs))
            summary = {"kind": "run_tests", "name": args["name"], "argv": spec["argv"]}
            for key in ("cwd", "timeout"):
                if spec.get(key):
                    summary[key] = spec[key]
            shown = {**summary, "argv": [self.scrub(a) for a in spec["argv"]]}
            return shown, _digest({**summary, "env_keys": sorted(spec["env"])})
        raise ValueError("not an effect tool")

    def call(self, name, args, call_id=None):   # call_id: the loop's id for this call; remote toolboxes derive idempotent op ids from it
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        fn = getattr(self, "t_" + name, None)
        if fn is None or name not in {s["name"] for s in SCHEMAS}:
            raise ValueError("unknown tool")
        self._require_mutation(name)
        try:
            result = fn(**args)
        except ValueError as exc:
            raise type(exc)(str(exc) + self._path_hint(args)) from None
        if not self.allow_mutation and name.startswith("propose_") and isinstance(result, dict):
            result["applied"] = False   # a proposal is a patch artifact here; this installation cannot apply it
            result["note"] = "read/propose mode: this is a proposal only and nothing was or can be changed by this assistant"
        if name in {"find_files", "search_text"} and isinstance(result, dict):
            result["scan_truncated"] = bool(getattr(self.ws, "scan_truncated", False))  # the walk hit its file limit
        return result

    def _tool_granted(self, name):
        """Whether the grants allow this tool to exist at all. Read and propose tools always do."""
        if name == "run_tests":
            return "test" in self.grants
        if name == "apply_proposal":
            return bool(self.grants & {"apply", "git_commit"})
        if name == "propose_undo":
            return "apply" in self.grants
        if name == "propose_git_commit":
            return "git_commit" in self.grants
        return True

    def _require_mutation(self, name):
        if not self._tool_granted(name):
            raise ValueError("this installation is in read/propose mode for that action: it is not granted here; present the proposal to the user instead")

    def _path_hint(self, args):
        """For a path that does not exist, suggest similar names: typos and wrong casing are the commonest tool failure."""
        path = args.get("path")
        if not isinstance(path, str) or not path or len(path) > 400:
            return ""
        try:
            target = self.ws.resolve(path)
            if target.exists():
                return ""
            parent = target.parent
            while not parent.exists() and parent != self.ws.root:
                parent = parent.parent
            names = [p.name for p in parent.iterdir() if not p.is_symlink()][:2000]
            close = difflib.get_close_matches(target.name, names, n=3, cutoff=0.5) or [n for n in names if n.lower() == target.name.lower()]
            rel = parent.relative_to(self.ws.root).as_posix()
            where = "" if rel == "." else rel + "/"
            return f"; '{path}' does not exist" + (f". Similar: {', '.join(where + c for c in close)}" if close else f". Nearest existing folder: {rel}")
        except (ValueError, OSError):
            return ""

    # --- read-only ---
    def t_list_dir(self, path=".", depth=1):
        return {"entries": self.ws.list_directory(path, depth)}

    def t_read_file(self, path, start_line=1, end_line=None, encoding=None, column=None, width=None):
        if type(start_line) is not int or start_line < 1:
            raise ValueError("invalid start_line")
        if end_line is None:
            end_line = start_line + 199
        if column is not None:   # slice one very long line (minified code, data dumps)
            if type(column) is not int or column < 1 or (width is not None and (type(width) is not int or not 1 <= width <= 32000)):
                raise ValueError("invalid column/width")
            result = self.ws.read_file(path, start_line, start_line, encoding=encoding)
            line = result["lines"][0] if result["lines"] else ""
            result.update({"lines": [line[column - 1:column - 1 + (width or MAX_LINE_CHARS)]], "line_length": len(line), "column": column})
            return result
        result = self.ws.read_file(path, start_line, min(end_line, start_line + 199), encoding=encoding)
        long_lines = [{"line": start_line + i, "length": len(l)} for i, l in enumerate(result["lines"]) if len(l) > MAX_LINE_CHARS]
        if long_lines:
            result["lines"] = [l if len(l) <= MAX_LINE_CHARS else l[:MAX_LINE_CHARS] + " ...[line truncated: read it with column/width]" for l in result["lines"]]
            result["truncated_lines"] = long_lines
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

    def _matcher(self, query, terms, whole_word):
        """line -> column (1-based) of the first hit or 0. Built from escaped text only: no user regex, no backtracking blowups."""
        words = [query] + ([t for t in terms] if terms is not None else [])
        if len(words) > MAX_TERMS or not all(isinstance(w, str) and 0 < len(w) <= 200 for w in words):
            raise ValueError(f"query and terms must be non-empty strings of at most 200 characters, at most {MAX_TERMS} in total")
        if whole_word:
            patterns = [word_pattern(w) for w in words]
            def find(line):
                hits = [m.start() + 1 for m in (p.search(line) for p in patterns) if m]
                return min(hits) if hits else 0
        else:
            lowered = [w.lower() for w in words]
            def find(line):
                low = line.lower()
                hits = [low.find(w) + 1 for w in lowered if w in low]
                return min(hits) if hits else 0
        return find

    def t_search_text(self, query, path=".", glob=None, start_line=1, encoding=None, whole_word=False, terms=None):
        if not isinstance(query, str) or not query or len(query) > 200:
            raise ValueError("invalid query")
        if type(start_line) is not int or start_line < 1:
            raise ValueError("invalid start_line")
        if terms is not None and not isinstance(terms, list):
            raise ValueError("terms must be a list of alternative strings")
        if encoding is not None:
            normalize(encoding)
        find = self._matcher(query, terms, bool(whole_word))
        matches, truncated, extra = [], False, {}
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
                column = find(line) if n >= start_line else 0
                if not column:
                    continue
                in_file += 1
                if in_file > MAX_PER_FILE:  # a noisy file must not crowd out other files
                    extra[rel] = extra.get(rel, 0) + 1
                elif len(matches) >= MAX_MATCHES:
                    return {"matches": matches, "truncated": True, "more_in_file": extra, "skipped_large_files": skipped_large, "skipped_encoding_files": skipped_encoding}
                else:
                    matches.append({"path": rel, "line": n, "column": column, "text": _excerpt(line, column)})
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
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError("old and new must be strings")
        info = self.flow.inspect(path)
        text, codec, prefix = decode_source(self.ws.resolve(path).read_bytes(), encoding)
        if len(old) > 20000 or len(new) > 20000:
            raise ValueError("invalid edit size; split into edits of at most 20000 characters")
        start, end = locate(text, old)   # tolerant to line endings; otherwise fails with the actual text and line numbers
        actual, replacement = text[start:end], adapt_eol(text, start, end, new)
        if len(actual) > 20000 or len(replacement) > 20000:   # measured after line-ending conversion: this is what apply will see
            raise ValueError("invalid edit size; split into edits of at most 20000 characters")
        after = text[:start] + replacement + text[end:]
        if len(encode_source(after, codec, prefix)) > MAX_FILE_BYTES:
            raise ValueError("updated file too large")
        return self._store(path, "edit", actual, replacement, text, after, info["sha256"], encoding=codec)

    def t_propose_new_file(self, path, content, encoding="utf-8"):
        if not isinstance(content, str) or not isinstance(encoding, str):
            raise ValueError("content and encoding must be strings")
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
        self._require_mutation("apply_proposal")
        peeked = self.proposals.get(proposal_id)
        if peeked is not None and ("git_commit" if peeked["kind"] == "git_commit" else "apply") not in self.grants:
            raise ValueError("this kind of change is not granted here (apply and git_commit are separate grants)")
        p = self.proposals.pop(proposal_id, None)
        if p is None:
            raise ValueError("unknown or already used proposal")
        try:
            if p["kind"] in self._appliers:
                return self._appliers[p["kind"]](p)
            if p["kind"] == "git_commit":
                return self._git_tools().apply(p["git_plan"])
            if p["kind"] == "file_operation":
                return self.files.apply(p["operation_plan"])
            if p["kind"] == "edit":
                before = self.ws.resolve(p["path"]).read_bytes()
                r = self.flow.replace_text(p["path"], p["sha"], p["old"], p["new"], encoding=p.get("encoding"))
                return {"status": "APPLIED", "path": p["path"], "sha256": r["sha256"],
                        "applied_id": self._remember([{"op": "write", "path": p["path"], "before": before, "after_sha": r["sha256"]}])}
            target = self.files.resolve(p["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            self.files.resolve(p["path"])
            data = encode_new_source(p["new"], p.get("encoding", "utf-8"))
            self.files._write_exclusive(target, data)
            return {"status": "APPLIED", "path": p["path"],
                    "applied_id": self._remember([{"op": "create", "path": p["path"], "before": None, "after_sha": hashlib.sha256(data).hexdigest()}])}
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
        self._require_mutation("run_tests")
        return self.runner.run(name)
