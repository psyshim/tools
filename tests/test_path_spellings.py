"""A directory has several spellings; text that leaves the process must not contain any of them (found on a Windows CI runner whose
temp directory was spelled C:\\Users\\RUNNER~1\\... while the workspace resolved to C:\\Users\\runneradmin\\...)."""
import os
import tempfile
import unittest
from pathlib import Path

from code_attach.privacy import Scrubber
from coding_agent.path_spellings import spellings


class Spellings(unittest.TestCase):
    def test_the_usual_forms_are_all_covered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            forms = set(spellings(root))
            self.assertTrue({str(root), root.as_posix(), str(root).replace(chr(92), chr(92) * 2)} <= forms)
            self.assertEqual(sorted(forms, key=len, reverse=True), spellings(root), "longest first so a prefix never shadows its extension")

    def test_a_missing_path_does_not_raise(self):
        self.assertTrue(spellings(Path("no/such/place")))

    @unittest.skipUnless(os.name == "nt", "8.3 short names are a Windows feature")
    def test_the_short_and_long_windows_spellings_are_both_scrubbed(self):
        with tempfile.TemporaryDirectory() as tmp:
            long_dir = Path(tmp).resolve() / "a_directory_with_a_long_name"
            long_dir.mkdir()
            from coding_agent.path_spellings import _windows_alias
            short = _windows_alias(long_dir, "GetShortPathNameW")
            if not short or short.lower() == str(long_dir).lower():
                self.skipTest("8.3 names are disabled on this volume")
            scrubber = Scrubber(long_dir)
            for spelling in (short, str(long_dir), short.replace(chr(92), "/"), str(long_dir).upper()):
                text = scrubber.scrub(f"error at {spelling} line 3")
                self.assertNotIn(Path(spelling).name.lower(), text.lower(), spelling)
                self.assertIn("<workspace>", text)
            as_given = Scrubber(Path(short))                       # the root handed over in its short spelling
            self.assertNotIn("a_directory_with", as_given.scrub(f"{long_dir} and {short}").lower())


if __name__ == "__main__":
    unittest.main()
