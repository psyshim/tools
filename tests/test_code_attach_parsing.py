"""Pure-function tests: outlines, whole-word matching, command-output parsing, project detection."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from coding_agent.workspace import Workspace
from code_attach.detect import overview, suggest_test_commands
from code_attach.outline import language_of, outline, word_pattern
from code_attach.testparse import diagnostics, parse

PY = sys.executable

PY_SOURCE = '''MAX_SIZE = 10
def top(a):
    return a

class Shop:
    def add(self, x):
        return x
    async def pay(self):
        pass

    class Inner:
        def deep(self):
            pass
'''

class Outlines(unittest.TestCase):
    def names(self, text, suffix):
        return [(e["kind"], e["name"], e["line"]) for e in outline(text, suffix)]

    def test_python_uses_the_real_parser_with_qualified_names(self):
        got = self.names(PY_SOURCE, ".py")
        self.assertIn(("constant", "MAX_SIZE", 1), got)
        self.assertIn(("function", "top", 2), got)
        self.assertIn(("class", "Shop", 5), got)
        self.assertIn(("method", "Shop.add", 6), got)
        self.assertIn(("method", "Shop.pay", 8), got)
        self.assertIn(("method", "Shop.Inner.deep", 12), got)

    def test_broken_python_still_yields_a_heuristic_outline(self):
        got = self.names("def ok(:\n    pass\nclass K:\n  def m(self): pass\n", ".py")
        self.assertIn(("class", "K", 3), got)

    def test_other_languages(self):
        js = "export class A {}\nexport async function load() {}\nconst go = async (x) => x\ninterface I {}\ntype T = string\n"
        self.assertEqual({n for _, n, _ in self.names(js, ".ts")}, {"A", "load", "go", "I", "T"})
        go = "package x\nfunc Run() {}\nfunc (s *S) Method() {}\ntype Config struct {\n}\n"
        self.assertEqual({n for _, n, _ in self.names(go, ".go")}, {"Run", "Method", "Config"})
        java = "public class Foo {\n    public static void main(String[] args) {\n    }\n    private int count() {\n    }\n}\n"
        self.assertTrue({"Foo", "main", "count"} <= {n for _, n, _ in self.names(java, ".java")})
        rust = "pub struct S;\nimpl S {\n    pub fn new() -> S { S }\n}\nfn helper() {}\n"
        self.assertTrue({"S", "new", "helper"} <= {n for _, n, _ in self.names(rust, ".rs")})
        md = "# Title\ntext\n## Section\n"
        self.assertEqual([n for _, n, _ in self.names(md, ".md")], ["Title", "  Section"])
        ini = "[net]\nTIMEOUT = 1\n[db]\n"
        self.assertEqual([n for _, n, _ in self.names(ini, ".ini")], ["net", "db"])
        self.assertEqual(self.names("select 1", ".txt"), [])

    def test_outline_is_bounded_and_survives_hostile_input(self):
        self.assertLessEqual(len(outline("def f(): pass\n" * 5000, ".py")), 500)
        self.assertLessEqual(len(outline("function f() {}\n" * 5000, ".js")), 500)
        for text in ("", "\x00" * 1000, "(" * 100000, "def " * 50000, "class\n" * 1000):
            outline(text, ".py")
            outline(text, ".js")

    def test_language_detection(self):
        self.assertEqual(language_of(".PY"), "python")
        self.assertIsNone(language_of(".exe"))

    def test_word_pattern_is_whole_word_and_regex_safe(self):
        p = word_pattern("load")
        self.assertTrue(p.search("x = load(1)"))
        self.assertFalse(p.search("download()"))
        self.assertFalse(p.search("loaded"))
        for hostile in ("(a+)+$", ".*", "[", "\\", "a|b"):
            word_pattern(hostile).search("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa!")

class ParsingOutput(unittest.TestCase):
    def test_real_unittest_failure_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "test_x.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_bad(self):\n        self.assertEqual(1, 2)\n    def test_err(self):\n        raise KeyError('k')\n    def test_ok(self):\n        pass\n", encoding="utf-8")
            r = subprocess.run([PY, "-m", "unittest", "test_x"], cwd=tmp, capture_output=True, text=True)
        parsed = parse(r.stderr)
        self.assertEqual(parsed["runner"], "unittest")
        self.assertEqual(parsed["summary"]["ran"], 3)
        self.assertEqual((parsed["summary"]["failures"], parsed["summary"]["errors"]), (1, 1))
        kinds = {f["kind"] for f in parsed["failures"]}
        self.assertEqual(kinds, {"fail", "error"})
        bad = next(f for f in parsed["failures"] if f["kind"] == "fail")
        self.assertEqual(bad["line"], 4)
        self.assertTrue(bad["file"].endswith("test_x.py"))
        self.assertIn("1 != 2", bad["message"])

    def test_unittest_ok(self):
        parsed = parse("....\n----------------------------------------------------------------------\nRan 4 tests in 0.002s\n\nOK\n")
        self.assertEqual((parsed["runner"], parsed["summary"], parsed["failures"]), ("unittest", {"ran": 4}, []))

    def test_pytest_variants(self):
        quiet = "FAILED tests/test_a.py::test_x - assert 1 == 2\n1 failed, 3 passed in 0.12s\n"
        parsed = parse(quiet)
        self.assertEqual(parsed["runner"], "pytest")
        self.assertEqual(parsed["summary"], {"failed": 1, "passed": 3})
        self.assertEqual(parsed["failures"][0]["test"], "tests/test_a.py::test_x")
        verbose = "tests/test_a.py:12: AssertionError\n=========================== short test summary info ============================\nFAILED tests/test_a.py::test_x - AssertionError\n========================= 1 failed, 2 passed in 0.31s ==========================\n"
        parsed = parse(verbose)
        self.assertEqual(parsed["failures"][0]["line"], 12)
        self.assertEqual(parse("no tests ran in 0.01s\n")["runner"], "pytest")

    def test_other_runners(self):
        self.assertEqual(parse("Tests:       2 failed, 5 passed, 7 total\n  ● adds numbers\n  ● subtracts\n")["summary"], {"failed": 2, "passed": 5})
        go = "--- FAIL: TestAdd (0.00s)\n    add_test.go:12: got 3 want 4\nFAIL\nFAIL\tpkg/x\t0.01s\n"
        parsed = parse(go)
        self.assertEqual((parsed["runner"], parsed["failures"][0]["test"], parsed["failures"][0]["line"]), ("go", "TestAdd", 12))
        cargo = "test a ... ok\ntest b ... FAILED\n\ntest result: FAILED. 1 passed; 1 failed; 0 ignored\n"
        self.assertEqual(parse(cargo)["failures"], [{"test": "b"}])
        maven = "[ERROR] Tests run: 5, Failures: 1, Errors: 0, Skipped: 0\n[ERROR]   com.x.FooTest.testBar:42 expected:<1> but was:<2>\n"
        self.assertEqual(parse(maven)["summary"], {"run": 5, "failures": 1, "errors": 0})
        dotnet = "Failed MyTests.Adds [3 ms]\nFailed!  - Failed:     1, Passed:     5, Skipped:     0, Total:     6\n"
        self.assertEqual(parse(dotnet)["failures"], [{"test": "MyTests.Adds"}])

    def test_diagnostics_from_compilers_and_linters(self):
        text = ("src/app.py:10:5: F401 'os' imported but unused\n"
                "main.c:7:3: error: expected ';' before 'return'\n"
                "src/Foo.java:12: error: cannot find symbol\n"
                "app.ts(5,10): error TS2322: Type 'string' is not assignable to type 'number'.\n"
                "./main.go:4:2: undefined: foo\n"
                "pkg/mod.py:3: error: Incompatible types in assignment  [assignment]\n")
        got = diagnostics(text)
        self.assertEqual({(d["file"], d["line"]) for d in got},
                         {("src/app.py", 10), ("main.c", 7), ("src/Foo.java", 12), ("app.ts", 5), ("./main.go", 4), ("pkg/mod.py", 3)})
        self.assertEqual(next(d for d in got if d["file"] == "src/app.py")["code"], "F401")
        self.assertEqual(next(d for d in got if d["file"] == "app.ts")["code"], "TS2322")

    def test_parser_never_raises_and_stays_bounded(self):
        for text in (None, 5, "", "\x00" * 5000, "FAILED " * 20000, ("a.py:1:1: error: x\n") * 5000, "Ran 5 tests in 1s\nOK\n" * 1000):
            parsed = parse(text)
            self.assertLessEqual(len(parsed["failures"]), 20)
            self.assertLessEqual(len(parsed["diagnostics"]), 40)

class Detection(unittest.TestCase):
    def project(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for rel, text in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return Workspace(root)

    def test_python_node_and_dotnet_in_one_repo(self):
        ws = self.project({"pyproject.toml": "[tool.pytest.ini_options]\n", "tests/test_a.py": "", "web/package.json": "{}", "package.json": json.dumps({"scripts": {"test": "jest", "lint": "eslint ."}}),
                           "yarn.lock": "", "App.sln": "", "README.md": "x", ".venv/Scripts/python.exe": "", "node_modules/x/index.js": ""})
        got = overview(ws, {".venv", "node_modules"})
        names = {e["name"]: e for e in got["ecosystems"]}
        self.assertTrue({"python", "node", "dotnet"} <= set(names))
        self.assertEqual(names["python"]["suggested_commands"]["unit"][1:], ["-m", "pytest", "-q"])
        self.assertEqual(names["node"]["suggested_commands"]["unit"][0].split(".")[0], "yarn")
        self.assertEqual(names["node"]["suggested_commands"]["lint"][1:], ["run", "lint"])
        self.assertIn("tests", got["test_dirs"])
        self.assertIn("pyproject.toml", got["config_files"])
        self.assertNotIn("node_modules", json.dumps(got))
        self.assertIn("NOT runnable", got["note"])

    def test_python_without_pytest_uses_unittest_and_venv_interpreter(self):
        ws = self.project({"requirements.txt": "requests\n", ".venv/bin/python": "", "app.py": "x=1\n"})
        self.assertEqual(suggest_test_commands(ws, {".venv"})["unit"], [".venv/bin/python", "-m", "unittest", "discover"])

    def test_go_rust_java_make_php_and_empty(self):
        ws = self.project({"go.mod": "module x\n", "Cargo.toml": "", "pom.xml": "", "Makefile": "test:\n\techo\n", "composer.json": json.dumps({"scripts": {"test": "phpunit"}})})
        names = {e["name"] for e in overview(ws)["ecosystems"]}
        self.assertTrue({"go", "rust", "java-maven", "make", "php"} <= names)
        self.assertEqual(overview(self.project({"notes.txt": "hi"}))["ecosystems"], [])

    def test_hostile_manifests_do_not_break_detection(self):
        ws = self.project({"package.json": "{not json", "pyproject.toml": "= = =", "composer.json": "[1,2]", "Makefile": "\x00\x00"})
        self.assertIsInstance(overview(ws)["ecosystems"], list)

    def test_suggestions_are_never_executed_by_detection(self):
        ws = self.project({"package.json": json.dumps({"scripts": {"test": "rm -rf /"}})})
        got = suggest_test_commands(ws)
        self.assertEqual(got["unit"][1], "test")   # argv is [npm, test]; the script body is never interpreted or run here

if __name__ == "__main__":
    unittest.main()
