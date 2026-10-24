"""Second audit batch: concurrency, cancellation, tolerance of sloppy models, bounded context, path display."""
import getpass
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from code_attach import make_router
from code_attach.activity import display_path
from code_attach.engine import Engine
from code_attach.session import KEEP_FULL_TOOL_RESULTS, Task
from code_attach.tools import ToolBox

PY = sys.executable

def tc(i, tool, **args):
    return {"id": f"c{i}", "name": tool, "args": args}

def wait(pred, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        value = pred()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition not reached")

def project():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name).resolve()
    (root / "a.txt").write_text("x\nmatch one\nmatch two\nmatch three\n", encoding="utf-8")
    (root / "b.txt").write_text("match elsewhere\n", encoding="utf-8")
    return tmp, root

class SearchAndScan(unittest.TestCase):
    def test_start_line_pages_one_file_and_applies_to_every_file_in_a_directory(self):
        tmp, root = project()
        with tmp:
            box = ToolBox(root)
            self.assertEqual({h["path"] for h in box.t_search_text("match", path=".", start_line=2)["matches"]}, {"a.txt"},
                             "b.txt has its only hit on line 1, which start_line=2 skips")
            hits = box.t_search_text("match", path="a.txt", start_line=3)["matches"]
            self.assertEqual([h["line"] for h in hits], [3, 4])

    def test_single_file_path_is_searchable(self):
        tmp, root = project()
        with tmp:
            self.assertEqual({h["path"] for h in ToolBox(root).t_search_text("match", path="b.txt")["matches"]}, {"b.txt"})

    def test_scan_limit_is_reported_not_silent(self):
        import code_attach.tools as tools
        tmp, root = project()
        with tmp:
            for i in range(5):
                (root / f"f{i}.txt").write_text("y\n", encoding="utf-8")
            old, tools.MAX_FILES_SCANNED = tools.MAX_FILES_SCANNED, 3
            try:
                result = ToolBox(root).call("find_files", {"pattern": "*.txt"})
            finally:
                tools.MAX_FILES_SCANNED = old
            self.assertTrue(result["scan_truncated"])
            self.assertFalse(ToolBox(root).call("find_files", {"pattern": "*.txt"})["scan_truncated"])

    @unittest.skipUnless(os.name == "nt", "junctions are a Windows feature")
    def test_junction_pointing_outside_is_not_followed(self):
        tmp, root = project()
        with tmp, tempfile.TemporaryDirectory() as outside:
            (Path(outside) / "secret.txt").write_text("match outside\n", encoding="utf-8")
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "link"), outside], capture_output=True)
            if made.returncode != 0:
                self.skipTest("cannot create a junction here")
            paths = {h["path"] for h in ToolBox(root).t_search_text("match")["matches"]}
            self.assertNotIn("link/secret.txt", paths)
            self.assertEqual(paths, {"a.txt", "b.txt"})

class SloppyModel(unittest.TestCase):
    def run_model(self, turns):
        tmp, root = project()
        self.addCleanup(tmp.cleanup)
        it = iter(turns)
        engine = Engine(lambda m, t: next(it))
        tid, _ = engine.start("u", root, "k", "x")
        wait(lambda: engine.events("u", tid)["terminal"])
        engine.join("u", tid)
        return engine.events("u", tid)

    def test_call_without_id_is_executed_and_completes(self):
        r = self.run_model([{"text": "", "tool_calls": [{"name": "list_dir", "args": {}}]}, {"text": "done", "tool_calls": []}])
        self.assertEqual(r["state"], "COMPLETED")
        self.assertEqual([e for e in r["events"] if e["type"] == "tool_result"][0]["data"]["result"]["entries"][:1], ["a.txt"])

    def test_garbage_tool_calls_become_errors_not_crashes(self):
        r = self.run_model([{"text": "", "tool_calls": ["oops", 5, None, {"id": 7, "name": None, "args": "x"}]}, {"text": "ok", "tool_calls": []}])
        self.assertEqual(r["state"], "COMPLETED")
        statuses = [e["data"]["result"]["status"] for e in r["events"] if e["type"] == "tool_result"]
        self.assertEqual(statuses, ["ERROR"] * 4)

class Cancellation(unittest.TestCase):
    def test_cancel_kills_a_running_test_promptly(self):
        tmp, root = project()
        with tmp:
            turn = iter([{"text": "", "tool_calls": [tc(1, "run_tests", name="slow")]}, {"text": "", "tool_calls": []}])
            engine = Engine(lambda m, t: next(turn), {"slow": [PY, "-c", "import time; time.sleep(60)"]}, test_timeout=120)
            tid, _ = engine.start("u", root, "k", "x")
            ev = wait(lambda: [e for e in engine.events("u", tid)["events"] if e["type"] == "approval_required"])
            engine.decide("u", tid, ev[0]["data"]["action_id"], ev[0]["data"]["fingerprint"], True)
            time.sleep(1.0)  # let the command start
            started = time.time()
            engine.cancel("u", tid)
            self.assertTrue(engine.join("u", tid, timeout=10))
            self.assertLess(time.time() - started, 8)
            self.assertEqual(engine.events("u", tid)["state"], "CANCELLED")

class ApprovalAndRetry(unittest.TestCase):
    def test_unanswered_approval_expires_visibly(self):
        tmp, root = project()
        with tmp:
            box = ToolBox(root)
            proposal = box.t_propose_new_file("n.txt", "hi\n")
            turns = iter([{"text": "", "tool_calls": [tc(1, "apply_proposal", proposal_id=proposal["proposal_id"])]}, {"text": "", "tool_calls": []}])
            task = Task("u", box, lambda m, t: next(turns), "x", approval_timeout=0.3)
            task.start()
            wait(lambda: task.events_after()[1] in {"COMPLETED", "FAILED"})
            types = [e["type"] for e in task.events_after()[0]]
            self.assertIn("approval_expired", types)
            self.assertFalse((root / "n.txt").exists())

    def test_transient_os_error_does_not_burn_the_proposal(self):
        tmp, root = project()
        with tmp:
            box = ToolBox(root)
            pid = box.t_propose_edit("a.txt", "match one", "match 1")["proposal_id"]
            real, calls = box.flow.replace_text, []
            def flaky(*a, **k):
                calls.append(1)
                if len(calls) == 1:
                    raise PermissionError("locked by editor")
                return real(*a, **k)
            box.flow.replace_text = flaky
            with self.assertRaises(PermissionError):
                box.t_apply_proposal(pid)
            self.assertEqual(box.t_apply_proposal(pid)["status"], "APPLIED")
            self.assertIn("match 1", (root / "a.txt").read_text(encoding="utf-8"))

class BoundedContext(unittest.TestCase):
    def test_old_tool_results_are_shortened_for_the_model_only(self):
        tmp, root = project()
        with tmp:
            task = Task("u", ToolBox(root), lambda m, t: None, "x")
            big = json.dumps({"blob": "z" * 5000})
            for i in range(KEEP_FULL_TOOL_RESULTS + 3):
                task.messages.append({"role": "tool", "tool_call_id": f"c{i}", "name": "read_file", "content": big})
            view = [m for m in task._model_view() if m["role"] == "tool"]
            self.assertEqual(sum(len(m["content"]) > 1000 for m in view), KEEP_FULL_TOOL_RESULTS)
            self.assertTrue(view[0]["content"].endswith("[older result shortened]"))
            self.assertEqual(len([m for m in task.messages if m["role"] == "tool" and len(m["content"]) == len(big)]), KEEP_FULL_TOOL_RESULTS + 3)

class LockingAndLogging(unittest.TestCase):
    def test_polling_is_not_blocked_by_a_slow_log_write(self):
        tmp, root = project()
        with tmp:
            slow = threading.Event()
            def sink(task_id, event):
                if event["type"] == "assistant":
                    slow.set()
                    time.sleep(1.0)  # simulated fsync latency
            engine = Engine(lambda m, t: {"text": "hello", "tool_calls": []}, event_sink=sink)
            tid, _ = engine.start("u", root, "k", "x")
            self.assertTrue(slow.wait(5))
            started = time.time()
            engine.events("u", tid)
            self.assertLess(time.time() - started, 0.3, "poll waited for the sink")
            engine.join("u", tid)

    def test_concurrent_decisions_and_events_do_not_deadlock(self):
        tmp, root = project()
        with tmp:
            def sink(task_id, event):
                time.sleep(0.002)
            def model(messages, tools):
                n = sum(m["role"] == "assistant" for m in messages)
                if n < 6:
                    return {"text": "", "tool_calls": [tc(n, "run_tests", name="ok")]}
                return {"text": "done", "tool_calls": []}
            engine = Engine(model, {"ok": [PY, "-c", "pass"]}, event_sink=sink)
            tid, _ = engine.start("u", root, "k", "x")
            answered = set()
            def approver():
                while not engine.events("u", tid)["terminal"]:
                    for e in engine.events("u", tid)["events"]:
                        if e["type"] == "approval_required" and e["data"]["action_id"] not in answered:
                            try:
                                if engine.decide("u", tid, e["data"]["action_id"], e["data"]["fingerprint"], True):
                                    answered.add(e["data"]["action_id"])
                            except OSError:
                                pass
                    time.sleep(0.005)
            threads = [threading.Thread(target=approver) for _ in range(3)]
            for t in threads:
                t.start()
            self.assertTrue(engine.join("u", tid, timeout=30))
            for t in threads:
                t.join(5)
            self.assertEqual(engine.events("u", tid)["state"], "COMPLETED")
            self.assertEqual(len(answered), 6)

    def test_only_log_failures_map_to_503(self):
        tmp, root = project()
        with tmp:
            def make(sink):
                app = FastAPI()
                app.include_router(make_router(lambda m, t: {"text": "", "tool_calls": []}, lambda r: "u", lambda p, w: str(root), event_sink=sink))
                return TestClient(app)
            body = {"workspace_id": "w", "instruction": "x", "idempotency_key": "k"}
            def broken(task_id, event):
                raise OSError("disk full")
            self.assertEqual(make(broken).post("/tasks", json=body).status_code, 503)
            self.assertEqual(make(lambda task_id, event: None).post("/tasks", json=body).status_code, 202)

class PathDisplay(unittest.TestCase):
    def test_display_path_never_contains_the_user_name_or_drive(self):
        shown = display_path(Path.home() / ".code-attach" / "logs" / "abc")
        self.assertEqual(shown, "~/.code-attach/logs/abc")
        outside = display_path(Path(Path.home().anchor) / "audit_outside_home" / "logs")
        self.assertEqual(outside, "logs")
        for text in (shown, outside):
            self.assertNotIn(getpass.getuser().lower(), text.lower())

    def test_host_can_wire_logging_without_importing_the_primitives_package(self):
        source = (Path(__file__).resolve().parents[1] / "examples" / "local_app.py").read_text(encoding="utf-8")
        self.assertIn("from code_attach.activity import", source)
        self.assertNotIn("from coding_agent", source)

if __name__ == "__main__":
    unittest.main()
