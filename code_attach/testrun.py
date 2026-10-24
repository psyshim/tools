"""Runs the host-configured commands (tests, linters, builds): filtered environment, per-command settings, cancel support,
process-tree kill, bounded output, and a structured reading of the output.

The host decides *what* can run (`test_commands`); the model only picks a name. A command is either an argv list or a dict
`{"argv": [...], "timeout": seconds, "cwd": "relative/subdir", "env": {"NAME": "value"}, "description": "text for the model"}`.
"""
import os
import signal
import subprocess
import time
import threading
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from .testparse import parse, verification_status

MAX_OUTPUT = 8000
PARSE_TAIL = 65536           # kept in memory for reading results; only MAX_OUTPUT characters are returned
MAX_TIMEOUT = 3600
# Environment handed to approved test commands: an allowlist, so host secrets (API keys, tokens) never reach project code.
SAFE_ENV = {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "LANG", "LANGUAGE", "TZ", "CI",
            "PYTHONIOENCODING", "PYTHONUTF8", "VIRTUAL_ENV", "JAVA_HOME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"}

def normalize_commands(commands):
    """Validate the host's command table once, at configuration time. Returns {name: {argv, timeout, cwd, env, description}}."""
    out = {}
    for name, value in (commands or {}).items():
        if not isinstance(name, str) or not 0 < len(name) <= 64:
            raise ValueError("command names must be non-empty strings of at most 64 characters")
        spec = {"argv": value} if isinstance(value, (list, tuple)) else dict(value) if isinstance(value, dict) else None
        if spec is None:
            raise ValueError(f"command {name!r}: expected an argv list or a dict")
        argv = spec.get("argv")
        if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise ValueError(f"command {name!r}: argv must be a non-empty list of non-empty strings")
        timeout = spec.get("timeout")
        if timeout is not None and (type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT):
            raise ValueError(f"command {name!r}: timeout must be an integer from 1 to {MAX_TIMEOUT}")
        cwd = spec.get("cwd")
        if cwd is not None and (not isinstance(cwd, str) or PurePosixPath(cwd).is_absolute() or PureWindowsPath(cwd).is_absolute() or PureWindowsPath(cwd).drive
                                or cwd.startswith(("/", "\\")) or ".." in re.split(r"[\\/]+", cwd)):
            raise ValueError(f"command {name!r}: cwd must be a relative path inside the workspace")
        env = dict(spec.get("env") or {})
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ValueError(f"command {name!r}: env must map strings to strings")
        out[name] = {"argv": [str(a) for a in argv], "timeout": timeout, "cwd": cwd, "env": env, "description": str(spec.get("description") or "")[:200]}
    return out

def _tail(data):
    return data.decode("utf-8", "replace")[-MAX_OUTPUT:]

class CommandRunner:
    def __init__(self, root, commands, timeout, extra_env, scrub):
        self.root, self.commands, self.timeout, self.extra_env, self.scrub = Path(root), normalize_commands(commands), timeout, dict(extra_env or {}), scrub
        self._cancel = threading.Event()

    def request_cancel(self):
        self._cancel.set()

    def _env(self, spec):
        env = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV or k.upper().startswith("LC_")}
        env.update(self.extra_env)
        env.update(spec["env"])
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

    def _cwd(self, spec):
        if not spec["cwd"]:
            return self.root
        cwd = (self.root / spec["cwd"]).resolve()
        if not cwd.is_relative_to(self.root.resolve()) or not cwd.is_dir():
            raise ValueError("configured cwd is not a directory inside the workspace")
        return cwd

    def _result(self, status, shown, raw, **extra):
        """Bounded scrubbed output plus a structured reading of it (failures with file:line, summary counts, diagnostics)."""
        text = self.scrub(raw.decode("utf-8", "replace").replace("\r\n", "\n"))
        result = {"status": status, "argv": shown, **extra, "output": text[-MAX_OUTPUT:]}
        parsed = parse(text)
        evidence = verification_status(parsed)
        result["verification"] = evidence
        if status == "PASS" and evidence in {"NO_TESTS", "ALL_SKIPPED", "REPORTED_FAILURES"}:
            result["status"] = "INCONSISTENT" if evidence == "REPORTED_FAILURES" else evidence
        if parsed["runner"] or parsed["failures"] or parsed["diagnostics"]:
            result["parsed"] = parsed
        return result

    def run(self, name):
        spec = self.commands.get(name)
        if not spec:
            raise ValueError("unknown command; available: " + ", ".join(self.commands))
        argv = spec["argv"]
        shown = [self.scrub(a) for a in argv]
        if self._cancel.is_set():
            return {"status": "CANCELLED", "argv": shown, "output": ""}
        try:
            cwd = self._cwd(spec)
        except (ValueError, OSError) as exc:
            return {"status": "UNKNOWN", "argv": shown, "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}
        kw = {"cwd": cwd, "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "env": self._env(spec)}
        kw.update({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True})
        try:
            proc = subprocess.Popen(argv, **kw)
        except OSError as exc:
            return {"status": "UNKNOWN", "argv": shown, "error": type(exc).__name__}
        # communicate() retains all output in parent memory even when its return
        # value is sliced later. Drain continuously and retain a bounded tail.
        tail, reader_errors = bytearray(), []
        tail_lock, drained = threading.Lock(), threading.Event()
        def read_output():
            try:
                while True:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    with tail_lock:
                        tail.extend(chunk)
                        if len(tail) > PARSE_TAIL:
                            del tail[:-PARSE_TAIL]
            except OSError as exc:
                reader_errors.append(type(exc).__name__)
            finally:
                drained.set()
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + (spec["timeout"] or self.timeout)
        timed_status = None
        while not (drained.is_set() and proc.poll() is not None):
            cancelled = self._cancel.is_set()
            if cancelled or time.monotonic() >= deadline:
                timed_status = "CANCELLED" if cancelled else "TIMEOUT"
                self._kill_tree(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                drained.wait(5)
                break
            time.sleep(.05)
        reader.join(timeout=.1)
        with tail_lock:
            out = bytes(tail)
        if drained.is_set():
            proc.stdout.close()
        if timed_status:
            return self._result(timed_status, shown, out)
        if reader_errors:
            return {"status": "UNKNOWN", "argv": shown, "error": reader_errors[0]}
        return self._result("PASS" if proc.returncode == 0 else "FAIL", shown, out, exit_code=proc.returncode)
