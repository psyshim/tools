"""Regression tests for defects found in the audit: each failed before its fix."""
import getpass
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from code_attach.engine import BadWorkspace, Busy, Engine
from code_attach.tools import ToolBox, glob_match

PY = sys.executable

def wait_terminal(engine, principal, tid):
    for _ in range(500):
        if engine.events(principal, tid)["terminal"]:
            return
        time.sleep(0.01)
    raise AssertionError("task did not finish")

class TaskCapacity(unittest.TestCase):
    def test_finished_tasks_do_not_consume_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(lambda m, t: {"text": "ok", "tool_calls": []}, max_tasks=2)
            for i in range(6):  # more than max_tasks over the engine's lifetime
                tid, _ = engine.start("u", tmp, f"k{i}", "x")
                wait_terminal(engine, "u", tid)

    def test_concurrently_active_tasks_are_still_capped(self):
        gate = threading.Event()
        def blocking(messages, tools):
            gate.wait(10)
            return {"text": "", "tool_calls": []}
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(blocking, max_tasks=2)
            ids = [engine.start("u", tmp, f"k{i}", "x")[0] for i in range(2)]
            with self.assertRaises(Busy):
                engine.start("u", tmp, "k3", "x")
            gate.set()
            for tid in ids:
                wait_terminal(engine, "u", tid)

    def test_finished_task_retention_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(lambda m, t: {"text": "ok", "tool_calls": []}, max_tasks=5, keep_finished=2)
            for i in range(6):
                wait_terminal(engine, "u", engine.start("u", tmp, f"k{i}", "x")[0])
            self.assertLessEqual(len(engine._tasks), 3)
            self.assertEqual(len(engine._keys), len(engine._tasks))

    def test_missing_workspace_is_a_typed_error(self):
        engine = Engine(lambda m, t: {"text": "", "tool_calls": []})
        with self.assertRaises(BadWorkspace):
            engine.start("u", Path(tempfile.gettempdir()) / "definitely-missing-dir-xyz", "k", "x")

class ScrubForms(unittest.TestCase):
    def test_repr_escaped_lowercase_and_posix_spellings_are_scrubbed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            box = ToolBox(root)
            try:
                open(root / "nope" / "x.txt")
            except OSError as exc:
                message = f"{type(exc).__name__}: {exc}"  # str(OSError) uses the doubled-backslash repr form
            samples = [message, str(root).lower() + "\\x", root.as_posix() + "/y", str(Path.home()) + "\\z"]
            for text in samples:
                out = box.scrub(text)
                self.assertNotIn(str(root).lower(), out.lower())
                self.assertNotIn(str(root).replace("\\", "\\\\").lower(), out.lower())
                self.assertNotIn(str(Path.home()).lower(), out.lower())
                self.assertNotIn("users\\\\" + getpass.getuser().lower(), out.lower())

    def test_ordinary_words_are_not_corrupted_by_user_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = ToolBox(tmp)
            real = getpass.getuser
            getpass.getuser = lambda: "test"
            try:
                text = "FAILED tests/test_calc.py::test_add in src/test/utils"
                self.assertEqual(box.scrub(text), text)
            finally:
                getpass.getuser = real

class TestRunHardening(unittest.TestCase):
    def test_host_secrets_are_not_passed_to_test_commands(self):
        os.environ["AUDIT_FAKE_API_KEY"] = "sk-audit-secret"
        self.addCleanup(os.environ.pop, "AUDIT_FAKE_API_KEY", None)
        with tempfile.TemporaryDirectory() as tmp:
            code = "import os; print(os.environ.get('AUDIT_FAKE_API_KEY'), os.environ.get('EXTRA_OK'), 'PATH' in os.environ or 'Path' in os.environ)"
            box = ToolBox(tmp, {"t": [PY, "-c", code]}, test_env={"EXTRA_OK": "yes"})
            out = box.t_run_tests("t")["output"]
            self.assertIn("None yes True", out)
            self.assertNotIn("sk-audit-secret", out)

    def test_timeout_kills_the_process_tree_and_returns_promptly(self):
        code = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)']);time.sleep(60)"
        with tempfile.TemporaryDirectory() as tmp:
            box = ToolBox(tmp, {"t": [PY, "-c", code]}, test_timeout=1)
            started = time.time()
            result = box.t_run_tests("t")
            self.assertEqual(result["status"], "TIMEOUT")
            self.assertLess(time.time() - started, 8, "a surviving grandchild held the pipe open")

class GlobSemantics(unittest.TestCase):
    def test_double_star_matches_zero_or_more_directories(self):
        self.assertTrue(glob_match("src/m.py", "m.py", "src/**/*.py"))
        self.assertTrue(glob_match("src/a/b/m.py", "m.py", "src/**/*.py"))
        self.assertFalse(glob_match("lib/m.py", "m.py", "src/**/*.py"))
        self.assertTrue(glob_match("deploy/legacy/settings_old.ini", "settings_old.ini", "*.ini"))
        self.assertFalse(glob_match("src/a/b.py", "b.py", "src/*.py"), "single * must not cross directories")

if __name__ == "__main__":
    unittest.main()
