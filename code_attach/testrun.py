"""Runs the host-configured test commands: filtered environment, cancel support, process-tree kill, bounded output."""
import os
import signal
import subprocess
import time
import threading

MAX_OUTPUT = 8000
# Environment handed to approved test commands: an allowlist, so host secrets (API keys, tokens) never reach project code.
SAFE_ENV = {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "LANG", "LANGUAGE", "TZ", "CI",
            "PYTHONIOENCODING", "PYTHONUTF8", "VIRTUAL_ENV", "JAVA_HOME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"}

def _tail(data):
    return data.decode("utf-8", "replace")[-MAX_OUTPUT:]

class CommandRunner:
    def __init__(self, cwd, commands, timeout, extra_env, scrub):
        self.cwd, self.commands, self.timeout, self.extra_env, self.scrub = cwd, commands, timeout, dict(extra_env or {}), scrub
        self._cancel = threading.Event()

    def request_cancel(self):
        self._cancel.set()

    def _env(self):
        env = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV or k.upper().startswith("LC_")}
        env.update(self.extra_env)
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

    def run(self, name):
        argv = self.commands.get(name)
        if not argv:
            raise ValueError("unknown test command")
        shown = [self.scrub(a) for a in argv]
        if self._cancel.is_set():
            return {"status": "CANCELLED", "argv": shown, "output": ""}
        kw = {"cwd": self.cwd, "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "env": self._env()}
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
                        if len(tail) > MAX_OUTPUT:
                            del tail[:-MAX_OUTPUT]
            except OSError as exc:
                reader_errors.append(type(exc).__name__)
            finally:
                drained.set()
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + self.timeout
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
            return {"status": timed_status, "argv": shown, "output": self.scrub(_tail(out))}
        if reader_errors:
            return {"status": "UNKNOWN", "argv": shown, "error": reader_errors[0]}
        return {"status": "PASS" if proc.returncode == 0 else "FAIL", "exit_code": proc.returncode, "argv": shown, "output": self.scrub(_tail(out))}
