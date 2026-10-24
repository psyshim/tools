"""Reproducible tool acceptance checks: no model, API key, or existing project.

python -m examples.stress_check --output stress-report.json
All effects are restricted to newly created temporary fixture workspaces.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from code_attach.engine import Engine
from code_attach.tools import ToolBox, MAX_FILE_BYTES


def snapshot(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


def seed_script(root, lines=1000, newline="\n", suffix=""):
    """Valid Python with unique filler lines and a real bug close to the end."""
    text = "".join(f"# legacy section {n:06d} — unrelated behavior\n" for n in range(lines))
    text += "def calculate_total(price, percent):\n    return price - price * percent\n"
    text += suffix
    (root / "legacy.py").write_bytes(text.replace("\n", newline).encode("utf-8"))
    (root / "test_regression.py").write_text(
        "import unittest\nfrom legacy import calculate_total\nclass Regression(unittest.TestCase):\n"
        "    def test_discount(self):\n        self.assertEqual(calculate_total(200, 10), 180)\n"
        "if __name__ == '__main__':\n    unittest.main()\n", encoding="utf-8")
    return text


def test_command(root):
    result = subprocess.run([sys.executable, "-B", "test_regression.py"], cwd=root,
                            capture_output=True, timeout=30)
    return result.returncode


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def expect_error(action, text):
    try:
        action()
    except ValueError as exc:
        require(text in str(exc), f"wrong rejection: {exc}")
        return
    raise AssertionError("operation should have been rejected")


def edit_script(root, lines=1000, newline="\n"):
    before = seed_script(root, lines, newline)
    tools = ToolBox(root, {"unit": [sys.executable, "-B", "test_regression.py"]})
    initial = snapshot(root)
    require(test_command(root) != 0, "fixture must fail before repair")
    matches = tools.t_search_text("def calculate_total", glob="legacy.py")["matches"]
    require(matches and matches[0]["line"] == lines + 1, "tail definition not found")
    # Deliberately omit end_line, reproducing the old late-page failure.
    page = tools.t_read_file("legacy.py", start_line=matches[0]["line"])
    require("return price - price * percent" in "\n".join(page["lines"]), "tail page missing")
    proposal = tools.t_propose_edit("legacy.py", "return price - price * percent", "return price - price * percent / 100")
    require(snapshot(root) == initial, "proposal changed files")
    result = tools.t_apply_proposal(proposal["proposal_id"])
    require(result["status"] == "APPLIED", "repair not applied")
    expected = before.replace("return price - price * percent", "return price - price * percent / 100")
    require((root / "legacy.py").read_bytes() == expected.replace("\n", newline).encode("utf-8"), "collateral byte change")
    require(tools.t_run_tests("unit")["status"] == "PASS", "real regression test failed")
    require(snapshot(root)["test_regression.py"] == initial["test_regression.py"], "regression test was altered")
    return {"source_bytes": len(before.replace("\n", newline).encode("utf-8")), "lines": lines + 2,
            "observed": "FAIL → tail search/read → exact edit → PASS"}


def extract_function(root):
    helper = "def calculate_discount(price, percent):\n    return price * percent / 100\n"
    before = seed_script(root, 10000)
    tools = ToolBox(root)
    p = tools.t_propose_new_file("pricing_helpers.py", helper)
    require(tools.t_apply_proposal(p["proposal_id"])["status"] == "APPLIED", "new helper missing")
    old = "def calculate_total(price, percent):\n    return price - price * percent\n"
    new = "from pricing_helpers import calculate_discount\n\ndef calculate_total(price, percent):\n    return price - calculate_discount(price, percent)\n"
    p = tools.t_propose_edit("legacy.py", old, new)
    require(tools.t_apply_proposal(p["proposal_id"])["status"] == "APPLIED", "call site not changed")
    require((root / "legacy.py").read_text(encoding="utf-8") == before.replace(old, new), "unrelated source changed")
    require(test_command(root) == 0, "extracted helper/import does not work")
    return {"observed": "10,000-line script → separate helper module + import → real PASS",
            "limitation": "multiple approvals, not an atomic multi-file transaction"}


def duplicate_context(root):
    seed_script(root, 5000, suffix="\ndef unrelated_total(price, percent):\n    return price - price * percent\n")
    tools = ToolBox(root)
    expect_error(lambda: tools.t_propose_edit("legacy.py", "return price - price * percent", "return 0"), "exactly once")
    old = "def calculate_total(price, percent):\n    return price - price * percent"
    p = tools.t_propose_edit("legacy.py", old, old + " / 100")
    require(tools.t_apply_proposal(p["proposal_id"])["status"] == "APPLIED", "context edit failed")
    require("def unrelated_total(price, percent):\n    return price - price * percent\n" in (root / "legacy.py").read_text(encoding="utf-8"), "duplicate code was modified")
    require(test_command(root) == 0, "context repair fails")
    return {"observed": "ambiguous replacement rejected; enclosing function context succeeds"}


def late_search(root):
    seed_script(root, 5000)
    (root / "many.py").write_text("def repeated(): pass\n" * 100 + "# final needle\n", encoding="utf-8")
    tools = ToolBox(root)
    first = tools.t_search_text("repeated", glob="many.py")
    require(len(first["matches"]) == 5 and first["more_in_file"]["many.py"] == 95, "per-file limit incorrect")
    page = tools.t_search_text("repeated", path="many.py", start_line=96)
    require([m["line"] for m in page["matches"]] == list(range(96, 101)), "late matches inaccessible")
    return {"observed": "search after first five matches using start_line"}


def stale_edit(root):
    seed_script(root, 5000)
    tools = ToolBox(root)
    p = tools.t_propose_edit("legacy.py", "return price - price * percent", "return 0")
    with (root / "legacy.py").open("a", encoding="utf-8") as stream:
        stream.write("\n# human editor change\n")
    human = snapshot(root)
    require(tools.t_apply_proposal(p["proposal_id"])["status"] == "CONFLICT", "stale proposal overwritten")
    require(snapshot(root) == human, "human change lost")
    return {"observed": "CONFLICT; external editor bytes preserved"}


def size_boundary(root, over=False):
    body = b"#" + b"x" * (MAX_FILE_BYTES - 2) + b"\n"
    (root / "boundary.py").write_bytes(body + (b"#" if over else b""))
    tools = ToolBox(root)
    before = snapshot(root)
    if over:
        expect_error(lambda: tools.t_read_file("boundary.py"), "file too large")
        expect_error(lambda: tools.t_propose_edit("boundary.py", "x", "y"), "file too large")
        search = tools.t_search_text("x", glob="boundary.py")
        require(not search["matches"] and search["skipped_large_files"] == 1, "oversize search omission not reported")
    else:
        # Exactly 8 MiB is legal as a file, but a single huge line is not a useful model page.
        require(tools.flow.inspect("boundary.py")["size"] == MAX_FILE_BYTES, "8 MiB boundary rejected")
        page = tools.t_read_file("boundary.py")   # a single 8 MiB line is shortened and reported, never dumped into the model context
        require(page.get("truncated_lines") and len(page["lines"][0]) < 2100, "enormous single-line read was not bounded")
    require(snapshot(root) == before, "boundary rejection modified source")
    return {"source_bytes": len(body) + int(over), "observed": "oversize refused" if over else "8 MiB accepted; enormous single-line read is truncated and reported"}


def oversized_edit(root):
    seed_script(root)
    tools = ToolBox(root)
    before = snapshot(root)
    expect_error(lambda: tools.t_propose_edit("legacy.py", "return price - price * percent", "x" * 20001), "invalid edit size")
    require(not tools.proposals and snapshot(root) == before, "unreviewable proposal stored")
    return {"observed": "20,001-character replacement rejected before approval"}


def exact_edit_boundary(root, crlf=False):
    tools = ToolBox(root)
    if crlf:
        old = "# x\n" * 4000  # 16,000 LF characters become exactly 20,000 CRLF characters.
        new = "# y\n" * 4000
        data = old.replace("\n", "\r\n").encode()
    else:
        old, new = "x" * 20000, "y" * 20000
        data = old.encode()
    (root / "boundary.txt").write_bytes(data)
    p = tools.t_propose_edit("boundary.txt", old, new)
    require(tools.t_apply_proposal(p["proposal_id"])["status"] == "APPLIED", "exact 20,000-character edit failed")
    require((root / "boundary.txt").read_bytes() == (new.replace("\n", "\r\n") if crlf else new).encode(), "boundary bytes wrong")
    if crlf:
        # Normalization must be checked before showing an actionable approval.
        too_big = "# x\n" * 4001
        (root / "too_big.txt").write_bytes(too_big.replace("\n", "\r\n").encode())
        expect_error(lambda: tools.t_propose_edit("too_big.txt", too_big, too_big.replace("x", "y")), "invalid edit size")
    return {"observed": "exact 20,000-character replacement applies" + ("; CRLF expansion over limit rejected early" if crlf else "")}


def unusual_text(root, bom=False):
    before = seed_script(root, 5000).rstrip("\n")
    raw = (b"\xef\xbb\xbf" if bom else b"") + before.encode("utf-8")
    (root / "legacy.py").write_bytes(raw)
    tools = ToolBox(root)
    p = tools.t_propose_edit("legacy.py", "return price - price * percent", "return price - price * percent / 100")
    require(tools.t_apply_proposal(p["proposal_id"])["status"] == "APPLIED", "unusual text edit failed")
    require((root / "legacy.py").read_bytes() == raw.replace(b"return price - price * percent", b"return price - price * percent / 100"), "BOM or final newline changed")
    require(test_command(root) == 0, "unusual text regression failed")
    return {"observed": "UTF-8 BOM and no-final-newline preserved" if bom else "no-final-newline preserved"}


def unsupported_encoding(root):
    raw = "# 日本語の旧形式\nvalue = 1\n".encode("cp932")
    (root / "legacy.py").write_bytes(raw)
    tools = ToolBox(root)
    try:
        tools.t_propose_edit("legacy.py", "value = 1", "value = 2")
    except UnicodeDecodeError:
        pass
    else:
        raise AssertionError("non-UTF-8 should not be silently converted")
    require((root / "legacy.py").read_bytes() == raw and not tools.proposals, "legacy encoding corrupted")
    return {"observed": "CP932 is unsupported; rejected with original bytes intact"}


def engine_large_script(root):
    before = seed_script(root, 100000)
    def complete(messages, schemas):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        step = len(results)
        if step == 0:
            tool, args = "search_text", {"query": "def calculate_total", "glob": "legacy.py"}
        elif step == 1:
            tool, args = "read_file", {"path": "legacy.py", "start_line": results[-1]["matches"][0]["line"]}
        elif step == 2:
            require("return price - price * percent" in "\n".join(results[-1]["lines"]), "wrong model read window")
            tool, args = "propose_edit", {"path": "legacy.py", "old": "return price - price * percent", "new": "return price - price * percent / 100"}
        elif step == 3:
            tool, args = "apply_proposal", {"proposal_id": results[-1]["proposal_id"]}
        elif step == 4:
            tool, args = "run_tests", {"name": "unit"}
        else:
            return {"text": "fixed fixture finished"}
        return {"tool_calls": [{"id": f"call-{step}", "name": tool, "args": args}]}
    engine = Engine(complete, {"unit": [sys.executable, "-B", "test_regression.py"]})
    tid, _ = engine.start("fixture", root, "large", "fixed fixture; approvals are automatic for this generated project only")
    decisions = set()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            data = engine.events("fixture", tid)
            for event in data["events"]:
                if event["type"] == "approval_required" and event["id"] not in decisions:
                    d = event["data"]
                    require(engine.decide("fixture", tid, d["action_id"], d["fingerprint"], True), "approval refused")
                    decisions.add(event["id"])
            if data["terminal"]:
                break
            time.sleep(.01)
        require(engine.join("fixture", tid, 1), "large-script worker did not stop")
        require(data["state"] == "COMPLETED" and len(decisions) == 2, "full Engine flow incomplete")
        require(any(e["type"] == "tool_result" and e["data"]["tool"] == "run_tests" and e["data"].get("result", {}).get("status") == "PASS" for e in data["events"]), "no actual PASS event")
        expected = before.replace("return price - price * percent", "return price - price * percent / 100")
        require((root / "legacy.py").read_bytes() == expected.encode("utf-8"), "collateral source changes")
    finally:
        engine.cancel("fixture", tid)
        engine.join("fixture", tid)
    return {"source_bytes": len(before.encode("utf-8")), "lines": 100002,
            "observed": "full Engine: search → read → propose → approve → apply → approve → real test PASS"}


def full_diff(root):
    tools = ToolBox(root)
    content = "# review line\n" * 1000 + "# END_MARKER\n"
    p = tools.t_propose_new_file("review.py", content)
    require(len(p["diff"]) > 8000 and "END_MARKER" in p["diff"], "approval diff silently truncated")
    huge = "# review line\n" * 5000
    expect_error(lambda: tools.t_propose_new_file("too_big.py", huge), "diff too large")
    require(not (root / "too_big.py").exists(), "oversized proposal wrote a file")
    return {"observed": "full >8,000-character diff retained; >64,000 diff refused"}


def reject_through_engine(root):
    seed_script(root, 5000)
    before = snapshot(root)
    def complete(messages, tools):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not results:
            tool, args = "propose_edit", {"path": "legacy.py", "old": "return price - price * percent", "new": "return 0"}
        elif "proposal_id" in results[-1]:
            tool, args = "apply_proposal", {"proposal_id": results[-1]["proposal_id"]}
        else:
            return {"text": "finished"}
        return {"tool_calls": [{"id": str(len(results)), "name": tool, "args": args}]}
    engine = Engine(complete)
    tid, _ = engine.start("fixture", root, "reject", "fixed fixture test")
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            data = engine.events("fixture", tid)
            for event in data["events"]:
                if event["type"] == "approval_required":
                    d = event["data"]
                    engine.decide("fixture", tid, d["action_id"], d["fingerprint"], False)
            if data["terminal"]:
                break
            time.sleep(.01)
        require(engine.join("fixture", tid, 1), "worker did not stop")
        require(snapshot(root) == before, "rejected action modified fixture")
        require(any(e["type"] == "tool_result" and e["data"].get("status") == "NOT_APPROVED" for e in data["events"]), "no rejection evidence")
    finally:
        engine.cancel("fixture", tid)
        engine.join("fixture", tid)
    return {"observed": "rejected through actual Engine approval gate; all source hashes unchanged"}


CASES = {
    "script_100_lines": lambda root: edit_script(root, 100),
    "script_5000_lines": lambda root: edit_script(root, 5000),
    "script_30000_lines": lambda root: edit_script(root, 30000),
    "script_100000_lines": lambda root: edit_script(root, 100000),
    "script_175000_lines_near_8_mib": lambda root: edit_script(root, 175000),
    "large_crlf_script": lambda root: edit_script(root, 30000, "\r\n"),
    "extract_helper_from_large_script": extract_function,
    "duplicate_code_disambiguation": duplicate_context,
    "search_beyond_first_five_hits": late_search,
    "external_edit_conflict": stale_edit,
    "exact_size_and_single_huge_line": lambda root: size_boundary(root),
    "over_8_mib_rejected": lambda root: size_boundary(root, True),
    "oversized_replacement_rejected_early": oversized_edit,
    "exact_20000_character_replacement": lambda root: exact_edit_boundary(root),
    "crlf_edit_size_boundary": lambda root: exact_edit_boundary(root, True),
    "no_final_newline": lambda root: unusual_text(root),
    "utf8_bom_preserved": lambda root: unusual_text(root, True),
    "cp932_rejected_without_corruption": unsupported_encoding,
    "complete_approval_diff": full_diff,
    "approval_rejection_preserves_all_files": reject_through_engine,
    "engine_100000_lines_with_approvals": engine_large_script,
}


def run_case(name):
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="code-attach-stress-") as directory:
        try:
            details = CASES[name](Path(directory))
            return {"case": name, "status": "PASS", "seconds": round(time.monotonic() - started, 3), **details}
        except Exception as exc:
            return {"case": name, "status": "FAIL", "seconds": round(time.monotonic() - started, 3),
                    "error": f"{type(exc).__name__}: {exc}"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("stress-report.json"))
    parser.add_argument("--case", choices=CASES, action="append")
    args = parser.parse_args()
    records = []
    for name in args.case or CASES:
        record = run_case(name)
        records.append(record)
        print(f"{record['status']:4} {name} ({record['seconds']}s)", flush=True)
    report = {"schema": 1, "timestamp": datetime.now(timezone.utc).isoformat(),
              "evidence": "deterministic tool fixtures; no LLM calls or model-quality measurement",
              "python": sys.version.split()[0], "cases": records,
              "passed": sum(r["status"] == "PASS" for r in records), "total": len(records)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Report: {args.output.resolve()}")
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
