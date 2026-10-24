import hashlib
import tempfile
import unittest
from pathlib import Path
from coding_agent.workflow import Conflict, Workflow

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.file = self.root / "example.py"
        self.file.write_text("def value():\n    return 1\n", encoding="utf-8")
        self.engine = Workflow(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_edit_and_syntax_check(self):
        digest = self.engine.inspect("example.py")["sha256"]
        result = self.engine.replace_text("example.py", digest, "return 1", "return 2")
        self.assertEqual(result["sha256"], hashlib.sha256(self.file.read_bytes()).hexdigest())
        self.assertEqual(self.engine.test_python_syntax("example.py")["status"], "PASS")
        self.assertIn("return 2", self.file.read_text())

    def test_stale_hash_does_not_overwrite(self):
        digest = self.engine.inspect("example.py")["sha256"]
        self.file.write_text("human edit")
        with self.assertRaises(Conflict):
            self.engine.replace_text("example.py", digest, "return 1", "return 2")
        self.assertEqual(self.file.read_text(), "human edit")

    def test_ambiguous_edit_rejected(self):
        digest = self.engine.inspect("example.py")["sha256"]
        with self.assertRaises(Conflict):
            self.engine.replace_text("example.py", digest, " ", "!")

    def test_escape_and_git_metadata_rejected(self):
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("x")
        for path in ("../outside", ".git/config"):
            with self.assertRaises(ValueError):
                self.engine.inspect(path)

    def test_bad_syntax_reports_failure(self):
        digest = self.engine.inspect("example.py")["sha256"]
        self.engine.replace_text("example.py", digest, "return 1", "return (")
        self.assertEqual(self.engine.test_python_syntax("example.py")["status"], "FAIL")

    def test_outside_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            target = Path(other) / "secret"
            target.write_text("secret")
            try:
                (self.root / "link.py").symlink_to(target)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows symlink privilege unavailable")
                raise
            with self.assertRaises(ValueError):
                self.engine.inspect("link.py")

if __name__ == "__main__":
    unittest.main()
