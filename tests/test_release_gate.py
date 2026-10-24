"""The delivery gate must be able to say no. Each test damages a throw-away copy of the project in one specific way and expects the
matching check (and the overall verdict) to fail; the undamaged copy passes every check that does not need git or the full suite."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPIED = ("docs", "coding_service", "device_agent", "coding_agent", "code_attach", "protocol", "packages", "testkit", "hostkit_build", "examples", "tests", "kpi")


def gate(mutate=None):
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "project"
        for name in COPIED:
            shutil.copytree(ROOT / name, copy / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if mutate:
            mutate(copy)
        out = Path(tmp) / "receipt.json"
        done = subprocess.run([sys.executable, "-m", "hostkit_build.release_gate", "--allow-dirty", "--output", str(out)], cwd=copy, capture_output=True, text=True, timeout=300)
        receipt = json.loads(out.read_text(encoding="utf-8"))
        return done.returncode, {c["check"]: c for c in receipt["checks"]}, receipt


def edit(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert old in text, (path, old)
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


class GateSaysNo(unittest.TestCase):
    def failing(self, mutate):
        code, checks, receipt = gate(mutate)
        self.assertEqual(code, 1)
        self.assertFalse(receipt["delivery_ok"])
        return {name for name, c in checks.items() if c["outcome"] == "FAIL"}

    def test_the_undamaged_copy_passes_everything_that_does_not_need_git_or_the_full_suite(self):
        code, checks, receipt = gate()
        failed = {name for name, c in checks.items() if c["outcome"] == "FAIL"}
        self.assertEqual(failed, {"the working tree is clean and the revision is known", "the complete test suite", "speed and weight are within the budgets"}, failed)
        self.assertEqual(code, 1, "no revision and no full run: never deliverable")
        self.assertTrue(receipt["host_kit_sha256"])
        self.assertTrue(receipt["not_covered"], "the receipt must say what it does not cover")

    def test_a_personal_path_in_the_kit_is_caught(self):
        failed = self.failing(lambda c: edit(c / "packages/host_client/host_client/events.py", "MAX_TEXT = 20000", "MAX_TEXT = 20000\nSECRET_HOME = 'C:/Users/realperson/work'"))
        self.assertIn("the host kit contains nothing powerful and nothing personal", failed)

    def test_a_powerful_import_in_the_kit_is_caught(self):
        failed = self.failing(lambda c: edit(c / "packages/host_client/host_client/events.py", "import html", "import html\nimport subprocess"))
        self.assertIn("the host kit contains nothing powerful and nothing personal", failed)

    def test_a_document_that_names_a_missing_file_is_caught(self):
        failed = self.failing(lambda c: edit(c / "docs/HOST_KIT.md", "# Host kit", "# Host kit\n\nSee `docs/NO_SUCH_FILE.md`.\n"))
        self.assertIn("documents refer only to files that exist", failed)

    def test_a_configuration_example_the_loaders_reject_is_caught(self):
        failed = self.failing(lambda c: edit(c / "docs/OPERATOR_GUIDE.md", '"device_id": "laptop-1",', '"device_id": "laptop-1", "surprise_key": true,'))
        self.assertIn("every configuration example in the operator guide is accepted by the real loaders", failed)

    def test_an_unsafe_default_is_caught(self):
        def unsafe(copy):
            edit(copy / "coding_service/config.py", 'allow = raw.get("allow_mutation", False)', 'allow = raw.get("allow_mutation", True)')
        grep = (ROOT / "coding_service/config.py").read_text(encoding="utf-8")
        self.assertIn('allow = raw.get("allow_mutation", False)', grep, "the mutation anchor moved: update this test")
        failed = self.failing(unsafe)
        self.assertIn("the default installation cannot change anything", failed)


if __name__ == "__main__":
    unittest.main()
