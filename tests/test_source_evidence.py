from pathlib import Path
import tempfile
import unittest
from examples.adversarial_check import source_snapshot


class SourceEvidenceTests(unittest.TestCase):
    def test_uncommitted_source_changes_are_detected_without_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "coding_service").mkdir()
            path = root / "coding_service" / "module.py"
            path.write_bytes(b"value = 1\n")
            before = source_snapshot(root)
            self.assertEqual(list(before), ["coding_service/module.py"])
            self.assertEqual(source_snapshot(root), before)
            path.write_bytes(b"value = 2\n")
            self.assertNotEqual(source_snapshot(root), before)
