"""Run the contract and conformance suites and record what they actually proved, tagged with the exact revision.

    python -m examples.conformance_evidence [--output evidence.json]

Every test gets one of PASS / FAIL / ERROR / UNSUPPORTED (a scenario an implementation honestly cannot prove). Nothing is
inferred: the report is built from the test runner's own results. Commit a report only from a clean tree; CI uploads one per run.
"""
import argparse
import io
import json
import platform
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ["tests.test_protocol_contract", "tests.test_protocol_conformance",     # the contract, mock + real service, in-process and as separate processes
           "tests.test_host_client", "tests.test_sample_host", "tests.test_host_wrapper",                  # the host side: client behaviour and a complete adapter
           "tests.test_device_agent", "tests.test_device_effect_uncertainty",   # the device: grants, journal, uncertain outcomes
           "tests.test_service_device_processes", "tests.test_service_recovery",  # isolation and restart/kill recovery across processes
           "tests.test_experimental_mutation_stack", "tests.test_effect_semantics", "tests.test_write_guard"]  # effects, crash points, write guard

class Recording(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rows = []

    def _row(self, test, outcome, reason=""):
        self.rows.append({"id": test.id(), "outcome": outcome, "reason": str(reason)[:300]})

    def addSuccess(self, test):
        super().addSuccess(test)
        self._row(test, "PASS")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._row(test, "FAIL", err[1])

    def addError(self, test, err):
        super().addError(test, err)
        self._row(test, "ERROR", err[1])

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._row(test, "UNSUPPORTED" if str(reason).startswith("UNSUPPORTED") else "SKIPPED", reason)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._row(subtest, "FAIL" if issubclass(err[0], AssertionError) else "ERROR", err[1])

def git(*args):
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(m) for m in MODULES)
    started = datetime.now(timezone.utc)
    result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=Recording, verbosity=0).run(suite)
    summary = {}
    for row in result.rows:
        summary[row["outcome"]] = summary.get(row["outcome"], 0) + 1
    sha, dirty = git("rev-parse", "HEAD"), bool(git("status", "--porcelain", "--untracked-files=no"))
    report = {"revision": sha, "dirty_tracked_files": dirty, "branch": git("branch", "--show-current"), "python": platform.python_version(), "platform": platform.platform(),
              "command": "python -m examples.conformance_evidence", "started": started.isoformat(timespec="seconds"), "ran": result.testsRun, "summary": summary, "results": result.rows}
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(f"revision {sha[:10] or 'unknown'}{' (tracked files modified)' if dirty else ''}: " + ", ".join(f"{k}={v}" for k, v in sorted(summary.items())))
    return 0 if not summary.get("FAIL") and not summary.get("ERROR") else 1

if __name__ == "__main__":
    raise SystemExit(main())
