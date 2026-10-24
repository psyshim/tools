"""Phase 6: semantics around effects that must hold at the loop and tool layer, independent of processes."""
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from code_attach.tools import ToolBox
from coding_service.loop import Task
from coding_service.wire import to_protocol_events

def wait(pred, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        value = pred()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition not reached")

class SlowCommitToolbox:
    """A toolbox whose apply 'happens' and then takes a while to return, so cancel can land after the commit point."""
    allow_mutation = True

    def __init__(self):
        self.applied, self.entered, self.release = False, threading.Event(), threading.Event()

    def schemas(self):
        return [{"name": "apply_proposal", "description": "Requires user approval.", "parameters": {"type": "object", "properties": {}}}]

    def is_effect(self, name):
        return name == "apply_proposal"

    def describe_effect(self, name, args):
        return {"kind": "apply", "path": "a.txt", "diff": "-a\n+b\n"}, "fingerprint-1"

    def scrub(self, text):
        return text

    def request_cancel(self):
        pass

    def call(self, name, args, call_id=None):
        self.applied = True            # the change is on disk from here on
        self.entered.set()
        self.release.wait(10)
        return {"status": "APPLIED", "path": "a.txt"}

class CancellationAfterCommit(unittest.TestCase):
    def test_a_cancel_that_lands_after_the_effect_never_hides_the_receipt(self):
        box = SlowCommitToolbox()
        turns = iter([{"text": "", "tool_calls": [{"id": "c1", "name": "apply_proposal", "args": {}}]}, {"text": "all done", "tool_calls": []}])
        task = Task("u", box, lambda m, t: next(turns), "apply it")
        task.start()
        card = wait(lambda: [e for e in task.events_after()[0] if e["type"] == "approval_required"])[0]["data"]
        self.assertTrue(task.decide(card["action_id"], card["fingerprint"], True))
        self.assertTrue(box.entered.wait(10))
        task.cancel()                  # the user changes their mind after the change is already made
        box.release.set()
        task.thread.join(10)
        events, state = task.events_after()
        self.assertEqual(state, "CANCELLED")
        receipts = [e for e in events if e["type"] == "tool_result" and e["data"].get("result", {}).get("status") == "APPLIED"]
        self.assertEqual(len(receipts), 1, "the effect that really happened is on record")
        protocol = [(e["type"], e["data"].get("state")) for e in to_protocol_events("t", events)]
        self.assertIn(("mutation_applied", None), protocol)
        self.assertEqual([s for t, s in protocol if t == "state"][-1], "CANCELLED")
        self.assertLess([t for t, _ in protocol].index("mutation_applied"), [t for t, s in protocol].index("state", [t for t, _ in protocol].index("mutation_applied")))

    def test_cancel_before_the_decision_applies_nothing(self):
        box = SlowCommitToolbox()
        turns = iter([{"text": "", "tool_calls": [{"id": "c1", "name": "apply_proposal", "args": {}}]}])
        task = Task("u", box, lambda m, t: next(turns), "apply it")
        task.start()
        wait(lambda: [e for e in task.events_after()[0] if e["type"] == "approval_required"])
        task.cancel()
        task.thread.join(10)
        self.assertFalse(box.applied)
        self.assertEqual(task.events_after()[1], "CANCELLED")

@unittest.skipUnless(shutil.which("git"), "git is not installed")
class GitCommitSeparation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.root / "a.txt").write_text("one\n", encoding="utf-8")
        self.git("add", "a.txt")
        self.git("commit", "-q", "-m", "initial")
        (self.root / "a.txt").write_text("two\n", encoding="utf-8")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, timeout=60).stdout.strip()

    def test_preexisting_staged_changes_block_the_commit_and_nothing_is_committed(self):
        (self.root / "other.txt").write_text("someone else's staged work\n", encoding="utf-8")
        self.git("add", "other.txt")
        box = ToolBox(self.root, allow_mutation={"git_commit"})
        with self.assertRaises(ValueError) as caught:
            box.call("propose_git_commit", {"paths": ["a.txt"], "message": "change a"})
        self.assertIn("staged", str(caught.exception).lower())
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "other.txt", "their staging area is untouched")

    def test_only_the_named_files_are_committed_and_everything_else_stays_as_it_was(self):
        (self.root / "untracked.txt").write_text("not part of this\n", encoding="utf-8")
        (self.root / "b.txt").write_text("tracked later\n", encoding="utf-8")
        box = ToolBox(self.root, allow_mutation={"git_commit"})
        proposal = box.call("propose_git_commit", {"paths": ["a.txt"], "message": "change a"})
        result = box.call("apply_proposal", {"proposal_id": proposal["proposal_id"]})
        self.assertIn(result.get("status"), ("APPLIED", "COMMITTED"), result)
        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD"), "a.txt")
        self.assertEqual(sorted(self.git("status", "--porcelain").replace("?? ", "").split()), ["b.txt", "untracked.txt"])
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    def test_committing_is_its_own_grant(self):
        apply_only = ToolBox(self.root, allow_mutation={"apply"})
        self.assertNotIn("propose_git_commit", {s["name"] for s in apply_only.schemas()})
        with self.assertRaises(ValueError):
            apply_only.call("propose_git_commit", {"paths": ["a.txt"], "message": "x"})
        commit_only = ToolBox(self.root, allow_mutation={"git_commit"})
        names = {s["name"] for s in commit_only.schemas()}
        self.assertTrue({"propose_git_commit", "apply_proposal"} <= names)
        self.assertNotIn("propose_undo", names)
        edit = commit_only.call("propose_edit", {"path": "a.txt", "old": "two", "new": "three"})
        with self.assertRaises(ValueError) as caught:
            commit_only.call("apply_proposal", {"proposal_id": edit["proposal_id"]})
        self.assertIn("not granted", str(caught.exception))
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8").strip(), "two")

if __name__ == "__main__":
    unittest.main()
