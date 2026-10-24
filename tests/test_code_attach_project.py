"""A virtual VS Code project on disk: do edits land, can tests be written and run, can things be found in unexpected places?

The 'model' is scripted, so this proves the tools make each step possible and safe, not that a real LLM will choose them.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from code_attach.engine import Engine

PY = sys.executable
CR, LF = chr(13), chr(10)

from kpi.fixture import FILES, build  # noqa: E402

def tc(i, tool, **args):
    return {"id": f"c{i}", "name": tool, "args": args}

def last(messages):
    return json.loads(messages[-1]["content"])

def script(*turns):
    it = iter(turns)
    return lambda messages, tools: (lambda t: t(messages) if callable(t) else t)(next(it))

class Project:
    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        build(self.root)
        test.addCleanup(self.close)
        self.id = self.engine = None

    def run(self, complete, approve=True, tests=None):
        self.engine = Engine(complete, tests if tests is not None else {"unit": [PY, "-m", "unittest", "discover", "-s", "tests"]}, test_timeout=60)
        self.id, _ = self.engine.start("me", self.root, "k", "task")
        seen = 0
        for _ in range(1500):
            r = self.engine.events("me", self.id, seen)
            for e in r["events"]:
                seen += 1
                if e["type"] == "approval_required":
                    self.engine.decide("me", self.id, e["data"]["action_id"], e["data"]["fingerprint"], approve)
            if r["terminal"] and not r["events"]:
                self.events = self.engine.events("me", self.id)["events"]
                return r["state"]
            time.sleep(0.01)
        raise AssertionError("did not finish")

    def results(self, tool):
        return [e["data"]["result"] for e in self.events if e["type"] == "tool_result" and e["data"]["tool"] == tool and "result" in e["data"]]

    def read(self, rel):
        return (self.root / rel).read_text(encoding="utf-8")

    def close(self):
        if self.engine and self.id:
            self.engine.cancel("me", self.id)
            self.engine.join("me", self.id)
        self.tmp.cleanup()

REGRESSION = (
    "import sys, unittest\nfrom pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n"
    "from shop.orders.pricing import apply_discount\n\n"
    "class PricingRegression(unittest.TestCase):\n"
    "    def test_ten_percent_off(self):\n"
    "        self.assertEqual(apply_discount(200, 10), 180)\n"
)

class VirtualProjectTests(unittest.TestCase):
    def test_write_failing_test_fix_bug_then_pass(self):
        p = Project(self)
        state = p.run(script(
            {"text": "", "tool_calls": [tc(1, "search_text", query="apply_discount")]},
            {"text": "", "tool_calls": [tc(2, "read_file", path="src/shop/orders/pricing.py")]},
            {"text": "", "tool_calls": [tc(3, "propose_new_file", path="tests/test_pricing_regression.py", content=REGRESSION)]},
            lambda m: {"text": "", "tool_calls": [tc(4, "apply_proposal", proposal_id=last(m)["proposal_id"])]},
            {"text": "", "tool_calls": [tc(5, "run_tests", name="unit")]},
            {"text": "", "tool_calls": [tc(6, "propose_edit", path="src/shop/orders/pricing.py", old="price - price * percent", new="price * (1 - percent / 100)")]},
            lambda m: {"text": "", "tool_calls": [tc(7, "apply_proposal", proposal_id=last(m)["proposal_id"])]},
            {"text": "", "tool_calls": [tc(8, "run_tests", name="unit")]},
            {"text": "fixed", "tool_calls": []}))
        self.assertEqual(state, "COMPLETED")
        runs = p.results("run_tests")
        self.assertEqual([r["status"] for r in runs], ["FAIL", "PASS"], "new test must fail before the fix and pass after")
        self.assertIn("price * (1 - percent / 100)", p.read("src/shop/orders/pricing.py"))
        self.assertTrue((p.root / "tests" / "test_pricing_regression.py").is_file())
        self.assertEqual(p.results("search_text")[0]["matches"][0]["path"], "src/shop/orders/pricing.py")

    def test_finds_setting_in_unexpected_location_and_edits_it(self):
        p = Project(self)
        state = p.run(script(
            {"text": "", "tool_calls": [tc(1, "search_text", query="retry_limit")]},  # case-insensitive
            {"text": "", "tool_calls": [tc(2, "find_files", pattern="*.ini")]},
            {"text": "", "tool_calls": [tc(3, "propose_edit", path="deploy/legacy/settings_old.ini", old="RETRY_LIMIT = 1", new="RETRY_LIMIT = 5")]},
            lambda m: {"text": "", "tool_calls": [tc(4, "apply_proposal", proposal_id=last(m)["proposal_id"])]},
            {"text": "done", "tool_calls": []}))
        self.assertEqual(state, "COMPLETED")
        found = {m["path"] for m in p.results("search_text")[0]["matches"]}
        self.assertIn("deploy/legacy/settings_old.ini", found)
        self.assertIn("src/shop/utils/config.py", found)
        self.assertIn("docs/メモ/仕様 メモ.md", found)
        self.assertTrue(all(not f.startswith(("node_modules", ".venv", "__pycache__", "data/blob.bin", "data/big.txt")) for f in found), found)
        self.assertEqual(p.results("find_files")[0]["files"], ["deploy/legacy/settings_old.ini"])
        self.assertIn("RETRY_LIMIT = 5", p.read("deploy/legacy/settings_old.ini"))

    def test_search_is_bounded_and_skips_noise(self):
        p = Project(self)
        p.run(script({"text": "", "tool_calls": [tc(1, "search_text", query="RETRY_LIMIT line", path="data"), tc(2, "find_files", pattern="*", path=".")]}, {"text": "", "tool_calls": []}))
        s = p.results("search_text")[0]
        self.assertLessEqual(len(s["matches"]), 5, "one noisy file is capped per file")
        self.assertEqual(s["more_in_file"], {"data/many.txt": 195})
        files = p.results("find_files")[0]["files"]
        self.assertFalse([f for f in files if f.startswith((".git/", "node_modules/", ".venv/", "__pycache__/"))])
        self.assertIn(".vscode/launch.json", files, ".vscode must stay discoverable")

    def test_crlf_file_edit_applies_and_keeps_crlf(self):
        p = Project(self)
        state = p.run(script(
            {"text": "", "tool_calls": [tc(1, "propose_edit", path="legacy_win/crlf_module.py", old="def total():" + LF + "    return 1", new="def total():" + LF + "    return 2")]},
            lambda m: {"text": "", "tool_calls": [tc(2, "apply_proposal", proposal_id=last(m)["proposal_id"])]},
            {"text": "ok", "tool_calls": []}))
        self.assertEqual(state, "COMPLETED")
        self.assertEqual(p.results("apply_proposal")[0]["status"], "APPLIED")
        data = (p.root / "legacy_win" / "crlf_module.py").read_bytes()
        self.assertEqual(data, ("def total():" + CR + LF + "    return 2" + CR + LF).encode())

    def test_japanese_path_with_space_is_editable(self):
        p = Project(self)
        path = "docs/メモ/仕様 メモ.md"
        p.run(script(
            {"text": "", "tool_calls": [tc(1, "read_file", path=path)]},
            {"text": "", "tool_calls": [tc(2, "propose_edit", path=path, old="RETRY_LIMIT で決まる", new="RETRY_LIMIT と TIMEOUT で決まる")]},
            lambda m: {"text": "", "tool_calls": [tc(3, "apply_proposal", proposal_id=last(m)["proposal_id"])]},
            {"text": "ok", "tool_calls": []}))
        self.assertIn("TIMEOUT で決まる", p.read(path))

    def test_edits_across_several_files_all_land(self):
        p = Project(self)
        def apply_prev(i):
            return lambda m: {"text": "", "tool_calls": [tc(i, "apply_proposal", proposal_id=last(m)["proposal_id"])]}
        state = p.run(script(
            {"text": "", "tool_calls": [tc(1, "propose_edit", path="deploy/legacy/settings_old.ini", old="TIMEOUT = 30", new="TIMEOUT = 60")]},
            apply_prev(2),
            {"text": "", "tool_calls": [tc(3, "propose_edit", path="src/shop/orders/pricing.py", old="# percent is 0-100", new="# percent is 0-100 (not a ratio)")]},
            apply_prev(4),
            {"text": "", "tool_calls": [tc(5, "propose_new_file", path="docs/CHANGES.md", content="- timeout 60\n")]},
            apply_prev(6),
            {"text": "ok", "tool_calls": []}))
        self.assertEqual(state, "COMPLETED")
        self.assertIn("TIMEOUT = 60", p.read("deploy/legacy/settings_old.ini"))
        self.assertIn("not a ratio", p.read("src/shop/orders/pricing.py"))
        self.assertEqual(p.read("docs/CHANGES.md"), "- timeout 60\n")

    def test_denied_approval_changes_nothing_in_the_tree(self):
        p = Project(self)
        before = {f: f.read_bytes() for f in p.root.rglob("*") if f.is_file() and f.stat().st_size < 10000}
        p.run(script(
            {"text": "", "tool_calls": [tc(1, "propose_edit", path="deploy/legacy/settings_old.ini", old="RETRY_LIMIT = 1", new="RETRY_LIMIT = 9")]},
            lambda m: {"text": "", "tool_calls": [tc(2, "apply_proposal", proposal_id=last(m)["proposal_id"])]},
            {"text": "ok", "tool_calls": []}), approve=False)
        after = {f: f.read_bytes() for f in p.root.rglob("*") if f.is_file() and f.stat().st_size < 10000}
        self.assertEqual(before, after)

    def test_search_cannot_leave_workspace_or_read_git(self):
        p = Project(self)
        p.run(script({"text": "", "tool_calls": [tc(1, "search_text", query="x", path=".."), tc(2, "search_text", query="core", path=".git"), tc(3, "find_files", pattern="*", path="C:/")]}, {"text": "", "tool_calls": []}))
        self.assertEqual([r["status"] for r in p.results("search_text") + p.results("find_files")], ["ERROR"] * 3)

if __name__ == "__main__":
    unittest.main()
