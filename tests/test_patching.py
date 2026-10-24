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

    def test_reject_escape_and_symlink(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / "secret.txt"
            secret.write_text("secret")
            (Path(d) / "link.txt").symlink_to(secret)
            editor = SafeEditor(Workspace(d))
            digest = hashlib.sha256(b"secret").hexdigest()
            for p in ("../secret.txt", "link.txt"):
                with self.assertRaises(ValueError):
                    editor.replace(p, digest, "wrong")
            self.assertEqual(secret.read_text(), "secret")
