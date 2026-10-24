"""Offline acceptance of housekeeping, local Git commit and hostile encodings.

All writes and automatic approvals are confined to fresh temporary projects.
python -m examples.housekeeping_check --output housekeeping-report.json
"""
import argparse
import codecs
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time

from code_attach.engine import Engine
from code_attach.tools import ToolBox
from coding_agent.git_ops import _run_commit, _run


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def drive(root, recycle_dir, steps):
    def complete(messages, tools):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if len(results) >= len(steps):
            return {"text": "offline fixture finished"}
        name, arguments = steps[len(results)]
        return {"tool_calls": [{"id": f"call-{len(results)}", "name": name,
                                "args": arguments(results) if callable(arguments) else arguments}]}
    engine = Engine(complete, recycle_dir=recycle_dir)
    task_id, _ = engine.start("fixture", root, "one", "offline generated fixture only")
    decided = set()
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = engine.events("fixture", task_id)
            for event in state["events"]:
                if event["type"] == "approval_required" and event["id"] not in decided:
                    d = event["data"]
                    require(engine.decide("fixture", task_id, d["action_id"], d["fingerprint"], True), "approval rejected")
                    decided.add(event["id"])
            if state["terminal"]:
                break
            time.sleep(.01)
        require(state["state"] == "COMPLETED", f"unexpected task state: {state['state']}")
        require(engine.join("fixture", task_id), "worker still running")
        results = [e["data"]["result"] for e in state["events"] if e["type"] == "tool_result" and "result" in e["data"]]
        require(not any(r.get("status") in {"ERROR", "CONFLICT", "FAILED", "UNKNOWN"} for r in results), f"tool failure: {results}")
        return results, len(decided)
    finally:
        engine.cancel("fixture", task_id)
        engine.join("fixture", task_id)


APPLY = ("apply_proposal", lambda r: {"proposal_id": r[-1]["proposal_id"]})


def housekeeping(base):
    root = base / "housekeeping"
    root.mkdir()
    content = "# 日本語\r\nvalue = 1\r\n"
    steps = [
        ("propose_directory", {"path": "整理/空", "operation": "create"}), APPLY,
        ("propose_new_file", {"path": "整理/空/元.txt", "content": content, "encoding": "cp932"}), APPLY,
        ("propose_copy_file", {"source": "整理/空/元.txt", "destination": "整理/控え.txt"}), APPLY,
        ("propose_move_file", {"source": "整理/空/元.txt", "destination": "整理/移動先/改名.txt"}), APPLY,
        ("propose_delete_file", {"path": "整理/移動先/改名.txt"}), APPLY,
        ("list_file_backups", {}),
        ("propose_restore_file", lambda r: {"backup_id": r[-1]["backups"][0]["backup_id"]}), APPLY,
        ("propose_delete_file", {"path": "整理/控え.txt"}), APPLY,
        ("propose_directory", {"path": "整理/空", "operation": "remove_empty"}), APPLY,
    ]
    results, approvals = drive(root, base / "recycle", steps)
    require(approvals == 8, "each side effect must have an approval")
    require((root / "整理/移動先/改名.txt").read_bytes() == content.encode("cp932"), "copy/move/restore changed bytes")
    require(not (root / "整理/控え.txt").exists() and not (root / "整理/空").exists(), "delete/remove did not happen")
    backups = ToolBox(root, recycle_dir=base / "recycle").t_list_file_backups()["backups"]
    require(len(backups) == 2, "persistent deletion backups missing")
    return {"approvals": approvals, "tool_calls": len(results),
            "observed": "create directory/file → copy → move/rename → delete → restore → remove empty directory; CP932 bytes preserved"}


def git_commit(base):
    root = base / "git-fixture"
    root.mkdir()
    _run(["git", "init"], root)
    (root / "guide.txt").write_bytes(b"old\n")
    _run(["git", "add", "--", "guide.txt"], root)
    _run_commit(["git", "commit", "-m", "fixture initial"], root)
    before = _run(["git", "rev-parse", "HEAD"], root).strip()
    (root / "guide.txt").write_bytes(b"new\n")
    (root / "unrelated.txt").write_bytes(b"leave untouched\n")
    steps = [("git_inspect", {"action": "status"}), ("git_inspect", {"action": "diff", "path": "guide.txt"}),
             ("propose_git_commit", {"paths": ["guide.txt"], "message": "approved fixture change"}), APPLY,
             ("git_inspect", {"action": "log"})]
    results, approvals = drive(root, base / "git-recycle", steps)
    require(approvals == 1, "commit must require approval")
    committed = next(r["commit"] for r in results if r.get("status") == "APPLIED")
    require(committed != before and committed == _run(["git", "rev-parse", "HEAD"], root).strip(), "commit SHA evidence missing")
    changed = _run(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"], root).splitlines()
    require(changed == ["guide.txt"], "unselected file committed")
    require(not _run(["git", "diff", "--cached", "--name-only"], root).strip(), "unexpected staged changes after commit")
    require((root / "unrelated.txt").read_bytes() == b"leave untouched\n", "unrelated working file changed")
    return {"approvals": approvals, "commit": committed,
            "observed": "real Git status/diff → approved selected-file commit → log; unrelated file preserved"}


def hostile_encoding(base):
    root = base / "encoding"
    root.mkdir()
    tools = ToolBox(root, recycle_dir=base / "encoding-recycle")
    text = "# 日本語\r\nvalue = 1\r\n"
    checks = 0
    for codec, bom, explicit in (("cp932", b"", "cp932"), ("utf-16-le", codecs.BOM_UTF16_LE, None),
                                 ("utf-16-be", codecs.BOM_UTF16_BE, None), ("utf-8", codecs.BOM_UTF8, None)):
        path = root / "source.txt"
        raw = bom + text.encode(codec)
        path.write_bytes(raw)
        proposal = tools.t_propose_edit("source.txt", "value = 1", "value = 2", encoding=explicit)
        require(path.read_bytes() == raw, "proposal modified source")
        require(tools.t_apply_proposal(proposal["proposal_id"])["status"] == "APPLIED", "encoding edit failed")
        require(path.read_bytes() == bom + text.replace("value = 1", "value = 2").encode(codec), "encoding/BOM/newline changed")
        checks += 1
    for raw in (b"value = 1\n\xe3\x81", b"value = 1\x00binary", text.encode("cp932"),
                "# 日本語\n".encode() + text.encode("cp932"), codecs.BOM_UTF16_LE + b"v"):
        path.write_bytes(raw)
        try:
            tools.t_propose_edit("source.txt", "value = 1", "value = 2")
        except (ValueError, UnicodeError):
            pass
        else:
            raise AssertionError("hostile/ambiguous encoding accepted")
        require(path.read_bytes() == raw, "rejected source corrupted")
        checks += 1
    return {"checks": checks, "observed": "strict explicit/BOM round trips; malformed, mixed and binary data rejected without mutation"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("housekeeping-report.json"))
    args = parser.parse_args()
    records = []
    with tempfile.TemporaryDirectory(prefix="code-attach-housekeeping-") as directory:
        for fn in (housekeeping, git_commit, hostile_encoding):
            started = time.monotonic()
            try:
                detail = fn(Path(directory))
                record = {"case": fn.__name__, "status": "PASS", **detail}
            except Exception as exc:
                record = {"case": fn.__name__, "status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
            record["seconds"] = round(time.monotonic() - started, 3)
            records.append(record)
            print(record["status"], record["case"], flush=True)
    report = {"schema": 1, "timestamp": datetime.now(timezone.utc).isoformat(),
              "evidence": "fixed temporary fixtures, no LLM; actual filesystem and local Git assertions", "cases": records}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Report:", args.output.resolve())
    return 0 if all(r["status"] == "PASS" for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
