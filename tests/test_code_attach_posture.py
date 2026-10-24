"""Safety posture (migration playbook phase 0): the HTTP entry point is read/propose by default.

In that mode no tool can change the disk, run a command, commit or undo, whatever the model asks for and whatever approval
a client sends. Mutation exists only when the host explicitly opts in (`allow_mutation=True`), as a local experimental mode.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
import functools
from code_attach import make_router as _make_router
make_router = functools.partial(_make_router, prototype=True)
from code_attach.engine import Engine
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

class Posture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "a.txt").write_text("alpha\n", encoding="utf-8")
        self.before = {p.name: p.read_bytes() for p in self.root.iterdir()}

    def unchanged(self):
        return {p.name: p.read_bytes() for p in self.root.iterdir()} == self.before

    def test_default_router_offers_no_mutation_and_says_so(self):
        app = FastAPI()
        app.include_router(make_router(lambda m, t: {"text": "", "tool_calls": []}, lambda r: "u", lambda p, w: str(self.root), {"unit": [PY, "-c", "pass"]}))
        got = TestClient(app).get("/availability").json()
        self.assertEqual((got["modes"], got["mutation"]), (["read", "propose"], False))

    def test_tools_shown_to_the_model_exclude_every_effect(self):
        names = {s["name"] for s in ToolBox(self.root, {"t": [PY, "-c", "pass"]}, allow_mutation=False).schemas()}
        self.assertFalse(names & {"apply_proposal", "run_tests", "propose_undo"})
        self.assertTrue({"propose_edit", "propose_edits", "read_file", "find_symbol"} <= names)
        with_mutation = {s["name"] for s in ToolBox(self.root, {"t": [PY, "-c", "pass"]}).schemas()}
        self.assertTrue({"apply_proposal", "run_tests", "propose_undo"} <= with_mutation)

    def test_a_model_that_tries_anyway_gets_errors_and_no_approval_card(self):
        sequence = iter([
            {"text": "", "tool_calls": [tc(1, "propose_edit", path="a.txt", old="alpha", new="BETA")]},
            lambda m: {"text": "", "tool_calls": [tc(2, "apply_proposal", proposal_id=json.loads(m[-1]["content"])["proposal_id"]), tc(3, "run_tests", name="t"),
                                                  tc(4, "propose_undo", applied_id="x"), tc(5, "propose_new_file", path="n.txt", content="x")]},
            {"text": "here is the proposal", "tool_calls": []}])
        def model(messages, tools):
            step = next(sequence)
            return step(messages) if callable(step) else step
        engine = Engine(model, {"t": [PY, "-c", "pass"]}, allow_mutation=False)
        tid, _ = engine.start("u", self.root, "k", "x")
        wait(lambda: engine.events("u", tid)["terminal"])
        r = engine.events("u", tid)
        self.assertEqual(r["state"], "COMPLETED")
        self.assertFalse([e for e in r["events"] if e["type"] == "approval_required"])
        results = [e["data"]["result"] for e in r["events"] if e["type"] == "tool_result"]
        self.assertEqual([x["status"] for x in results[1:4]], ["ERROR"] * 3)
        self.assertIn("read/propose mode", results[1]["error"])
        self.assertIs(results[0]["applied"], False)
        self.assertIn("proposal only", results[0]["note"])
        self.assertTrue(self.unchanged())
        self.assertEqual(engine.modes, ["read", "propose"])

    def test_even_direct_calls_and_forged_decisions_cannot_mutate(self):
        box = ToolBox(self.root, {"t": [PY, "-c", "open('pwned.txt','w').write('x')"]}, allow_mutation=False)
        proposal = box.t_propose_edit("a.txt", "alpha", "BETA")
        for call in (lambda: box.t_apply_proposal(proposal["proposal_id"]), lambda: box.t_run_tests("t"), lambda: box.call("apply_proposal", {"proposal_id": proposal["proposal_id"]}),
                     lambda: box.describe_effect("run_tests", {"name": "t"}), lambda: box.describe_effect("apply_proposal", {"proposal_id": proposal["proposal_id"]})):
            with self.assertRaises(ValueError):
                call()
        self.assertTrue(self.unchanged())

    def test_system_prompt_tells_the_model_it_can_only_propose(self):
        seen = []
        engine = Engine(lambda m, t: (seen.append(m[0]["text"]), {"text": "ok", "tool_calls": []})[1], allow_mutation=False)
        tid, _ = engine.start("u", self.root, "k", "x")
        engine.join("u", tid)
        self.assertIn("cannot apply them", seen[0])

    def test_explicit_opt_in_restores_the_approval_flow(self):
        app = FastAPI()
        app.include_router(make_router(lambda m, t: {"text": "", "tool_calls": []}, lambda r: "u", lambda p, w: str(self.root), {"unit": [PY, "-c", "pass"]}, allow_mutation=True))
        got = TestClient(app).get("/availability").json()
        self.assertEqual((got["modes"], got["mutation"]), (["read", "propose", "apply", "test"], True))

    def test_in_process_entrances_refuse_to_mount_without_the_prototype_acknowledgement(self):
        from code_attach.protocol_router import make_protocol_router
        for factory in (_make_router, make_protocol_router):
            with self.assertRaises(RuntimeError) as caught:
                factory(lambda m, t: {"text": "", "tool_calls": []}, lambda r: "u", lambda p, w: None)
            self.assertIn("host_client", str(caught.exception))

if __name__ == "__main__":
    unittest.main()
