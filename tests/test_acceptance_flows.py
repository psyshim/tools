"""Offline end-to-end gates: actual Engine approvals and actual local effects."""
from pathlib import Path
import tempfile
import unittest
from examples.housekeeping_check import housekeeping, git_commit, hostile_encoding


class AcceptanceFlowTests(unittest.TestCase):
    def test_housekeeping_with_eight_approvals(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(housekeeping(Path(directory))["approvals"], 8)

    def test_git_commit_with_approval_and_unrelated_file(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(git_commit(Path(directory))["approvals"], 1)

    def test_hostile_encodings_preserve_or_reject(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(hostile_encoding(Path(directory))["checks"], 9)
