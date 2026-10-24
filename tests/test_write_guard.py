"""Phase 6 evidence: what the apply path does when a human saves at the worst moment. Deterministic race injection at the file
update boundary. Windows-only where the guard exists; the POSIX gap is stated, not hidden."""
import os
import tempfile
import threading
import unittest
from pathlib import Path

import coding_agent.write_guard as wg
from coding_agent.workflow import Conflict, Workflow

WINDOWS = os.name == "nt"

class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "a.txt"
        self.path.write_text("alpha\n", encoding="utf-8")
        self.flow = Workflow(self.root)
        self.addCleanup(wg.TEST_HOOKS.clear)

    def sha(self):
        return self.flow.inspect("a.txt")["sha256"]

class RaceInjection(Fixture):
    def test_a_change_before_the_final_check_is_detected_and_nothing_is_overwritten(self):
        sha = self.sha()
        self.path.write_text("human edit\n", encoding="utf-8")      # the human saved after we computed the edit
        with self.assertRaises(Conflict):
            self.flow.replace_bytes("a.txt", sha, b"ours\n")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "human edit\n")

    @unittest.skipUnless(WINDOWS, "the write guard is a Windows mechanism")
    def test_a_save_in_the_window_between_check_and_replace_is_refused_not_lost(self):
        outcome = {}
        def human_saves_now():
            try:
                with open(self.path, "w", encoding="utf-8") as handle:     # what an editor's normal save does
                    handle.write("human edit\n")
                outcome["human"] = "written"
            except OSError as exc:
                outcome["human"] = type(exc).__name__
        wg.TEST_HOOKS["after_check"] = human_saves_now
        self.flow.replace_bytes("a.txt", self.sha(), b"ours\n")
        self.assertEqual(outcome["human"], "PermissionError", "the editor's save fails visibly instead of being overwritten later")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "ours\n")
        self.path.write_text("human retries after the failure\n", encoding="utf-8")     # once we are done they can save again
        self.assertEqual(self.path.read_text(encoding="utf-8"), "human retries after the failure\n")

    @unittest.skipUnless(WINDOWS, "the write guard is a Windows mechanism")
    def test_a_file_already_open_for_writing_elsewhere_is_a_conflict_not_a_clobber(self):
        sha = self.sha()
        with open(self.path, "r+b") as other_program:
            other_program.read(1)
            with self.assertRaises(Conflict) as caught:
                self.flow.replace_bytes("a.txt", sha, b"ours\n")
            self.assertIn("open for writing", str(caught.exception))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "alpha\n")

    @unittest.skipUnless(WINDOWS, "the write guard is a Windows mechanism")
    def test_no_temporary_files_are_left_behind_after_a_refused_or_failed_apply(self):
        sha = self.sha()
        with open(self.path, "r+b"):
            with self.assertRaises(Conflict):
                self.flow.replace_bytes("a.txt", sha, b"ours\n")
        self.assertEqual([p.name for p in self.root.iterdir()], ["a.txt"])

    @unittest.skipUnless(WINDOWS, "the write guard is a Windows mechanism")
    def test_readers_are_never_blocked(self):
        seen = []
        wg.TEST_HOOKS["after_check"] = lambda: seen.append(self.path.read_bytes())
        self.flow.replace_bytes("a.txt", self.sha(), b"ours\n")
        self.assertEqual(seen, [b"alpha" + os.linesep.encode()])

    @unittest.skipUnless(WINDOWS, "the write guard is a Windows mechanism")
    def test_concurrent_apply_attempts_never_interleave(self):
        results, gate = [], threading.Barrier(4)
        sha = self.sha()
        def go(n):
            gate.wait()
            try:
                self.flow.replace_bytes("a.txt", sha, f"writer {n}\n".encode())
                results.append("applied")
            except Conflict:
                results.append("conflict")
        threads = [threading.Thread(target=go, args=(n,)) for n in range(4)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        self.assertEqual(sorted(results), ["applied", "conflict", "conflict", "conflict"])
        self.assertTrue(self.path.read_text(encoding="utf-8").startswith("writer "))

class StatedLimits(unittest.TestCase):
    def test_the_module_documents_the_residual_risks(self):
        text = wg.__doc__
        self.assertIn("renaming it OVER the target", text)
        self.assertIn("POSIX", text)

    def test_hooks_are_inert_by_default(self):
        self.assertEqual(wg.TEST_HOOKS, {})

if __name__ == "__main__":
    unittest.main()
