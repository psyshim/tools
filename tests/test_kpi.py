"""Tests for the KPI machinery and the real-model eval harness (mock models only)."""
import unittest
from kpi.kpi import LEVELS, evaluate, pct
from kpi.real_eval import SCENARIOS, liar_model, oracle_retry_model, run_scenario

class RealEvalHarnessTests(unittest.TestCase):
    def scenario(self, sid):
        return next(s for s in SCENARIOS if s["id"] == sid)

    def test_liar_model_is_scored_fail(self):
        for sc in SCENARIOS:
            with self.subTest(scenario=sc["id"]):
                rec = run_scenario(liar_model, sc, timeout=60)
                # the liar happens to mention "Dockerfile" so the honesty scenario may pass; every change scenario must fail
                if sc["id"] != "absent_thing_no_change":
                    self.assertFalse(rec["passed"], rec)

    def test_positive_control_passes(self):
        rec = run_scenario(oracle_retry_model, self.scenario("retry_limit_hidden_config"), timeout=60)
        self.assertTrue(rec["passed"], rec)
        self.assertEqual(rec["changed"], ["deploy/legacy/settings_old.ini"])

    def test_collateral_change_fails_scenario(self):
        # the same oracle edits the ini; under a scenario that allows no changes this must fail
        sc = {**self.scenario("retry_limit_hidden_config"), "allowed": set()}
        rec = run_scenario(oracle_retry_model, sc, timeout=60)
        self.assertFalse(rec["passed"])
        self.assertIn("collateral", rec["detail"])

class KpiMathTests(unittest.TestCase):
    ITEM = {"doc": ["docs/ATTACH_V0.md", "Layering"], "code": [["code_attach/engine.py", "class Engine"]], "tests": ["t.ok"], "integration": None}

    def test_levels_are_computed_from_evidence(self):
        self.assertEqual(evaluate(self.ITEM, {"t.ok": "PASS"})[0], "TESTED")
        self.assertEqual(evaluate(self.ITEM, {"t.ok": "FAIL"})[0], "IMPLEMENTED")
        self.assertEqual(evaluate({**self.ITEM, "code": [["code_attach/engine.py", "no_such_symbol"]]}, {"t.ok": "PASS"})[0], "SPECIFIED")
        self.assertEqual(evaluate({**self.ITEM, "doc": ["docs/ATTACH_V0.md", "no such anchor"]}, {"t.ok": "PASS"})[0], "NONE")

    def test_integrated_needs_real_evidence_file(self):
        item = {**self.ITEM, "integration": "kpi/evidence/does_not_exist.md"}
        level, missing, max_idx = evaluate(item, {"t.ok": "PASS"})
        self.assertEqual((level, max_idx), ("TESTED", 4))
        self.assertIn("integration evidence", missing)

    def test_item_without_tests_cannot_be_tested(self):
        self.assertEqual(evaluate({**self.ITEM, "tests": []}, {})[0], "IMPLEMENTED")

    def test_pct_handles_zero_denominator(self):
        self.assertEqual(pct(1, 0), "n/a")
        self.assertEqual(pct(1, 4), "25.0%")
        self.assertEqual(LEVELS[0], "NONE")

if __name__ == "__main__":
    unittest.main()
