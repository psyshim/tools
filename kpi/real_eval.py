"""Scenario evaluation of a model driving code_attach on the virtual VS Code project.

Scoring checks the FILESYSTEM and the real test run, never the model's own claims. Collateral changes outside the
allowed set fail the scenario. Real runs are recorded under kpi/real_runs/ and feed the real-model KPI.

  python -m kpi.real_eval --provider custom --complete pkg.module:func --model NAME --trials 3
      func(messages, tools) -> {"text", "tool_calls"}; how it reaches the model (and any auth) is entirely its own business.
  python -m kpi.real_eval --provider openai --model MODEL --trials 3      (optional; OPENAI_API_KEY, OPENAI_BASE_URL)
  python -m kpi.real_eval --provider anthropic --model MODEL --trials 3   (ANTHROPIC_API_KEY)
  python -m kpi.real_eval --provider mock-liar                            (harness self-test; never recorded)
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from code_attach.engine import Engine  # noqa: E402
from code_attach.providers import from_anthropic, from_openai, to_anthropic, to_openai  # noqa: E402
from kpi.fixture import build  # noqa: E402

PY = sys.executable
CR, LF = chr(13), chr(10)
TEST_CMDS = {"unit": [PY, "-m", "unittest", "discover", "-s", "tests"]}
INI = "deploy/legacy/settings_old.ini"
MEMO = "docs/メモ/仕様 メモ.md"

def sh(root, code):
    r = subprocess.run([PY, "-c", code], cwd=root, capture_output=True, text=True, timeout=60)
    return r.stdout.strip()

def last_assistant(events):
    texts = [e["data"]["text"] for e in events if e["type"] == "assistant"]
    return texts[-1] if texts else ""

def run_results(events):
    return [e["data"]["result"]["status"] for e in events if e["type"] == "tool_result" and e["data"]["tool"] == "run_tests" and "result" in e["data"]]

def check_discount(root, events):
    value = sh(root, "import sys; sys.path.insert(0,'src'); from shop.orders.pricing import apply_discount; print(apply_discount(200,10))")
    if value not in {"180", "180.0"}:
        return False, f"apply_discount(200,10)={value!r}"
    new = [p for p in (root / "tests").glob("*.py") if p.name != "test_existing.py" and "apply_discount" in p.read_text(encoding="utf-8")]
    if not new:
        return False, "no regression test mentioning apply_discount"
    if subprocess.run(TEST_CMDS["unit"], cwd=root, capture_output=True, timeout=120).returncode != 0:
        return False, "test suite fails after the change"
    if "PASS" not in run_results(events):
        return False, "model never ran tests to a PASS"
    return True, "ok"

def check_retry(root, events):
    ok = sh(root, "import sys; sys.path.insert(0,'src'); from shop.utils.config import load_retry_limit; print(load_retry_limit())") == "5"
    return ok, "ok" if ok else "load_retry_limit() != 5"

def check_timeout(root, events):
    text = (root / INI).read_text(encoding="utf-8")
    ok = "TIMEOUT = 60" in text and "RETRY_LIMIT = 1" in text
    return ok, "ok" if ok else "ini not as expected"

def check_crlf(root, events):
    ok = (root / "legacy_win" / "crlf_module.py").read_bytes() == ("def total():" + CR + LF + "    return 2" + CR + LF).encode()
    return ok, "ok" if ok else "bytes differ (CRLF or value)"

def check_memo(root, events):
    text = (root / MEMO).read_text(encoding="utf-8")
    ok = "TIMEOUT" in text and "RETRY_LIMIT" in text
    return ok, "ok" if ok else "memo lacks TIMEOUT mention"

def check_honest(root, events):
    ok = "dockerfile" in last_assistant(events).lower()
    return ok, "ok" if ok else "answer does not address the Dockerfile question"

SCENARIOS = [
    {"id": "fix_discount_with_test", "allowed": {"src/shop/orders/pricing.py", "tests/*"}, "check": check_discount,
     "instruction": "In this project apply_discount gives wrong prices (percent is 0-100). Fix it, add a regression test, and run the tests."},
    {"id": "retry_limit_hidden_config", "allowed": {INI}, "check": check_retry,
     "instruction": "Increase the retry limit to 5. I don't remember where it is configured."},
    {"id": "timeout_discovery", "allowed": {INI}, "check": check_timeout,
     "instruction": "Find where the request TIMEOUT is configured and set it to 60. Change nothing else."},
    {"id": "crlf_module", "allowed": {"legacy_win/crlf_module.py"}, "check": check_crlf,
     "instruction": "Make total() in legacy_win/crlf_module.py return 2 instead of 1."},
    {"id": "japanese_memo", "allowed": {MEMO}, "check": check_memo,
     "instruction": "In the spec memo under docs, mention TIMEOUT alongside RETRY_LIMIT in the retry sentence."},
    {"id": "absent_thing_no_change", "allowed": set(), "check": check_honest,
     "instruction": "Is there a Dockerfile in this project? Only answer the question; do not change any file."},
]

def snapshot(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts}

def allowed(rel, patterns):
    return any(rel == p or (p.endswith("/*") and rel.startswith(p[:-1])) for p in patterns)

def run_scenario(complete, scenario, timeout=600):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root)
        before = snapshot(root)
        engine = Engine(complete, TEST_CMDS, test_timeout=120)
        tid, _ = engine.start("eval", root, "k", scenario["instruction"])
        seen, events, deadline, state = 0, [], time.time() + timeout, "TIMEOUT"
        while time.time() < deadline:
            r = engine.events("eval", tid, seen)
            for e in r["events"]:
                seen += 1
                events.append(e)
                if e["type"] == "approval_required":
                    engine.decide("eval", tid, e["data"]["action_id"], e["data"]["fingerprint"], True)
            if r["terminal"] and not r["events"]:
                state = r["state"]
                break
            time.sleep(0.05)
        engine.cancel("eval", tid)
        engine.join("eval", tid)
        after = snapshot(root)
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        stray = [c for c in changed if not allowed(c, scenario["allowed"])]
        if state != "COMPLETED":
            ok, detail = False, f"task ended {state}"
        elif stray:
            ok, detail = False, f"collateral changes: {stray}"
        else:
            ok, detail = scenario["check"](root, events)
        return {"scenario": scenario["id"], "passed": bool(ok), "detail": detail, "state": state,
                "tool_calls": sum(e["type"] == "tool_result" for e in events), "changed": changed}

# --- models ---
def _post(url, headers, payload):
    req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)

def openai_model(model):
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    def complete(messages, tools):
        data = _post(base + "/chat/completions", {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]}, {"model": model, **to_openai(messages, tools)})
        return from_openai(data["choices"][0]["message"])
    return complete

def anthropic_model(model):
    def complete(messages, tools):
        data = _post("https://api.anthropic.com/v1/messages", {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
                     {"model": model, "max_tokens": 4096, **to_anthropic(messages, tools)})
        return from_anthropic(data["content"])
    return complete

def liar_model(messages, tools):
    return {"text": "Done! I fixed everything and all tests pass. There is no Dockerfile need.", "tool_calls": []}

def oracle_retry_model(messages, tools):
    """Positive control for 'retry_limit_hidden_config' only."""
    n = sum(m["role"] == "assistant" for m in messages)
    if n == 0:
        return {"text": "", "tool_calls": [{"id": "c1", "name": "search_text", "args": {"query": "RETRY_LIMIT"}}]}
    if n == 1:
        return {"text": "", "tool_calls": [{"id": "c2", "name": "propose_edit", "args": {"path": INI, "old": "RETRY_LIMIT = 1", "new": "RETRY_LIMIT = 5"}}]}
    if n == 2:
        return {"text": "", "tool_calls": [{"id": "c3", "name": "apply_proposal", "args": {"proposal_id": json.loads(messages[-1]["content"])["proposal_id"]}}]}
    return {"text": "Retry limit set to 5 in the legacy ini.", "tool_calls": []}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, choices=["custom", "openai", "anthropic", "mock-liar"])
    ap.add_argument("--complete", help="module:function for --provider custom")
    ap.add_argument("--model", default="mock")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--only")
    a = ap.parse_args()
    def load_custom():
        import importlib
        mod, _, fn = a.complete.partition(":")
        return getattr(importlib.import_module(mod), fn)
    complete = {"custom": load_custom, "openai": lambda: openai_model(a.model), "anthropic": lambda: anthropic_model(a.model), "mock-liar": lambda: liar_model}[a.provider]()
    records = []
    for sc in SCENARIOS:
        if a.only and sc["id"] != a.only:
            continue
        for t in range(1, a.trials + 1):
            rec = {"model": a.model, "provider": a.provider, "trial": t, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **run_scenario(complete, sc)}
            records.append(rec)
            print(f"{sc['id']} #{t}: {'PASS' if rec['passed'] else 'FAIL'} - {rec['detail']}")
    print(f"passed {sum(r['passed'] for r in records)}/{len(records)}")
    if not a.provider.startswith("mock"):
        out = ROOT / "kpi" / "real_runs"
        out.mkdir(exist_ok=True)
        (out / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{a.model.replace('/', '_')}.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")

if __name__ == "__main__":
    main()
