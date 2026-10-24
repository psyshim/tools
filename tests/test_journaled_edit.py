"""Journaled edit API regressions on a disposable workspace."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from coding_agent.journal import OperationConflict, OperationJournal
from coding_agent.workflow import Workflow
from coding_agent.journaled_edit import JournaledEdit

class JournaledEditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.file = self.root / "test.py"
        self.file.write_text("value = 1\n")
        self.database = self.root / "history.sqlite"
        self.journal = OperationJournal(self.database)
        self.facade = JournaledEdit(Workflow(self.root), self.journal)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def test_exactly_once_api_replay_and_reopen(self):
        before = hashlib.sha256(self.file.read_bytes()).hexdigest()
        first = self.facade.replace_text("op1", "test.py", before, "1", "2")
        self.assertEqual(self.file.read_text(), "value = 2\n")
        self.assertEqual(self.facade.replace_text("op1", "test.py", before, "1", "2"), first)
        self.journal.close()
        self.journal = OperationJournal(self.database)
        self.facade = JournaledEdit(Workflow(self.root), self.journal)
        self.assertEqual(self.facade.replace_text("op1", "test.py", before, "1", "2"), first)

    def test_conflicting_same_id_rejected(self):
        before = hashlib.sha256(self.file.read_bytes()).hexdigest()
        self.facade.replace_text("same", "test.py", before, "1", "2")
        with self.assertRaises(OperationConflict):
            self.facade.replace_text("same", "test.py", before, "1", "3")

    def test_unknown_never_runs_again(self):
        before = hashlib.sha256(self.file.read_bytes()).hexdigest()
        request = {"kind": "replace_text_v1", "path": "test.py",
                   "expected_sha256": before, "original": "1", "replacement": "2"}
        self.journal.prepare("pending", request)
        self.journal.mark_uncertain("pending")
        with self.assertRaises(OperationConflict):
            self.facade.replace_text("pending", "test.py", before, "1", "2")
        self.assertEqual(self.file.read_text(), "value = 1\n")

    def test_failed_edit_stays_unknown(self):
        before = hashlib.sha256(self.file.read_bytes()).hexdigest()
        self.file.write_text("human changed\n")
        with self.assertRaises(OperationConflict):
            self.facade.replace_text("failed", "test.py", before, "1", "2")
        self.assertEqual(self.file.read_text(), "human changed\n")
        self.assertEqual(self.journal.prepare("failed", {
            "kind": "replace_text_v1", "path": "test.py",
            "expected_sha256": before, "original": "1", "replacement": "2"})["state"], "UNKNOWN")
