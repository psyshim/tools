"""Guard rails that make adding a tool a one-place change that cannot silently drift.

A tool is: a schema in schemas.py + a `t_<name>` method on ToolBox. Side effects go through apply_proposal / run_tests
(EFFECT_TOOLS), which Task gates behind an approval event. These tests fail loudly when the pieces disagree.
"""
import importlib.util
import inspect
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from code_attach.schemas import EFFECT_TOOLS, SCHEMAS
from code_attach.tools import ToolBox

PKG = Path(__file__).resolve().parents[1] / "code_attach"

def method_names():
    return {n[2:] for n, _ in inspect.getmembers(ToolBox, inspect.isfunction) if n.startswith("t_")}

class RegistryTests(unittest.TestCase):
    def test_schemas_and_methods_pair_up_exactly(self):
        names = [s["name"] for s in SCHEMAS]
        self.assertEqual(len(names), len(set(names)), "duplicate schema name")
        self.assertEqual(set(names), method_names())

    def test_each_schema_is_well_formed_and_matches_its_signature(self):
        for schema in SCHEMAS:
            with self.subTest(tool=schema["name"]):
                self.assertTrue(schema["description"].strip())
                params = schema["parameters"]
                self.assertEqual(params["type"], "object")
                props, required = params.get("properties", {}), set(params.get("required", []))
                self.assertLessEqual(required, set(props))
                sig = inspect.signature(getattr(ToolBox, "t_" + schema["name"]))
                accepted = {p for p in sig.parameters if p != "self"}
                needed = {p for p, v in sig.parameters.items() if p != "self" and v.default is inspect.Parameter.empty}
                self.assertLessEqual(set(props), accepted, "schema offers a parameter the method does not accept")
                self.assertLessEqual(needed, required, "a parameter without a default must be marked required, or the model can omit it")

    def test_effect_tools_are_exactly_those_that_say_they_need_approval(self):
        declared = {s["name"] for s in SCHEMAS if "Requires user approval" in s["description"]}
        self.assertEqual(declared, EFFECT_TOOLS)
        self.assertLessEqual(EFFECT_TOOLS, {s["name"] for s in SCHEMAS})

    def test_every_effect_tool_can_describe_itself_for_the_approval_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = ToolBox(tmp, {"t": [sys.executable, "-c", "pass"]})
            for name, args in (("apply_proposal", {"proposal_id": "nope"}), ("run_tests", {"name": "nope"})):
                with self.subTest(tool=name):
                    with self.assertRaises(ValueError) as caught:
                        box.describe_effect(name, args)
                    self.assertNotIn("not an effect tool", str(caught.exception))
            summary, fingerprint = box.describe_effect("run_tests", {"name": "t"})
            self.assertEqual(summary["kind"], "run_tests")
            self.assertEqual(len(fingerprint), 64)

    def test_proposing_tools_never_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("one\n", encoding="utf-8")
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            box = ToolBox(root, recycle_dir=root.parent / (root.name + "-recycle"))
            box.t_propose_edit("a.txt", "one", "two")
            box.t_propose_new_file("b.txt", "x\n")
            box.t_propose_delete_file("a.txt")
            self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)

class HygieneTests(unittest.TestCase):
    def test_no_unused_imports_or_names_in_code_attach(self):
        if importlib.util.find_spec("pyflakes") is None:
            self.skipTest("pyflakes not installed")
        result = subprocess.run([sys.executable, "-m", "pyflakes", str(PKG)], capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "", result.stdout)

    def test_only_testrun_starts_processes(self):
        for path in PKG.glob("*.py"):
            with self.subTest(module=path.name):
                text = path.read_text(encoding="utf-8")
                if path.name != "testrun.py":
                    self.assertNotIn("subprocess", text)

if __name__ == "__main__":
    unittest.main()
