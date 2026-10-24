"""Coverage of the former weak spots: edit failures that teach, navigation, per-command settings, structured results,
line-range / batch / notebook edits and undo. Everything runs through ToolBox (and Engine for the end-to-end case)."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from code_attach.engine import Engine
from code_attach.testrun import normalize_commands
from code_attach.tools import ToolBox
from kpi.fixture import build

PY = sys.executable
CR, LF = chr(13), chr(10)

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def write(self, rel, text, newline=None):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8") if newline is None else text.replace("\n", newline).encode("utf-8"))
        return p

    def box(self, **kw):
        return ToolBox(self.root, recycle_dir=self.root.parent / (self.root.name + "-recycle"), **kw)

    def apply(self, box, proposal):
        return box.call("apply_proposal", {"proposal_id": proposal["proposal_id"]})

    def raw(self, rel):
        return (self.root / rel).read_bytes()

class EditDiagnostics(Base):
    def test_tab_vs_space_failure_shows_the_real_text_and_line(self):
        self.write("t.py", "def f():\n\tif x:\n\t\treturn 1\n")
        with self.assertRaises(ValueError) as caught:
            self.box().call("propose_edit", {"path": "t.py", "old": "    if x:\n        return 1", "new": "pass"})
        message = str(caught.exception)
        self.assertIn("line 2", message)
        self.assertIn("TAB indentation", message)
        self.assertIn("\tif x:", message)

    def test_duplicate_failure_lists_every_line(self):
        self.write("d.py", "a = f(1)\nb = 2\nc = f(1)\nd = f(1)\n")
        with self.assertRaises(ValueError) as caught:
            self.box().call("propose_edit", {"path": "d.py", "old": "f(1)", "new": "f(2)"})
        self.assertIn("3 times", str(caught.exception))
        self.assertIn("1, 3, 4", str(caught.exception))

    def test_near_miss_points_at_the_closest_text(self):
        self.write("n.py", "def compute_total(items):\n    total = sum(items)\n    return total\n")
        with self.assertRaises(ValueError) as caught:
            self.box().call("propose_edit", {"path": "n.py", "old": "def compute_totl(items):\n    totl = sum(items)", "new": "x"})
        self.assertIn("closest text is at line 1", str(caught.exception))
        self.assertIn("compute_total", str(caught.exception))

    def test_nothing_similar_says_so_plainly(self):
        self.write("z.py", "x = 1\n")
        with self.assertRaises(ValueError) as caught:
            self.box().call("propose_edit", {"path": "z.py", "old": "completely_unrelated_snippet()", "new": "y"})
        self.assertIn("not found anywhere", str(caught.exception))

    def test_lf_old_text_matches_crlf_and_mixed_files_and_keeps_endings_consistent(self):
        self.write("c.txt", "one\ntwo\nthree\nfour\n", newline=CR + LF)
        box = self.box()
        self.assertEqual(self.apply(box, box.call("propose_edit", {"path": "c.txt", "old": "two\nthree", "new": "TWO\nTHREE\nEXTRA"}))["status"], "APPLIED")
        self.assertEqual(self.raw("c.txt"), ("one\r\nTWO\r\nTHREE\r\nEXTRA\r\nfour\r\n").encode())
        (self.root / "m.txt").write_bytes(b"one\r\ntwo\nthree\r\nfour\n")   # mixed endings: an LF-only span inside a CRLF-majority file
        self.assertEqual(self.apply(box, box.call("propose_edit", {"path": "m.txt", "old": "two\nthree", "new": "X\nY"}))["status"], "APPLIED")
        self.assertEqual(self.raw("m.txt"), b"one\r\nX\nY\r\nfour\n")

    def test_single_line_replaced_by_several_lines_in_a_crlf_file_adds_no_bare_lf(self):
        self.write("s.txt", "alpha\nbeta\ngamma\n", newline=CR + LF)
        box = self.box()
        self.apply(box, box.call("propose_edit", {"path": "s.txt", "old": "beta", "new": "b1\nb2"}))
        self.assertEqual(self.raw("s.txt"), b"alpha\r\nb1\r\nb2\r\ngamma\r\n")

    def test_missing_path_suggests_similar_names(self):
        self.write("src/shop/pricing.py", "x = 1\n")
        for tool, args in (("read_file", {"path": "src/shop/pricng.py"}), ("outline_file", {"path": "src/shop/pricing.pyy"}), ("propose_edit", {"path": "src/shop/pricin.py", "old": "x", "new": "y"})):
            with self.subTest(tool=tool):
                with self.assertRaises(ValueError) as caught:
                    self.box().call(tool, args)
                self.assertIn("src/shop/pricing.py", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            self.box().call("read_file", {"path": "nothing/here/at/all.py"})
        self.assertIn("does not exist", str(caught.exception))

class Navigation(Base):
    def setUp(self):
        super().setUp()
        build(self.root)
        self.write("src/shop/orders/cart.py", "from .pricing import apply_discount\n\nclass Cart:\n    def total(self, price):\n        return apply_discount(price, 10)\n")
        self.write("web/app.ts", "export function apply_discount(p: number) { return p }\nconst use = apply_discount(1)\n")
        self.write("notes/apply_discount_notes.txt", "apply_discount is documented here\n")

    def test_find_symbol_separates_definitions_from_references_across_languages(self):
        got = self.box().call("find_symbol", {"name": "apply_discount"})
        defs = {(d["path"], d["kind"]) for d in got["definitions"]}
        self.assertEqual(defs, {("src/shop/orders/pricing.py", "function"), ("web/app.ts", "function")})
        refs = {r["path"] for r in got["references"]}
        self.assertTrue({"src/shop/orders/cart.py", "web/app.ts"} <= refs)
        self.assertNotIn("notes/apply_discount_notes.txt", {d["path"] for d in got["definitions"]})
        self.assertFalse(got["time_limited"])

    def test_qualified_names_and_kind_filter(self):
        box = self.box()
        got = box.call("find_symbol", {"name": "Cart.total"})
        self.assertEqual([(d["path"], d["name"]) for d in got["definitions"]], [("src/shop/orders/cart.py", "Cart.total")])
        self.assertEqual(box.call("find_symbol", {"name": "Cart", "kind": "method"})["definitions"], [])
        self.assertEqual(box.call("find_symbol", {"name": "nonexistent_zzz"})["definitions"], [])

    def test_symbol_search_skips_noise_directories(self):
        self.write("node_modules/lib/index.js", "function apply_discount() {}\n")
        paths = {d["path"] for d in self.box().call("find_symbol", {"name": "apply_discount"})["definitions"]}
        self.assertFalse([p for p in paths if p.startswith("node_modules")])

    def test_outline_file_tool(self):
        got = self.box().call("outline_file", {"path": "src/shop/orders/cart.py"})
        self.assertEqual([(e["kind"], e["name"]) for e in got["entries"]], [("class", "Cart"), ("method", "Cart.total")])
        self.assertIsNone(self.box().call("outline_file", {"path": "data/many.txt"})["language"])

    def test_search_whole_word_terms_and_column(self):
        self.write("w.txt", "download the file\nload it\nreload\nLOAD again\n")
        box = self.box()
        hits = box.call("search_text", {"query": "load", "path": "w.txt", "whole_word": True})["matches"]
        self.assertEqual([h["line"] for h in hits], [2, 4])
        self.assertEqual(box.call("search_text", {"query": "load", "path": "w.txt"})["matches"][0]["column"], 5)
        alts = box.call("search_text", {"query": "download", "terms": ["reload"], "path": "w.txt"})["matches"]
        self.assertEqual([h["line"] for h in alts], [1, 3])
        for bad in ({"terms": "x"}, {"terms": [1]}, {"terms": ["a"] * 9}, {"terms": [""]}):
            with self.assertRaises(ValueError):
                box.call("search_text", {"query": "q", **bad})

    def test_very_long_lines_are_excerpted_and_readable_by_column(self):
        self.write("min.js", "var a=1;" * 5000 + "NEEDLE" + "var b=2;" * 5000 + "\nshort\n")
        box = self.box()
        hit = box.call("search_text", {"query": "NEEDLE", "path": "min.js"})["matches"][0]
        self.assertIn("NEEDLE", hit["text"])
        self.assertLess(len(hit["text"]), 260)
        page = box.call("read_file", {"path": "min.js", "start_line": 1, "end_line": 2})
        self.assertEqual(page["truncated_lines"], [{"line": 1, "length": 80006}])
        self.assertIn("short", page["lines"][1])
        piece = box.call("read_file", {"path": "min.js", "start_line": 1, "column": hit["column"] - 10, "width": 40})
        self.assertIn("NEEDLE", piece["lines"][0])
        self.assertEqual(piece["line_length"], 80006)
        for bad in ({"column": 0}, {"column": "x"}, {"column": 1, "width": 0}, {"column": 1, "width": 10 ** 9}):
            with self.assertRaises(ValueError):
                box.call("read_file", {"path": "min.js", **bad})

    def test_project_overview_includes_what_the_host_enabled(self):
        box = self.box(test_commands={"unit": [PY, "-c", "pass"]})
        got = box.call("project_overview", {})
        self.assertEqual(got["configured_commands"], ["unit"])
        self.assertIn("python", {e["name"] for e in got["ecosystems"]})

class Commands(Base):
    def test_model_sees_the_available_command_names_and_descriptions(self):
        box = self.box(test_commands={"unit": {"argv": [PY, "-c", "pass"], "description": "fast unit tests"}, "lint": [PY, "-c", "pass"]})
        schema = next(s for s in box.schemas() if s["name"] == "run_tests")
        self.assertEqual(schema["parameters"]["properties"]["name"]["enum"], ["unit", "lint"])
        self.assertIn("fast unit tests", schema["description"])
        with self.assertRaises(ValueError) as caught:
            box.describe_effect("run_tests", {"name": "nope"})
        self.assertIn("unit, lint", str(caught.exception))
        self.assertFalse([s for s in self.box().schemas() if s["name"] == "run_tests"])

    def test_per_command_cwd_timeout_and_env(self):
        self.write("services/api/marker.txt", "here\n")
        code = "import os; print(os.listdir('.'), os.environ.get('MODE'), os.environ.get('GLOBAL_X'))"
        box = self.box(test_commands={"api": {"argv": [PY, "-c", code], "cwd": "services/api", "env": {"MODE": "ci"}},
                                      "slow": {"argv": [PY, "-c", "import time; time.sleep(30)"], "timeout": 1}}, test_env={"GLOBAL_X": "g"})
        out = box.t_run_tests("api")
        self.assertEqual(out["status"], "PASS")
        self.assertIn("marker.txt", out["output"])
        self.assertIn("ci", out["output"])
        self.assertIn("g", out["output"])
        started = time.time()
        self.assertEqual(box.t_run_tests("slow")["status"], "TIMEOUT")
        self.assertLess(time.time() - started, 15)
        summary, fingerprint = box.describe_effect("run_tests", {"name": "api"})
        self.assertEqual(summary["cwd"], "services/api")
        self.assertNotIn("ci", json.dumps(summary), "env values are never shown")

    def test_invalid_command_tables_are_rejected_up_front(self):
        for bad in ({"x": []}, {"x": [1]}, {"x": {"argv": ["a"], "timeout": 0}}, {"x": {"argv": ["a"], "timeout": 99999}}, {"x": {"argv": ["a"], "cwd": ".."}},
                    {"x": {"argv": ["a"], "cwd": "/abs"}}, {"x": {"argv": ["a"], "env": {"k": 1}}}, {"": ["a"]}, {"x": "a b"}, {"x": {"nope": 1}}):
            with self.subTest(bad=str(bad)):
                with self.assertRaises(ValueError):
                    normalize_commands(bad)
                with self.assertRaises(ValueError):
                    Engine(lambda m, t: {"text": "", "tool_calls": []}, bad)

    def test_cwd_that_is_not_a_directory_reports_unknown(self):
        box = self.box(test_commands={"x": {"argv": [PY, "-c", "pass"], "cwd": "missing"}})
        self.assertEqual(box.t_run_tests("x")["status"], "UNKNOWN")

    def test_results_are_parsed_into_failures_with_file_and_line(self):
        self.write("pkg/test_it.py", "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(False, 'boom')\n")
        box = self.box(test_commands={"unit": {"argv": [PY, "-m", "unittest", "discover", "-s", "pkg"], "cwd": "."}})
        out = box.t_run_tests("unit")
        self.assertEqual(out["status"], "FAIL")
        self.assertEqual(out["parsed"]["runner"], "unittest")
        self.assertEqual(out["parsed"]["failures"][0]["line"], 4)
        self.assertIn("boom", out["parsed"]["failures"][0]["message"])

    def test_compiler_style_output_yields_diagnostics(self):
        code = "print('src/a.py:3:1: F401 unused import'); print('b.c:9:2: error: expected ;'); raise SystemExit(1)"
        out = self.box(test_commands={"lint": [PY, "-c", code]}).t_run_tests("lint")
        self.assertEqual({(d["file"], d["line"]) for d in out["parsed"]["diagnostics"]}, {("src/a.py", 3), ("b.c", 9)})

class LineRangeEdits(Base):
    def test_replace_lines_happy_path_delete_and_insertion_growth(self):
        self.write("a.txt", "l1\nl2\nl3\nl4\n")
        box = self.box()
        self.assertEqual(self.apply(box, box.call("propose_replace_lines", {"path": "a.txt", "start_line": 2, "end_line": 3, "expect": "l2\nl3", "new_text": "X\nY\nZ"}))["status"], "APPLIED")
        self.assertEqual(self.raw("a.txt"), b"l1\nX\nY\nZ\nl4\n")
        self.apply(box, box.call("propose_replace_lines", {"path": "a.txt", "start_line": 2, "end_line": 4, "expect": "X\nY\nZ", "new_text": ""}))
        self.assertEqual(self.raw("a.txt"), b"l1\nl4\n")

    def test_expectation_mismatch_shows_the_real_lines_and_out_of_range_is_clear(self):
        self.write("a.txt", "l1\nl2\nl3\n")
        box = self.box()
        with self.assertRaises(ValueError) as caught:
            box.call("propose_replace_lines", {"path": "a.txt", "start_line": 1, "end_line": 2, "expect": "wrong", "new_text": "x"})
        self.assertIn("l1", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            box.call("propose_replace_lines", {"path": "a.txt", "start_line": 2, "end_line": 9, "expect": "l2", "new_text": "x"})
        self.assertIn("3 lines", str(caught.exception))
        for bad in ({"start_line": 0}, {"start_line": 3, "end_line": 1}, {"start_line": "1"}):
            with self.assertRaises(ValueError):
                box.call("propose_replace_lines", {"path": "a.txt", "start_line": 1, "end_line": 1, "expect": "l1", "new_text": "x", **bad})

    def test_crlf_and_duplicates_elsewhere_are_handled(self):
        self.write("c.txt", "same\nsame\nsame\n", newline=CR + LF)
        box = self.box()
        self.apply(box, box.call("propose_replace_lines", {"path": "c.txt", "start_line": 2, "end_line": 2, "expect": "same", "new_text": "middle\nextra"}))
        self.assertEqual(self.raw("c.txt"), b"same\r\nmiddle\r\nextra\r\nsame\r\n")

    def test_stale_file_conflicts_instead_of_overwriting(self):
        self.write("a.txt", "l1\nl2\n")
        box = self.box()
        proposal = box.call("propose_replace_lines", {"path": "a.txt", "start_line": 1, "end_line": 1, "expect": "l1", "new_text": "mine"})
        self.write("a.txt", "changed elsewhere\nl2\n")
        self.assertEqual(self.apply(box, proposal)["status"], "CONFLICT")
        self.assertEqual(self.raw("a.txt"), b"changed elsewhere\nl2\n")

class BatchEdits(Base):
    def files(self):
        self.write("a.py", "def f():\n    return old_name(1)\n")
        self.write("b.py", "from a import f\nold_name = 1\nprint(old_name)\n")

    def test_rename_across_files_is_one_approval_and_one_result(self):
        self.files()
        box = self.box()
        proposal = box.call("propose_edits", {"edits": [{"path": "a.py", "old": "old_name(1)", "new": "new_name(1)"}, {"path": "b.py", "old": "old_name = 1", "new": "new_name = 1"},
                                                        {"path": "b.py", "old": "print(old_name)", "new": "print(new_name)"}, {"path": "docs/CHANGES.md", "content": "renamed\n"}]})
        self.assertEqual(self.raw("a.py"), b"def f():\n    return old_name(1)\n", "nothing is written by proposing")
        summary, _ = box.describe_effect("apply_proposal", {"proposal_id": proposal["proposal_id"]})
        self.assertEqual(summary["kind"], "batch_edit")
        self.assertEqual({f["path"] for f in summary["files"]}, {"a.py", "b.py", "docs/CHANGES.md"})
        result = self.apply(box, proposal)
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(self.raw("b.py"), b"from a import f\nnew_name = 1\nprint(new_name)\n")
        self.assertEqual(self.raw("docs/CHANGES.md"), b"renamed\n")

    def test_one_bad_edit_rejects_the_whole_proposal_with_its_position(self):
        self.files()
        with self.assertRaises(ValueError) as caught:
            self.box().call("propose_edits", {"edits": [{"path": "a.py", "old": "old_name(1)", "new": "x"}, {"path": "b.py", "old": "not here", "new": "x"}]})
        self.assertIn("edit 2 (b.py)", str(caught.exception))

    def test_external_change_before_apply_conflicts_and_changes_nothing(self):
        self.files()
        box = self.box()
        proposal = box.call("propose_edits", {"edits": [{"path": "a.py", "old": "old_name(1)", "new": "new_name(1)"}, {"path": "b.py", "old": "old_name = 1", "new": "new_name = 1"}]})
        self.write("b.py", "edited by someone\n")
        result = self.apply(box, proposal)
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(self.raw("a.py"), b"def f():\n    return old_name(1)\n")

    def test_failure_midway_rolls_back_every_file(self):
        self.files()
        box = self.box()
        proposal = box.call("propose_edits", {"edits": [{"path": "a.py", "old": "old_name(1)", "new": "new_name(1)"}, {"path": "b.py", "old": "old_name = 1", "new": "new_name = 1"}]})
        real, calls = box.flow.replace_bytes, []
        def flaky(*a, **k):
            calls.append(a[0])
            if len(calls) == 2:
                raise PermissionError("locked")
            return real(*a, **k)
        box.flow.replace_bytes = flaky
        result = self.apply(box, proposal)
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(self.raw("a.py"), b"def f():\n    return old_name(1)\n", "the first file was restored")
        self.assertEqual(self.raw("b.py"), b"from a import f\nold_name = 1\nprint(old_name)\n")

    def test_rollback_failure_is_reported_as_needing_reconciliation(self):
        self.files()
        box = self.box()
        proposal = box.call("propose_edits", {"edits": [{"path": "a.py", "old": "old_name(1)", "new": "new_name(1)"}, {"path": "b.py", "old": "old_name = 1", "new": "new_name = 1"}]})
        real, calls = box.flow.replace_bytes, []
        def sabotage(*a, **k):
            calls.append(a[0])
            if len(calls) >= 2:
                raise PermissionError("locked")
            return real(*a, **k)
        box.flow.replace_bytes = sabotage
        result = self.apply(box, proposal)
        self.assertEqual(result["status"], "NEEDS_RECONCILIATION")
        self.assertEqual(result["inconsistent_paths"], ["a.py"])

    def test_limits_and_shapes(self):
        box = self.box()
        for bad in ([], "x", [1], [{"path": 5}], [{"path": "a.py"}], [{"path": "a.py", "old": 1, "new": "x"}], [{"path": "n.py", "content": 5}], [{"path": "x"}] * 21):
            with self.assertRaises(ValueError):
                box.call("propose_edits", {"edits": bad})
        self.write("e.py", "x\n")
        with self.assertRaises(ValueError):
            box.call("propose_edits", {"edits": [{"path": "e.py", "content": "already exists"}]})

class Undo(Base):
    def test_undo_restores_exact_bytes_and_can_itself_be_undone(self):
        self.write("a.txt", "one\r\ntwo\r\n")
        box = self.box()
        applied = self.apply(box, box.call("propose_edit", {"path": "a.txt", "old": "two", "new": "TWO"}))
        self.assertEqual(self.raw("a.txt"), b"one\r\nTWO\r\n")
        undone = self.apply(box, box.call("propose_undo", {"applied_id": applied["applied_id"]}))
        self.assertEqual((undone["status"], self.raw("a.txt")), ("APPLIED", b"one\r\ntwo\r\n"))
        redone = self.apply(box, box.call("propose_undo", {"applied_id": undone["applied_id"]}))
        self.assertEqual((redone["status"], self.raw("a.txt")), ("APPLIED", b"one\r\nTWO\r\n"))

    def test_undo_of_a_created_file_and_of_a_batch(self):
        self.write("k.py", "v = 1\n")
        box = self.box()
        created = self.apply(box, box.call("propose_new_file", {"path": "new/f.txt", "content": "hi\n"}))
        self.apply(box, box.call("propose_undo", {"applied_id": created["applied_id"]}))
        self.assertFalse((self.root / "new" / "f.txt").exists())
        batch = self.apply(box, box.call("propose_edits", {"edits": [{"path": "k.py", "old": "1", "new": "2"}, {"path": "m.py", "content": "m\n"}]}))
        self.assertTrue((self.root / "m.py").exists())
        self.apply(box, box.call("propose_undo", {"applied_id": batch["applied_id"]}))
        self.assertEqual((self.raw("k.py"), (self.root / "m.py").exists()), (b"v = 1\n", False))

    def test_undo_is_refused_after_someone_else_changed_the_file(self):
        self.write("a.txt", "one\n")
        box = self.box()
        applied = self.apply(box, box.call("propose_edit", {"path": "a.txt", "old": "one", "new": "two"}))
        self.write("a.txt", "someone else\n")
        with self.assertRaises(ValueError) as caught:
            box.call("propose_undo", {"applied_id": applied["applied_id"]})
        self.assertIn("undo refused", str(caught.exception))
        self.assertEqual(self.raw("a.txt"), b"someone else\n")

    def test_unknown_ids_and_history_bounds(self):
        box = self.box()
        for bad in ("nope", "", None, 5):
            with self.assertRaises(ValueError):
                box.call("propose_undo", {"applied_id": bad})
        self.write("h.txt", "0\n")
        for i in range(60):
            box.apply = None
            self.apply(box, box.call("propose_edit", {"path": "h.txt", "old": str(i), "new": str(i + 1)}))
        self.assertLessEqual(len(box.applied), 50)

class Notebooks(Base):
    NB = {"cells": [{"cell_type": "markdown", "metadata": {}, "source": ["# Title\n"]},
                    {"cell_type": "code", "execution_count": 3, "metadata": {}, "outputs": [{"output_type": "stream", "name": "stdout", "text": ["1\n"]}], "source": ["x = 1\n", "print(x)"]}],
          "metadata": {}, "nbformat": 4, "nbformat_minor": 5}

    def setUp(self):
        super().setUp()
        self.write("n.ipynb", json.dumps(self.NB, indent=1, ensure_ascii=False) + "\n")

    def test_read_notebook_lists_cells(self):
        got = self.box().call("read_notebook", {"path": "n.ipynb"})
        self.assertEqual((got["cell_count"], got["cells"][1]["cell_type"], got["cells"][1]["outputs"]), (2, "code", 1))
        self.assertEqual(got["cells"][1]["source"], "x = 1\nprint(x)")

    def test_replace_clears_stale_outputs_and_keeps_the_file_layout(self):
        box = self.box()
        self.apply(box, box.call("propose_notebook_cell", {"path": "n.ipynb", "index": 1, "source": "y = 2\nprint(y)\n"}))
        nb = json.loads(self.raw("n.ipynb"))
        self.assertEqual((nb["cells"][1]["source"], nb["cells"][1]["outputs"], nb["cells"][1]["execution_count"]), (["y = 2\n", "print(y)\n"], [], None))
        text = self.raw("n.ipynb").decode()
        self.assertTrue(text.endswith("}\n") and text.startswith('{\n "cells"'))

    def test_insert_and_delete(self):
        box = self.box()
        self.apply(box, box.call("propose_notebook_cell", {"path": "n.ipynb", "index": 0, "operation": "insert_after", "source": "# new", "cell_type": "markdown"}))
        self.apply(box, box.call("propose_notebook_cell", {"path": "n.ipynb", "index": -1, "operation": "insert_after", "source": "import os"}))
        nb = json.loads(self.raw("n.ipynb"))
        self.assertEqual([c["cell_type"] for c in nb["cells"]], ["code", "markdown", "markdown", "code"])
        self.assertIn("id", nb["cells"][0])   # nbformat 4.5 requires cell ids
        self.apply(box, box.call("propose_notebook_cell", {"path": "n.ipynb", "index": 0, "operation": "delete"}))
        self.assertEqual(len(json.loads(self.raw("n.ipynb"))["cells"]), 3)

    def test_bad_input_is_refused_cleanly(self):
        box = self.box()
        self.write("broken.ipynb", "{not json")
        self.write("plain.json", json.dumps({"a": 1}))
        for call in (lambda: box.call("read_notebook", {"path": "broken.ipynb"}), lambda: box.call("read_notebook", {"path": "plain.json"}),
                     lambda: box.call("propose_notebook_cell", {"path": "n.ipynb", "index": 9, "source": "x"}), lambda: box.call("propose_notebook_cell", {"path": "n.ipynb", "index": 0}),
                     lambda: box.call("propose_notebook_cell", {"path": "n.ipynb", "index": 0, "operation": "explode", "source": "x"}),
                     lambda: box.call("propose_notebook_cell", {"path": "n.ipynb", "index": 0, "source": "x", "cell_type": "weird"}),
                     lambda: box.call("read_notebook", {"path": "n.ipynb", "count": 0})):
            with self.assertRaises(ValueError):
                call()

class EndToEnd(Base):
    def test_model_navigates_edits_atomically_runs_tests_and_undoes(self):
        build(self.root)
        steps = iter([
            {"text": "", "tool_calls": [{"id": "1", "name": "find_symbol", "args": {"name": "apply_discount"}}]},
            {"text": "", "tool_calls": [{"id": "2", "name": "outline_file", "args": {"path": "src/shop/orders/pricing.py"}}]},
            {"text": "", "tool_calls": [{"id": "3", "name": "propose_edits", "args": {"edits": [
                {"path": "src/shop/orders/pricing.py", "old": "price - price * percent", "new": "price * (1 - percent / 100)"},
                {"path": "tests/test_pricing_new.py", "content": "import sys, unittest\nsys.path.insert(0, 'src')\nfrom shop.orders.pricing import apply_discount\nclass T(unittest.TestCase):\n    def test_ten(self):\n        self.assertEqual(apply_discount(200, 10), 180)\n"}]}}]},
        ])
        state = {"pid": None, "stage": 0}
        def model(messages, tools):
            last = messages[-1]
            if state["stage"] < 3:
                state["stage"] += 1
                return next(steps)
            if state["stage"] == 3:
                state["stage"] = 4
                return {"text": "", "tool_calls": [{"id": "4", "name": "apply_proposal", "args": {"proposal_id": json.loads(last["content"])["proposal_id"]}}]}
            if state["stage"] == 4:
                state["stage"] = 5
                return {"text": "", "tool_calls": [{"id": "5", "name": "run_tests", "args": {"name": "unit"}}]}
            return {"text": "done", "tool_calls": []}
        engine = Engine(model, {"unit": {"argv": [PY, "-m", "unittest", "discover", "-s", "tests"], "description": "all unit tests"}}, test_timeout=60)
        tid, _ = engine.start("u", self.root, "k", "x")
        seen = 0
        for _ in range(2000):
            r = engine.events("u", tid, seen)
            for e in r["events"]:
                seen += 1
                if e["type"] == "approval_required":
                    engine.decide("u", tid, e["data"]["action_id"], e["data"]["fingerprint"], True)
            if r["terminal"] and not r["events"]:
                break
            time.sleep(0.01)
        final = engine.events("u", tid)
        self.assertEqual(final["state"], "COMPLETED")
        results = {e["data"]["tool"]: e["data"]["result"] for e in final["events"] if e["type"] == "tool_result" and "result" in e["data"]}
        self.assertEqual(results["find_symbol"]["definitions"][0]["path"], "src/shop/orders/pricing.py")
        self.assertEqual(results["apply_proposal"]["status"], "APPLIED")
        self.assertEqual(results["run_tests"]["status"], "PASS")
        self.assertEqual(results["run_tests"]["parsed"]["runner"], "unittest")
        self.assertIn("applied_id", results["apply_proposal"])

if __name__ == "__main__":
    unittest.main()
