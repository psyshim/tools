"""A failed write must not publish a partial new/copy/restored file."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from coding_agent.file_ops import FileOperations


class NewFileFailureTests(unittest.TestCase):
    def test_fsync_failure_leaves_no_partial_destination_or_staging_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "new.bin"
            with patch("coding_agent.file_ops.os.fsync", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    FileOperations._write_exclusive(target, b"new contents")
            self.assertEqual(list(root.iterdir()), [])

    def test_existing_destination_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "new.bin"
            target.write_bytes(b"human")
            with self.assertRaises(FileExistsError):
                FileOperations._write_exclusive(target, b"agent")
            self.assertEqual(target.read_bytes(), b"human")
            self.assertEqual(list(root.iterdir()), [target])

    def test_bytes_are_published_completely_and_staging_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "new.bin"
            data = bytes(range(256)) * 2048
            FileOperations._write_exclusive(target, data)
            self.assertEqual(target.read_bytes(), data)
            self.assertEqual(list(root.iterdir()), [target])
