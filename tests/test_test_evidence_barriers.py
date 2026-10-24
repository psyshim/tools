"""Zero-exit wrappers and empty discovery do not establish passing tests."""
from pathlib import Path
import sys
import tempfile
import unittest

from code_attach.testrun import CommandRunner


class TestEvidenceBarriers(unittest.TestCase):
    def run_fixture(self, source, argv=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_fixture.py").write_text(source, encoding="utf-8")
            runner = CommandRunner(root, {"test": [sys.executable, *(argv or ["-m", "unittest", "discover"])]}, 15, {}, lambda s: s)
            return runner.run("test")

    def test_zero_discovered_tests_is_not_pass(self):
        result = self.run_fixture("x = 1\n", ["-c", "import unittest; unittest.TextTestRunner().run(unittest.TestSuite())"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["status"], "NO_TESTS")

    def test_all_skipped_is_not_pass(self):
        source = "import unittest\n@unittest.skip('dependency absent')\nclass Test(unittest.TestCase):\n    def test_one(self): pass\n"
        result = self.run_fixture(source)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["status"], "ALL_SKIPPED")

    def test_wrapper_hiding_failed_test_exit_is_inconsistent(self):
        source = "import unittest\nclass Test(unittest.TestCase):\n    def test_one(self): self.fail('actual failure')\nunittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(Test))\n"
        result = self.run_fixture(source, ["test_fixture.py"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["status"], "INCONSISTENT")
        self.assertEqual(result["verification"], "REPORTED_FAILURES")

    def test_real_passing_test_and_generic_command_are_distinguished(self):
        passed = self.run_fixture("import unittest\nclass Test(unittest.TestCase):\n    def test_one(self): self.assertEqual(2 + 2, 4)\n")
        self.assertEqual((passed["status"], passed["verification"]), ("PASS", "RUNNER_SUMMARY"))
        command = self.run_fixture("print('build complete')\n", ["test_fixture.py"])
        self.assertEqual((command["status"], command["verification"]), ("PASS", "COMMAND_EXIT_ONLY"))
