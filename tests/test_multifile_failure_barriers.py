"""Fault-injected batches and an independent oracle for incomplete refactors."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from code_attach.tools import ToolBox


class MultifileFailureBarriers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("a.py", "b.py"):
            (self.root / name).write_bytes(b"value = 1\n")
        self.box = ToolBox(self.root)

    def proposal(self):
        return self.box.t_propose_edits([
            {"path": name, "old": "value = 1", "new": "value = 2"}
            for name in ("a.py", "b.py")
        ])["proposal_id"]

    def test_second_write_failure_restores_first_file(self):
        pid = self.proposal()
        original = self.box.flow.replace_bytes

        def fail_second(path, sha, data):
            if path == "b.py":
                raise PermissionError("injected locked file")
            return original(path, sha, data)

        with patch.object(self.box.flow, "replace_bytes", side_effect=fail_second):
            result = self.box.t_apply_proposal(pid)
        self.assertEqual(result["status"], "CONFLICT")
        for name in ("a.py", "b.py"):
            self.assertEqual((self.root / name).read_bytes(), b"value = 1\n")

    def test_human_edit_during_failure_survives_failed_rollback(self):
        pid = self.proposal()
        original = self.box.flow.replace_bytes

        def interfere(path, sha, data):
            if path == "b.py":
                (self.root / "a.py").write_bytes(b"value = 99 # human\n")
                raise PermissionError("injected locked file")
            return original(path, sha, data)

        with patch.object(self.box.flow, "replace_bytes", side_effect=interfere):
            result = self.box.t_apply_proposal(pid)
        self.assertEqual(result["status"], "NEEDS_RECONCILIATION")
        self.assertEqual(result["inconsistent_paths"], ["a.py"])
        self.assertEqual((self.root / "a.py").read_bytes(), b"value = 99 # human\n")
        self.assertEqual((self.root / "b.py").read_bytes(), b"value = 1\n")

    def test_stale_second_file_prevents_any_write(self):
        pid = self.proposal()
        (self.root / "b.py").write_bytes(b"value = 88 # human\n")
        result = self.box.t_apply_proposal(pid)
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual((self.root / "a.py").read_bytes(), b"value = 1\n")
        self.assertEqual((self.root / "b.py").read_bytes(), b"value = 88 # human\n")

    def test_syntax_success_does_not_prove_reference_rename_complete(self):
        (self.root / "api.py").write_text("def old_name():\n    return 42\n", encoding="utf-8")
        (self.root / "consumer.py").write_text("from api import old_name\nassert old_name() == 42\n", encoding="utf-8")
        pid = self.box.t_propose_edits([
            {"path": "api.py", "old": "def old_name():", "new": "def new_name():"}
        ])["proposal_id"]
        self.assertEqual(self.box.t_apply_proposal(pid)["status"], "APPLIED")
        self.assertEqual(self.box.flow.test_python_syntax("api.py")["status"], "PASS")
        failed = subprocess.run([sys.executable, "consumer.py"], cwd=self.root, capture_output=True, timeout=15)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn(b"ImportError", failed.stderr)
        pid = self.box.t_propose_edits([
            {"path": "consumer.py", "old": "from api import old_name\nassert old_name() == 42",
             "new": "from api import new_name\nassert new_name() == 42"}
        ])["proposal_id"]
        self.assertEqual(self.box.t_apply_proposal(pid)["status"], "APPLIED")
        passed = subprocess.run([sys.executable, "consumer.py"], cwd=self.root, capture_output=True, timeout=15)
        self.assertEqual(passed.returncode, 0, passed.stderr)
