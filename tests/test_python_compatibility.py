"""The declared Python 3.11 floor must not require a 3.12 filesystem API."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from coding_agent.workspace import Workspace, _reparse


class PythonCompatibilityTests(unittest.TestCase):
    def test_scan_works_without_os_path_isjunction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()      # the workspace reports resolved paths; a temp dir may be spelled as an 8.3 alias
            (root / "src").mkdir()
            (root / "src/a.py").write_bytes(b"x = 1\n")
            old = getattr(os.path, "isjunction", None)
            try:
                if old is not None:
                    delattr(os.path, "isjunction")
                self.assertEqual([p.relative_to(root).as_posix() for p in Workspace(root).iter_files()], ["src/a.py"])
            finally:
                if old is not None:
                    os.path.isjunction = old

    def test_reparse_attributes_and_unreadable_paths_are_not_traversed(self):
        with patch.object(Path, "lstat", return_value=SimpleNamespace(st_file_attributes=0x400)):
            self.assertTrue(_reparse(Path("junction")))
        with patch.object(Path, "lstat", side_effect=PermissionError("locked")):
            self.assertTrue(_reparse(Path("locked")))
