"""Normal and abnormal paths against the agent-side Engine with scripted mock models (no HTTP, no network)."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from code_attach.engine import Engine

BUGGY = "def add(a, b):\n    return a - b\n"
TEST = "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\nif __name__ == '__main__':\n    unittest.main()\n"
PY = sys.executable

def tc(i, tool, **args):
    return {"id": f"c{i}", "name": tool, "args": args}

def model(*turns):
    """Each turn is a dict or a callable(messages)->dict. Past the script the model raises (a hard model failure)."""
    it = iter(turns)
    def complete(messages, tools):
        t = next(it)
        return t(messages) if callable(t) else t
    return complete

def last_json(messages):
    return json.loads(messages[-1]["content"])

class Harness:
    def __init__(self, complete, tests=None, timeout=30):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "calc.py").write_text(BUGGY, encoding="utf-8")
        (self.root / "test_calc.py").write_text(TEST, encoding="utf-8")
        self.engine = Engine(complete, tests, test_timeout=timeout)
        self.id, _ = self.engine.start("alice", self.root, "k", "do it")

    def ev(self):
        return self.engine.events("alice", self.id)

    def wait(self, pred):
        for _ in range(400):
            r = self.ev()
            if pred(r):
                return r
            time.sleep(0.02)
        raise AssertionError(f"timeout; state={r['state']} events={r['events']}")

    def approval(self, n=1):
        r = self.wait(lambda r: sum(e["type"] == "approval_required" for e in r["events"]) >= n)
        return [e["data"] for e in r["events"] if e["type"] == "approval_required"][n - 1]

    def decide(self, d, approve=True, fingerprint=None):
        return self.engine.decide("alice", self.id, d["action_id"], fingerprint or d["fingerprint"], approve)

    def done(self):
        return self.wait(lambda r: r["terminal"])

    def results(self, tool=None):
        return [e["data"] for e in self.ev()["events"] if e["type"] == "tool_result" and (tool is None or e["data"]["tool"] == tool)]

    def calc(self):
        return (self.root / "calc.py").read_text(encoding="utf-8")

    def close(self):
        self.engine.cancel("alice", self.id)
        self.engine.join("alice", self.id)
        self.tmp.cleanup()

def fix_flow(test_name=None):
    turns = [
        {"text": "", "tool_calls": [tc(1, "propose_edit", path="calc.py", old="a - b", new="a + b")]},
        lambda m: {"text": "", "tool_calls": [tc(2, "apply_proposal", proposal_id=last_json(m)["proposal_id"])]},
    ]
    if test_name:
        turns.append({"text": "", "tool_calls": [tc(3, "run_tests", name=test_name)]})
    turns.append({"text": "finished", "tool_calls": []})
    return model(*turns)

class Case(unittest.TestCase):
    def make(self, *a, **k):
        h = Harness(*a, **k)
        self.addCleanup(h.close)
        return h

class NormalPaths(Case):
    def test_fix_apply_and_passing_tests(self):
        h = self.make(fix_flow("unit"), {"unit": [PY, "test_calc.py"]})
        self.assertEqual(h.calc(), BUGGY)
        h.decide(h.approval(1))
        second = h.approval(2)  # raised only after the apply result was produced
        self.assertEqual(h.calc(), BUGGY.replace("a - b", "a + b"))
        h.decide(second)
        self.assertEqual(h.done()["state"], "COMPLETED")
        self.assertEqual(h.results("run_tests")[0]["result"]["status"], "PASS")

    def test_new_file_creation(self):
        h = self.make(model(
            {"text": "", "tool_calls": [tc(1, "propose_new_file", path="pkg/new.py", content="X = 1\n")]},
            lambda m: {"text": "", "tool_calls": [tc(2, "apply_proposal", proposal_id=last_json(m)["proposal_id"])]},
            {"text": "ok", "tool_calls": []}))
        h.decide(h.approval())
        self.assertEqual(h.done()["state"], "COMPLETED")
        self.assertEqual((h.root / "pkg" / "new.py").read_text(encoding="utf-8"), "X = 1\n")

class ApprovalAbnormal(Case):
    def test_reject_leaves_file_and_reports_not_approved(self):
        h = self.make(fix_flow())
        h.decide(h.approval(), approve=False)
        h.done()
        self.assertEqual(h.calc(), BUGGY)
        self.assertEqual(h.results("apply_proposal")[0]["status"], "NOT_APPROVED")

    def test_wrong_fingerprint_and_duplicate_decision(self):
        h = self.make(fix_flow())
        d = h.approval()
        self.assertFalse(h.decide(d, fingerprint="0" * 64))
        self.assertTrue(h.decide(d))
        self.assertFalse(h.decide(d), "second decision on the same action must be refused")

    def test_decision_without_pending_action(self):
        h = self.make(model({"text": "hi", "tool_calls": []}))
        h.done()
        self.assertFalse(h.engine.decide("alice", h.id, "c1", "x", True))

    def test_cancel_while_awaiting_approval(self):
        h = self.make(fix_flow())
        h.approval()
        h.engine.cancel("alice", h.id)
        self.assertEqual(h.done()["state"], "CANCELLED")
        self.assertEqual(h.calc(), BUGGY)

    def test_stale_file_yields_conflict_not_overwrite(self):
        h = self.make(fix_flow())
        d = h.approval()
        (h.root / "calc.py").write_text("# edited elsewhere\n" + BUGGY, encoding="utf-8")
        h.decide(d)
        h.done()
        self.assertEqual(h.results("apply_proposal")[0]["result"]["status"], "CONFLICT")
        self.assertTrue(h.calc().startswith("# edited elsewhere"))

    def test_proposal_cannot_be_applied_twice(self):
        h = self.make(model(
            {"text": "", "tool_calls": [tc(1, "propose_edit", path="calc.py", old="a - b", new="a + b")]},
            lambda m: {"text": "", "tool_calls": [tc(2, "apply_proposal", proposal_id=last_json(m)["proposal_id"])]},
            lambda m: {"text": "", "tool_calls": [tc(3, "apply_proposal", proposal_id=json.loads(m[-3]["content"])["proposal_id"])]},
            {"text": "x", "tool_calls": []}))
        h.decide(h.approval(1))
        r = h.wait(lambda r: sum(e["type"] == "approval_required" for e in r["events"]) >= 1 and r["state"] in {"RUNNING", "COMPLETED"} and len(h.results("apply_proposal")) >= 1)
        h.done()
        second = h.results("apply_proposal")[-1]
        self.assertEqual(second["result"]["status"], "ERROR")

class ModelAbnormal(Case):
    def test_unknown_tool_is_error_and_loop_continues(self):
        h = self.make(model({"text": "", "tool_calls": [tc(1, "rm_rf", path="/")]}, ), )
        # next turn raises (script exhausted) -> FAILED, but the unknown tool must have been rejected first
        self.assertEqual(h.done()["state"], "FAILED")
        self.assertEqual(h.results()[0]["result"]["status"], "ERROR")

    def test_malformed_arguments_are_errors(self):
        h = self.make(model(
            {"text": "", "tool_calls": [tc(1, "read_file"), {"id": "c2", "name": "read_file", "args": "not-an-object"}, tc(3, "list_dir", depth="deep")]},
            {"text": "ok", "tool_calls": []}))
        self.assertEqual(h.done()["state"], "COMPLETED")
        self.assertTrue(all(r["result"]["status"] == "ERROR" for r in h.results()))

    def test_tool_call_without_id_fails_task(self):
        h = self.make(model({"text": "", "tool_calls": [{"name": "list_dir", "args": {}}]}))
        self.assertEqual(h.done()["state"], "FAILED")

    def test_model_exception_is_failed_not_completed(self):
        h = self.make(model())
        self.assertEqual(h.done()["state"], "FAILED")

    def test_endless_tool_loop_hits_step_limit(self):
        h = self.make(lambda messages, tools: {"text": "", "tool_calls": [tc(len(messages), "list_dir", path=".")]})
        r = h.done()
        self.assertEqual(r["state"], "FAILED")
        self.assertIn("step limit", r["events"][-1]["data"]["detail"])

    def test_completed_requires_final_message_without_tool_calls(self):
        h = self.make(model({"text": "just talking", "tool_calls": []}))
        self.assertEqual(h.done()["state"], "COMPLETED")
        self.assertEqual(h.results(), [])

class PathAbnormal(Case):
    def run_calls(self, calls):
        h = self.make(model({"text": "", "tool_calls": calls}, {"text": "", "tool_calls": []}))
        h.done()
        return h

    def test_escapes_and_secrets_refused_on_read_and_propose(self):
        (Path(tempfile.gettempdir()) / "attach_outside.txt").write_text("outside", encoding="utf-8")
        h0 = self.make(model())  # only to get a root
        (h0.root / ".env").write_text("SECRET=1", encoding="utf-8")
        calls = [tc(1, "read_file", path="../attach_outside.txt"), tc(2, "read_file", path=".env"), tc(3, "read_file", path="C:/Windows/win.ini"),
                 tc(4, "propose_new_file", path=".env.local", content="x"), tc(5, "propose_new_file", path="../escape.py", content="x"),
                 tc(6, "propose_new_file", path="calc.py", content="x"), tc(7, "propose_edit", path="calc.py", old="nope", new="x"),
                 tc(8, "propose_edit", path="calc.py", old="", new="x")]
        h = self.run_calls(calls)
        self.assertEqual([r["result"]["status"] for r in h.results()], ["ERROR"] * len(calls))
        self.assertNotIn("SECRET", json.dumps(h.ev()))
        self.assertFalse((h.root.parent / "escape.py").exists())

    def test_ambiguous_edit_is_rejected(self):
        h = self.make(model({"text": "", "tool_calls": [tc(1, "propose_edit", path="calc.py", old="a", new="z")]}, {"text": "", "tool_calls": []}))
        h.done()
        self.assertEqual(h.results()[0]["result"]["status"], "ERROR")

class TestRunAbnormal(Case):
    def one_test(self, argv, timeout=30):
        h = self.make(model({"text": "", "tool_calls": [tc(1, "run_tests", name="t")]}, {"text": "", "tool_calls": []}), {"t": argv}, timeout)
        h.decide(h.approval())
        h.done()
        return h.results("run_tests")[0]["result"]

    def test_failing_tests_report_fail_with_exit_code(self):
        r = self.one_test([PY, "-c", "import sys; print('boom'); sys.exit(3)"])
        self.assertEqual((r["status"], r["exit_code"]), ("FAIL", 3))
        self.assertIn("boom", r["output"])

    def test_timeout_is_reported(self):
        r = self.one_test([PY, "-c", "import time; time.sleep(30)"], timeout=1)
        self.assertEqual(r["status"], "TIMEOUT")

    def test_missing_executable_is_unknown(self):
        self.assertEqual(self.one_test(["definitely-not-a-real-binary-xyz"])["status"], "UNKNOWN")

    def test_huge_output_is_bounded(self):
        r = self.one_test([PY, "-c", "print('x' * 5_000_000)"])
        self.assertLessEqual(len(r["output"]), 8000)

    def test_model_cannot_choose_its_own_command(self):
        h = self.make(model({"text": "", "tool_calls": [tc(1, "run_tests", name="rm -rf /")]}, {"text": "", "tool_calls": []}), {"t": [PY, "-c", "pass"]})
        h.done()
        self.assertEqual(h.results("run_tests")[0]["result"]["status"], "ERROR")
        self.assertEqual([e for e in h.ev()["events"] if e["type"] == "approval_required"], [])

class OwnershipAbnormal(Case):
    def test_other_principal_cannot_observe_decide_or_cancel(self):
        from code_attach.engine import NotFound
        h = self.make(fix_flow())
        d = h.approval()
        for call in (lambda: h.engine.events("bob", h.id), lambda: h.engine.decide("bob", h.id, d["action_id"], d["fingerprint"], True), lambda: h.engine.cancel("bob", h.id)):
            with self.assertRaises(NotFound):
                call()
        self.assertEqual(h.calc(), BUGGY)

    def test_idempotency_and_capacity(self):
        from code_attach.engine import Busy, KeyConflict
        h = self.make(model({"text": "x", "tool_calls": []}))
        self.assertEqual(h.engine.start("alice", h.root, "k", "do it")[0], h.id)
        with self.assertRaises(KeyConflict):
            h.engine.start("alice", h.root, "k", "different")
        import threading
        gate = threading.Event()  # capacity counts ACTIVE tasks, so the first one must still be running
        small = Engine(lambda messages, tools: (gate.wait(10), {"text": "", "tool_calls": []})[1], max_tasks=1)
        first, _ = small.start("a", h.root, "1", "x")
        with self.assertRaises(Busy):
            small.start("a", h.root, "2", "x")
        gate.set()
        small.join("a", first)

if __name__ == "__main__":
    unittest.main()
