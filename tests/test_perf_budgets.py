"""Speed and weight are requirements with numbers. The stack timings are measured by the delivery gate (they need two processes);
here: the budgets file is sound, the evaluator can say no, and the cheap measurements are really inside their budgets."""
import json
import unittest
from pathlib import Path

from hostkit_build import perf

class Budgets(unittest.TestCase):
    def test_every_limit_is_a_positive_number_with_a_unit_in_its_name(self):
        for name, limit in perf.BUDGETS["limits"].items():
            with self.subTest(name=name):
                self.assertIsInstance(limit, (int, float))
                self.assertGreater(limit, 0)
                self.assertRegex(name, r"(_kib|_ms|_s)(_|$)|_(kib|ms|s)$")
        self.assertGreater(perf.BUDGETS["suite_wall_minutes"], 0)

    def test_the_evaluator_flags_a_breach_and_a_missing_measurement(self):
        limits = {"a_ms": 10, "b_kib": 5}
        self.assertEqual(perf.evaluate({"a_ms": 10, "b_kib": 5}, limits), [], "at the limit is allowed")
        self.assertEqual(perf.evaluate({"a_ms": 11, "b_kib": 1}, limits), [("a_ms", 11, 10)])
        self.assertEqual(perf.evaluate({"a_ms": 1}, limits), [("b_kib", None, 5)], "an unmeasured budget is a violation, not a pass")

    def test_p95_is_the_upper_tail_not_the_mean(self):
        self.assertEqual(perf.p95(list(range(100))), 95)
        self.assertEqual(perf.p95([7]), 7)

class CheapMeasurements(unittest.TestCase):
    def test_the_kit_is_light_and_the_wrapper_adds_almost_nothing(self):
        measured = {**perf.measure_kit(repeats=3), **perf.measure_wrapper(calls=60)}
        limits = {k: v for k, v in perf.BUDGETS["limits"].items() if k in measured}
        self.assertEqual(set(limits), {"kit_wheel_kib", "kit_unpacked_kib", "kit_import_ms_best", "wrapper_start_ms_p95", "wrapper_events_ms_p95"})
        self.assertEqual(perf.evaluate(measured, limits), [], measured)

    def test_the_budget_file_is_the_one_the_gate_reads(self):
        raw = json.loads((Path(perf.__file__).with_name("perf_budgets.json")).read_text(encoding="utf-8"))
        self.assertEqual(raw["limits"], perf.BUDGETS["limits"])

if __name__ == "__main__":
    unittest.main()
