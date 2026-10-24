"""Run the source checkout's offline adversarial gates and save verifiable JSON.

python -m examples.adversarial_check --output adversarial-report.json
No model credentials are needed. Tests use new temporary fixture projects.
"""
import argparse
import hashlib
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import time
import unittest

GATES = {
    "component_logs_and_usage_management": ("test_usage_accounting.", "test_component_management.", "test_usage_process_stack.", "test_state_backup.", "test_store_schema_migration.", "test_restore_process_stack."),
    "malformed_model_json": ("test_json_corruption.", "test_json_boundaries.", "test_json_file_repair.", "test_model_call_identity."),
    "multifile_failure_and_reference_oracle": ("test_multifile_failure_barriers.",),
    "workspace_boundary": ("test_workspace.", "test_security_boundary_regression.", "test_housekeeping."),
    "discovery_and_large_sources": ("test_code_attach_project.", "test_large_scripts."),
    "hostile_encodings": ("test_encoding_adversarial.",),
    "approval_ownership_and_conflicts": ("test_code_attach.", "test_code_attach_abnormal.", "test_mock_actor_isolation."),
    "file_housekeeping_and_recovery": ("test_housekeeping.", "test_new_file_failure.", "test_acceptance_flows.AcceptanceFlowTests.test_housekeeping"),
    "git_selected_commit_and_interference": ("test_git_ops.", "test_git_adversarial.", "test_acceptance_flows.AcceptanceFlowTests.test_git"),
    "process_environment_and_budgets": ("test_code_attach_audit.", "test_code_attach_audit2.", "test_bounded_process_output.", "test_execution_owner.", "test_test_evidence_barriers."),
    "privacy": ("test_code_attach_privacy.",),
    "durable_logs_and_unknown_outcomes": ("test_local_app.", "test_journal.", "test_journaled_edit.", "test_adversarial_recovery.", "test_durable_store_faults.", "test_manager_interruption.", "test_service_recovery.", "test_device_effect_uncertainty.", "test_corrupt_state_records.", "test_adversarial_report."),
    "architecture_and_protocol_errors": ("test_architecture_boundaries.", "test_code_attach_boundaries.", "test_fault_service.", "test_source_evidence.", "test_python_compatibility.", "test_protocol_byte_budgets."),
}


def source_snapshot(root):
    """Pin the actual sources, including uncommitted changes in a shared checkout."""
    files = {}
    for name in ("code_attach", "coding_agent", "coding_service", "device_agent", "protocol", "tests", "examples",
                 "packages", "hostkit_build", "host_mock", "testkit", "kpi"):
        for path in sorted((root / name).rglob("*.py")):
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self._started = {}
        self._status = {}

    def wasSuccessful(self):
        return super().wasSuccessful() and all(row["status"] in {"PASS", "SKIP"} for row in self.records)

    def startTest(self, test):
        self._started[test.id()] = time.monotonic()
        super().startTest(test)

    def addSuccess(self, test):
        self._status[test.id()] = ("PASS", None)
        super().addSuccess(test)

    def addFailure(self, test, err):
        self._status[test.id()] = ("FAIL", type(err[1]).__name__)
        super().addFailure(test, err)

    def addError(self, test, err):
        self._status[test.id()] = ("ERROR", type(err[1]).__name__)
        super().addError(test, err)
        self._outside_test(test, "ERROR", type(err[1]).__name__)

    def addSkip(self, test, reason):
        self._status[test.id()] = ("SKIP", reason)
        super().addSkip(test, reason)
        self._outside_test(test, "SKIP", reason)

    def _outside_test(self, test, status, detail):
        if test.id() not in self._started:
            identifier = test.id()
            target = identifier.partition(" (")[2].removesuffix(")") or identifier
            self.records.append({"test": identifier, "fixture_target": target, "scope": "fixture",
                                 "status": status, "seconds": 0, "detail": str(detail)})

    def addExpectedFailure(self, test, err):
        # A known failing requirement is not acceptance evidence merely because
        # unittest's decorator changes its exit-code interpretation.
        self._status[test.id()] = ("FAIL", "expected_failure: " + type(err[1]).__name__)
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self._status[test.id()] = ("FAIL", "unexpected_success: remove the stale marker and reverify")
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._status[test.id()] = ("FAIL", type(err[1]).__name__)
        super().addSubTest(test, subtest, err)

    def stopTest(self, test):
        status, detail = self._status.get(test.id(), ("UNMEASURED", None))
        self.records.append({"test": test.id(), "status": status,
                             "seconds": round(time.monotonic() - self._started[test.id()], 4),
                             **({"detail": detail} if detail else {})})
        super().stopTest(test)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("adversarial-report.json"))
    parser.add_argument("--tests-dir", type=Path, default=Path(__file__).resolve().parents[1] / "tests")
    args = parser.parse_args()
    if not args.tests_dir.is_dir():
        parser.error("source tests are unavailable; run from the checkout or supply --tests-dir")
    root = Path(__file__).resolve().parents[1]
    before = source_snapshot(root)
    suite = unittest.TestLoader().discover(str(args.tests_dir.resolve()))
    result = unittest.TextTestRunner(verbosity=2, resultclass=EvidenceResult).run(suite)
    after = source_snapshot(root)
    changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    gates = []
    for name, prefixes in GATES.items():
        records = [r for r in result.records if r.get("fixture_target", r["test"]).startswith(prefixes)]
        counts = {s: sum(r["status"] == s for r in records) for s in ("PASS", "FAIL", "ERROR", "SKIP", "UNMEASURED")}
        status = "NOT_COVERED" if not records else "FAIL" if counts["FAIL"] or counts["ERROR"] or counts["UNMEASURED"] else "PARTIAL" if counts["SKIP"] else "PASS"
        gates.append({"gate": name, "status": status, **counts})
    covered = all(g["status"] not in {"FAIL", "NOT_COVERED"} for g in gates)
    success = result.wasSuccessful() and covered and not changed and all(r["status"] in {"PASS", "SKIP"} for r in result.records)
    report = {"schema": 1, "timestamp": datetime.now(timezone.utc).isoformat(),
              "environment": {"python": platform.python_version(), "os": platform.system()},
              "evidence": "offline tool/runtime acceptance; actual filesystem, subprocess and Git assertions; not model reasoning or prompt-injection evaluation",
              "source_files_sha256": before, "source_changed_during_run": changed,
              "status": "FAIL" if not success else "PASS_WITH_SKIPS" if result.skipped else "PASS",
              "tests_run": result.testsRun, "gates": gates, "tests": result.records,
              "unsupported": ["network/push/force/reset/clean", "merge/rebase/cherry-pick", "OS privilege sandbox",
                              "automatic crash recovery/resume", "real-model reasoning and repository prompt-injection resistance"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Adversarial report:", args.output.resolve())
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
