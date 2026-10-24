import hashlib
import tempfile
import unittest
from pathlib import Path
from coding_agent.patching import SafeEditor, PatchConflict
from coding_agent.workspace import Workspace

class PatchingTests(unittest.TestCase):
    def test_replace_and_reject_stale(self):
        with tempfile.TemporaryDirectory() as d:
            file = Path(d) / "x.txt"
            file.write_text("old", encoding="utf-8")
            editor = SafeEditor(Workspace(d))
            old_hash = hashlib.sha256(b"old").hexdigest()
            result = editor.replace("x.txt", old_hash, "new")
            self.assertEqual(file.read_text(), "new")
            self.assertEqual(result["sha256"], hashlib.sha256(b"new").hexdigest())
            with self.assertRaises(PatchConflict):
                editor.replace("x.txt", old_hash, "overwrite")
            self.assertEqual(file.read_text(), "new")

    def test_reject_escape(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                SafeEditor(Workspace(d)).replace("../secret.txt", "", "wrong")

    def test_reject_symlink(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / "secret.txt"
            secret.write_text("secret")
            try:
                (Path(d) / "link.txt").symlink_to(secret)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows symlink privilege unavailable")
                raise
            editor = SafeEditor(Workspace(d))
            digest = hashlib.sha256(b"secret").hexdigest()
            with self.assertRaises(ValueError):
                editor.replace("link.txt", digest, "wrong")
            self.assertEqual(secret.read_text(), "secret")
