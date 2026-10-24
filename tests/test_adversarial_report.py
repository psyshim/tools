import io
import unittest
from examples.adversarial_check import EvidenceResult


class ResultRecorderTests(unittest.TestCase):
    def test_class_fixture_errors_and_skips_are_not_lost_outside_start_stop(self):
        class Broken(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise OSError("fixture startup failed")
            def test_never_runs(self):
                self.fail("unreachable")
        class Unavailable(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise unittest.SkipTest("platform unavailable")
            def test_never_runs(self):
                self.fail("unreachable")
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls) for cls in (Broken, Unavailable))
        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=EvidenceResult).run(suite)
        self.assertEqual(result.testsRun, 0)
        self.assertEqual([r["status"] for r in result.records], ["ERROR", "SKIP"])
        self.assertTrue(all(r["scope"] == "fixture" and r["fixture_target"] for r in result.records))
        self.assertFalse(result.wasSuccessful())

    def test_expected_failure_decorator_cannot_make_acceptance_evidence_pass(self):
        class KnownBug(unittest.TestCase):
            @unittest.expectedFailure
            def test_bug(self):
                self.fail("still broken")
            @unittest.expectedFailure
            def test_stale_marker(self):
                pass
        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=EvidenceResult).run(unittest.defaultTestLoader.loadTestsFromTestCase(KnownBug))
        self.assertEqual([r["status"] for r in result.records], ["FAIL", "FAIL"])
        self.assertIn("expected_failure", result.records[0]["detail"])
        self.assertIn("unexpected_success", result.records[1]["detail"])
        expected_only = unittest.TextTestRunner(stream=io.StringIO(), resultclass=EvidenceResult).run(unittest.TestSuite([KnownBug("test_bug")]))
        self.assertFalse(expected_only.wasSuccessful())

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
