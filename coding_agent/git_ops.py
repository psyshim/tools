"""Safe, read-heavy Git operations for the coding agent.

Only standard-library imports.  No shell execution, no network,
no destructive commands (merge/rebase/reset/clean/push).
"""
import getpass
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from .file_ops import FileOperations

_INSPECT_ACTIONS = frozenset({"status", "diff", "staged_diff", "log", "branches"})
_MAX_OUTPUT = 512 * 1024          # 512 KiB per git command
_TIMEOUT = 30                     # seconds
_BS = "\\"


# ── scrubbing ────────────────────────────────────────────────────────

def _scrub(text: str, root: Path) -> str:
    """Strip absolute root / home / username from *text*."""
    if not isinstance(text, str):
        return str(text)
    home = Path.home()
    for base, label in ((root, "<workspace>"), (home, "~")):
        variants = {str(base), base.as_posix(), str(base).replace(_BS, _BS + _BS)}
        pattern = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
        text = re.sub(pattern, label, text, flags=re.IGNORECASE)
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    if len(user) >= 2:
        sep = "[" + re.escape(_BS) + "/]+"
        text = re.sub(
            "(" + sep + "(?:users|home)" + sep + ")" + re.escape(user)
            + "(?=" + sep + "|['\"\\ ]|$)",
            lambda m: m.group(1) + "<user>",
            text, flags=re.IGNORECASE,
        )
    return text


def _sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


# ── path validation ──────────────────────────────────────────────────

_PRIVATE_NAMES = frozenset({
    ".git", ".env", ".npmrc", ".pypirc", ".netrc",
    ".ssh", ".aws", ".config", "id_rsa", "id_ed25519",
    "credentials", "credentials.json",
})


def _validate_relpath(raw: str, root: Path) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValueError("invalid path")
    return FileOperations(root).resolve(raw)


# ── low-level runner ─────────────────────────────────────────────────

def _run(argv: list[str], cwd: Path, env_override: dict | None = None, input_data=None) -> str:
    """Run a git command as argv (no shell) with timeout and output cap."""
    if not argv or argv[0] != "git":
        raise RuntimeError("only git commands allowed")
    safe = {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LANGUAGE", "TZ"}
    env = {k: v for k, v in os.environ.items() if k.upper() in safe}
    env.update(env_override or {})
    # Disable fsmonitor, extensions, and advice noise
    env["GIT_OPTIONAL_LOCKS"] = "0"
    argv = ["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", "-c", "log.showSignature=false"] + argv[1:]
    env["GIT_LITERAL_PATHSPECS"] = "1"
    # --no-textconv alone does not suppress clean/process filters while Git
    # compares working files. Neutralize every configured external filter.
    needs_filters = any(arg in {"diff", "status", "hash-object", "add", "checkout", "reset", "restore", "read-tree"} for arg in argv[1:])
    if needs_filters:
        config = subprocess.run(["git", "config", "--null", "--name-only", "--get-regexp",
                                 r"^filter\..*\.(clean|smudge|process|required)$"],
                                cwd=str(cwd), env=env, capture_output=True, timeout=_TIMEOUT, shell=False)
        if config.returncode not in (0, 1) or len(config.stdout) > _MAX_OUTPUT:
            raise RuntimeError("cannot determine safe Git filter configuration")
        filters = set()
        for raw_key in config.stdout.split(b"\x00"):
            if raw_key:
                key = raw_key.decode("utf-8", errors="strict")
                if key.startswith("filter."):
                    filters.add(key.rsplit(".", 1)[0])
        overrides = []
        for name in sorted(filters):
            for suffix, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")):
                overrides += ["-c", name + "." + suffix + "=" + value]
        argv[1:1] = overrides
    proc = subprocess.run(
        argv, input=input_data,
        cwd=str(cwd),
        capture_output=True,
        timeout=_TIMEOUT,
        env=env,
        shell=False,
    )
    stdout = proc.stdout[:_MAX_OUTPUT].decode("utf-8", errors="replace")
    stderr = proc.stderr[:_MAX_OUTPUT].decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"git exited {proc.returncode}: {stderr.strip()}")
    if len(proc.stdout) > _MAX_OUTPUT:
        raise RuntimeError("git output exceeds limit; narrow the path selection")
    return stdout


# ── commit-specific runner with isolated config ──────────────────────

def _run_commit(argv: list[str], cwd: Path, env_override=None, input_data=None) -> str:
    """Run a git command with hooks/gpgsign disabled via env overrides."""
    env_extra = {
        "GIT_COMMITTER_NAME": "code-attach",
        "GIT_COMMITTER_EMAIL": "code-attach@localhost",
        "GIT_AUTHOR_NAME": "code-attach",
        "GIT_AUTHOR_EMAIL": "code-attach@localhost",
    }
    env_extra.update(env_override or {})
    return _run(argv, cwd, env_override=env_extra, input_data=input_data)


# ── public class ─────────────────────────────────────────────────────

class GitOperations:
    """Safe git façade scoped to *root* (must be the repo working tree)."""

    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("root must be a directory")
        git_dir = self.root / ".git"
        if not git_dir.exists():
            raise ValueError("not a git repository")
        top = Path(_run(["git", "rev-parse", "--show-toplevel"], self.root).strip()).resolve()
        if top != self.root:
            raise ValueError("workspace must equal repository root")

    def _index_path(self):
        path = Path(_run(["git", "rev-parse", "--git-path", "index"], self.root).strip())
        return (self.root / path).resolve() if not path.is_absolute() else path.resolve()

    def _index_hash(self):
        path = self._index_path()
        return _sha(path.read_bytes()) if path.exists() else None

    def _busy_operation(self):
        directory = self._index_path().parent
        return any((directory / name).exists() for name in (
            "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"))

    # ── inspect ───────────────────────────────────────────────────

    def inspect(self, action: str = "status", path: str | None = None) -> dict:
        if action not in _INSPECT_ACTIONS:
            raise ValueError(f"unsupported action: {action}")

        if path is not None:
            _validate_relpath(path, self.root)

        base_flags = [
            "-c", "core.fsmonitor=",
            "-c", "diff.noprefix=false",
        ]

        if action == "status":
            argv = ["git"] + base_flags + ["status", "--porcelain=v1", "--ignore-submodules=all", "-u"]
            if path:
                argv += ["--", path]
            raw = _run(argv, self.root)
            entries = [line for line in raw.splitlines() if line.strip()]
            return {"action": "status", "entries": [_scrub(e, self.root) for e in entries]}

        if action == "diff":
            argv = ["git"] + base_flags + [
                "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--ignore-submodules=all",
            ]
            if path:
                argv += ["--", path]
            raw = _run(argv, self.root)
            return {"action": "diff", "diff": _scrub(raw, self.root)}

        if action == "staged_diff":
            argv = ["git"] + base_flags + [
                "diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-color", "--ignore-submodules=all",
            ]
            if path:
                argv += ["--", path]
            raw = _run(argv, self.root)
            return {"action": "staged_diff", "diff": _scrub(raw, self.root)}

        if action == "log":
            argv = ["git"] + base_flags + [
                "log", "--oneline", "-n", "20", "--no-decorate",
            ]
            if path:
                argv += ["--", path]
            raw = _run(argv, self.root)
            lines = [l for l in raw.splitlines() if l.strip()]
            return {"action": "log", "entries": [_scrub(l, self.root) for l in lines]}

        # branches
        argv = ["git"] + base_flags + ["branch", "--list", "--no-color"]
        raw = _run(argv, self.root)
        branches = [l.strip().lstrip("* ") for l in raw.splitlines() if l.strip()]
        current_argv = ["git"] + base_flags + ["rev-parse", "--abbrev-ref", "HEAD"]
        current = _run(current_argv, self.root).strip()
        return {"action": "branches", "current": current, "branches": branches}

    # ── plan_commit ───────────────────────────────────────────────

    def plan_commit(self, paths: list[str], message: str) -> dict:
        if self._busy_operation():
            raise ValueError("finish or abort the existing Git operation first")
        if not isinstance(paths, list) or not paths:
            raise ValueError("paths must be a non-empty list")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        if len(message) > 4096 or len(paths) > 100 or "\x00" in message:
            raise ValueError("commit request exceeds limits")

        # Reject if anything is already staged
        staged_raw = _run(
            ["git", "-c", "core.fsmonitor=", "diff", "--cached", "--name-only"],
            self.root,
        )
        if staged_raw.strip():
            raise RuntimeError(
                "existing staged changes detected — refusing to avoid "
                "committing someone else's staged work"
            )

        # Validate and snapshot each path
        head = _run(
            ["git", "-c", "core.fsmonitor=", "rev-parse", "HEAD"],
            self.root,
        ).strip()
        branch = _run(
            ["git", "-c", "core.fsmonitor=", "rev-parse", "--abbrev-ref", "HEAD"],
            self.root,
        ).strip()
        if branch == "HEAD":
            raise ValueError("detached HEAD commits are unsupported")
        index_hash = self._index_hash()

        file_snapshots = {}
        file_modes = {}
        file_blobs = {}
        for raw_path in paths:
            resolved = _validate_relpath(raw_path, self.root)
            rel = resolved.relative_to(self.root).as_posix()
            if resolved.is_file():
                blob = resolved.read_bytes()
                if len(blob) > 8 * 1024 * 1024:
                    raise ValueError("file too large")
                file_snapshots[rel] = _sha(blob)
                file_modes[rel] = "100755" if resolved.stat().st_mode & 0o111 else "100644"
                file_blobs[rel] = _run(["git", "hash-object", "--stdin", "--path=" + rel], self.root, input_data=blob).strip()
            else:
                if resolved.exists():
                    raise ValueError("commit paths must be individual files")
                _run(["git", "ls-files", "--error-unmatch", "--", rel], self.root)
                file_snapshots[rel] = None

        # Capture diff for proposed paths
        diff_argv = [
            "git", "-c", "core.fsmonitor=",
            "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--ignore-submodules=all",
            "--", *[p for p in file_snapshots],
        ]
        diff_text = _run(diff_argv, self.root)

        # Also capture untracked content for new files
        for rel, sha in file_snapshots.items():
            full = self.root / rel
            if sha is not None:
                # Check if it's untracked (not in diff)
                ls_argv = ["git", "-c", "core.fsmonitor=", "ls-files", "--error-unmatch", "--", rel]
                try:
                    _run(ls_argv, self.root)
                except RuntimeError:
                    # Untracked new file — note it
                    diff_text += f"\n(new untracked file: {rel})\n"
                    try:
                        preview = full.read_bytes().decode("utf-8", errors="strict")
                        diff_text += "".join("+" + line + "\n" for line in preview.splitlines())
                    except UnicodeError:
                        diff_text += f"binary or legacy encoding; sha256={sha}\n"
        if len(diff_text) > 64000:
            raise ValueError("commit diff too large; select fewer changes")

        plan = {
            "type": "commit_plan",
            "head": head,
            "branch": branch,
            "message": message.strip(),
            "paths": list(file_snapshots.keys()),
            "file_hashes": file_snapshots,
            "file_modes": file_modes,
            "file_blobs": file_blobs,
            "diff": _scrub(diff_text, self.root),
            "index_hash": index_hash,
            "policy": "worktree bytes preserved; built-in Git EOL/encoding conversions pinned; external filters, hooks and signing disabled; local commit; author code-attach <code-attach@localhost>",
        }
        return plan

    # ── apply ─────────────────────────────────────────────────────

    def apply(self, plan: dict) -> dict:
        if not isinstance(plan, dict) or plan.get("type") != "commit_plan":
            raise ValueError("invalid plan")
        required = {"paths", "file_hashes", "file_modes", "file_blobs", "head", "branch", "message", "index_hash"}
        if not required <= plan.keys():
            raise ValueError("incomplete commit plan")
        index = self._index_path()
        lock = Path(str(index) + ".lock")
        descriptor = None
        published = None
        keep_lock = False
        owned_lock = False
        publication_started = False

        def verify():
            if self._busy_operation():
                return "another Git operation is in progress"
            head = _run(["git", "rev-parse", "HEAD"], self.root).strip()
            if head != plan["head"]:
                return "HEAD changed since plan was created"
            branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], self.root).strip()
            if branch != plan["branch"]:
                return "branch changed since plan was created"
            if self._index_hash() != plan["index_hash"]:
                return "index or staged changes appeared since proposal"
            for rel, expected in plan["file_hashes"].items():
                full = _validate_relpath(rel, self.root)
                if expected is None:
                    if full.exists():
                        return "deleted file reappeared since proposal"
                elif not full.is_file() or _sha(full.read_bytes()) != expected:
                    return "file changed since proposal: " + rel
                elif ("100755" if full.stat().st_mode & 0o111 else "100644") != plan["file_modes"][rel]:
                    return "file mode changed since proposal: " + rel
            return None

        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            owned_lock = True
            conflict = verify()
            if conflict:
                return {"status": "CONFLICT", "reason": conflict}
            with tempfile.TemporaryDirectory(prefix="code-attach-git-") as temp:
                temp_index = Path(temp) / "index"
                if index.exists():
                    temp_index.write_bytes(index.read_bytes())
                override = {"GIT_INDEX_FILE": str(temp_index)}
                for rel, expected in plan["file_hashes"].items():
                    if expected is None:
                        _run(["git", "update-index", "--force-remove", "--", rel], self.root, override)
                    else:
                        full = _validate_relpath(rel, self.root)
                        raw = full.read_bytes()
                        if _sha(raw) != expected:
                            return {"status": "CONFLICT", "reason": "file changed during staging"}
                        blob = _run(["git", "hash-object", "-w", "--stdin", "--path=" + rel], self.root, override, raw).strip()
                        if blob != plan["file_blobs"][rel]:
                            return {"status": "CONFLICT", "reason": "Git attributes or encoding conversion changed since proposal"}
                        mode = plan["file_modes"][rel]
                        _run(["git", "update-index", "--add", "--cacheinfo", mode, blob, rel], self.root, override)
                tree = _run(["git", "write-tree"], self.root, override).strip()
                old_tree = _run(["git", "rev-parse", plan["head"] + "^{tree}"], self.root).strip()
                if tree == old_tree:
                    return {"status": "FAILED", "reason": "no selected changes to commit"}
                oid = _run_commit(["git", "commit-tree", tree, "-p", plan["head"], "--no-gpg-sign", "-F", "-"],
                                  self.root, override, (plan["message"] + "\n").encode("utf-8")).strip()
                conflict = verify()
                if conflict:
                    return {"status": "CONFLICT", "reason": conflict}
                # Prepare the index under Git's standard exclusive lock before
                # publishing the commit. No other staged work is touched.
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = None
                    stream.write(temp_index.read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                # Compare-and-swap protects against a concurrent branch writer.
                publication_started = True
                _run(["git", "update-ref", "-m", "code-attach approved commit", "refs/heads/" + plan["branch"], oid, plan["head"]], self.root)
                published = oid
                try:
                    os.replace(lock, index)
                except OSError:
                    keep_lock = True
                    return {"status": "UNKNOWN", "commit": oid, "reason": "commit published but index publication failed; inspect index.lock"}
                return {"status": "APPLIED", "commit": oid}
        except FileExistsError:
            return {"status": "CONFLICT", "reason": "Git index is locked by another writer"}
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            keep_lock = publication_started
            return {"status": "UNKNOWN" if publication_started else "FAILED", "reason": _scrub(str(exc), self.root),
                    **({"commit": oid} if publication_started else {})}
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if owned_lock and not keep_lock:
                lock.unlink(missing_ok=True)
