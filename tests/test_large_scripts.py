"""The same offline acceptance matrix is available in installed host examples."""
import unittest
from examples.stress_check import CASES, run_case


class LargeScriptTests(unittest.TestCase):
    pass


def case_test(name):
    def test(self):
        result = run_case(name)
        self.assertEqual(result["status"], "PASS", result)
    return test


for name in CASES:
    setattr(LargeScriptTests, "test_" + name, case_test(name))
