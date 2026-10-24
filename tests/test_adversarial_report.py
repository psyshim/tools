import io
import unittest
from examples.adversarial_check import EvidenceResult


class ResultRecorderTests(unittest.TestCase):
    def test_failed_skipped_and_subtest_checks_are_not_passes(self):
        class Fixture(unittest.TestCase):
            def case_pass(self):
                self.assertTrue(True)
            def case_fail(self):
                self.fail("intentional fixture failure")
            def case_skip(self):
                self.skipTest("fixture platform capability unavailable")
            def case_subtest(self):
                with self.subTest(item="changed bytes"):
                    self.assertEqual(b"old", b"new")
        names = ("case_pass", "case_fail", "case_skip", "case_subtest")
        suite = unittest.TestSuite(Fixture(name) for name in names)
        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=EvidenceResult).run(suite)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual([r["status"] for r in result.records], ["PASS", "FAIL", "SKIP", "FAIL"])
        self.assertTrue(all(r["seconds"] >= 0 for r in result.records))
