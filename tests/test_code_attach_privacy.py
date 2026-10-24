"""Absolute paths, home directory and OS user name must not leave the process (frontend events, model context, logs)."""
import getpass
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from code_attach.engine import Engine

PY = sys.executable

def tc(i, tool, **args):
    return {"id": f"c{i}", "name": tool, "args": args}

def forbidden_strings(root):
    home = Path.home()
    user = getpass.getuser()
    out = {str(root), root.as_posix(), str(home), home.as_posix()}
    if len(user) >= 3:
        out.add(user)
    return out

class PrivacyTests(unittest.TestCase):
    def run_task(self, turns, tests):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        (root / "a.txt").write_text("hello\n", encoding="utf-8")
        it = iter(turns)
        seen_by_model = []
        def complete(messages, tools):
            seen_by_model.append(json.dumps(messages, ensure_ascii=False))
            return next(it)
        sink = []
        engine = Engine(complete, tests, test_timeout=30, event_sink=lambda task_id, event: sink.append(event))
        tid, _ = engine.start("someone@example.com", root, "k", "check the tests")
        seen = 0
        for _ in range(500):
            r = engine.events("someone@example.com", tid, seen)
            for e in r["events"]:
                seen += 1
                if e["type"] == "approval_required":
                    engine.decide("someone@example.com", tid, e["data"]["action_id"], e["data"]["fingerprint"], True)
            if r["terminal"] and not r["events"]:
                break
            time.sleep(0.01)
        engine.cancel("someone@example.com", tid)
        engine.join("someone@example.com", tid)
        return root, engine.events("someone@example.com", tid), sink, seen_by_model

    def test_no_personal_or_absolute_path_in_events_sink_or_model_context(self):
        script = "import os, sys; print(os.getcwd()); print(os.path.expanduser('~')); print(sys.executable); sys.exit(1)"
        root, final, sink, model_ctx = self.run_task([
            {"text": "", "tool_calls": [tc(1, "read_file", path="missing.txt"), tc(2, "run_tests", name="t")]},
            {"text": "done", "tool_calls": []}], {"t": [PY, "-c", script]})
        frontend = json.dumps(final["events"], ensure_ascii=False)
        logs = json.dumps(sink, ensure_ascii=False)
        for needle in forbidden_strings(root):
            self.assertNotIn(needle, frontend, f"frontend leaks {needle!r}")
            self.assertNotIn(needle, logs, f"activity log leaks {needle!r}")
            self.assertNotIn(needle, "".join(model_ctx), f"model context leaks {needle!r}")

    def test_started_record_is_private_and_uses_opaque_refs(self):
        root, final, sink, _ = self.run_task([{"text": "ok", "tool_calls": []}], None)
        self.assertNotIn("task_started", [e["type"] for e in final["events"]])
        self.assertEqual([e["id"] for e in final["events"]], [str(i) for i in range(1, len(final["events"]) + 1)])
        started = next(e for e in sink if e["type"] == "task_started")
        self.assertEqual(set(started["data"]), {"instruction", "workspace_ref", "principal_ref"})
        self.assertNotIn("example.com", json.dumps(sink))

    def test_oserror_text_is_scrubbed(self):
        # a FileNotFoundError carrying an absolute path, raised inside a tool
        from code_attach.tools import ToolBox
        with tempfile.TemporaryDirectory() as tmp:
            box = ToolBox(tmp)
            text = box.scrub(f"FileNotFoundError: [Errno 2] No such file: '{Path(tmp).resolve()}\\x.txt' and {Path.home()}\\y")
            for needle in forbidden_strings(Path(tmp).resolve()):
                self.assertNotIn(needle, text)
            self.assertIn("<workspace>", text)

if __name__ == "__main__":
    unittest.main()
