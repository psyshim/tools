"""The delivery gate: one command that decides whether this revision may be handed over, and writes the receipt that says why.

    python -m hostkit_build.release_gate [--full] [--allow-dirty] [--output delivery_receipt.json]

Exit 0 only when every check PASSED. Checks that cannot run are FAIL, never skipped. The receipt records the exact revision, whether
the tree was clean, the hash of the host kit, every check's result and, in plain words, what this gate does NOT cover (so nobody
reads a green receipt as more than it is). CI runs this on Linux and Windows and a green receipt from both is the delivery evidence.

Without --full the (slow) complete test suite is not run: use that for a quick look, never for a delivery.
"""
import argparse
import ast
import hashlib
import json
import platform
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOT_COVERED = [
    "end-to-end behaviour inside the receiving host's real application (login, ACL, UI, quotas)",
    "the React approval card in a real browser",
    "quality of any real model; only scripted models are used",
    "concurrent human saves on POSIX; rename-over savers on Windows (documented residual risks)",
    "the receiving host's network and process supervision",
]
KIT_FORBIDDEN_IMPORTS = {"code_attach", "coding_agent", "device_agent", "coding_service", "fastapi", "starlette", "pydantic", "uvicorn", "subprocess", "shutil",
                         "socket", "ctypes", "multiprocessing", "openai", "anthropic", "httpx", "requests", "testkit", "tests", "examples"}
PERSONAL = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+(?!me\b|<)|/home/(?!me\b|<)|/Users/(?!me\b|<)")
DOC_PATH = re.compile(r"`((?:docs|examples|tests|packages|coding_agent|code_attach|coding_service|device_agent|protocol|hostkit_build|testkit|kpi)/[\w./-]+?)`")
RESULTS = []


def git(*args):
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def check(name):
    def wrap(fn):
        started = time.time()
        try:
            detail = fn() or ""
            outcome = "PASS"
        except AssertionError as exc:
            outcome, detail = "FAIL", str(exc)[:600]
        except Exception as exc:                                        # a check that cannot run is a failure, never a skip
            outcome, detail = "FAIL", f"{type(exc).__name__}: {str(exc)[:400]}"
        RESULTS.append({"check": name, "outcome": outcome, "detail": detail, "seconds": round(time.time() - started, 1)})
        print(f"{outcome:4} {name}" + (f"  -- {detail}" if detail and outcome != "PASS" else ""), flush=True)
        return fn
    return wrap


def run_tests(modules, timeout):
    done = subprocess.run([sys.executable, "-u", "-m", "unittest", *modules], cwd=ROOT, capture_output=True, text=True, timeout=timeout, errors="replace")
    tail = done.stderr[-4000:]
    ran = re.search(r"Ran (\d+) tests? in", tail)
    failed = re.findall(r"^(?:FAIL|ERROR): (\S+) \(([^)]*)\)", done.stderr, re.M)
    return {"returncode": done.returncode, "ran": int(ran.group(1)) if ran else 0, "failures": [f"{b}" for _, b in failed][:20], "tail": tail[-600:]}


def test_groups():
    heavy, rest = [], []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        (heavy if ("stack_harness" in text or "uvicorn" in text or "Stack(" in text) else rest).append(f"tests.{path.stem}")
    return heavy, rest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--full", action="store_true", help="run the complete test suite (required for a delivery)")
    parser.add_argument("--allow-dirty", action="store_true", help="permit uncommitted changes; recorded in the receipt and never acceptable for a delivery")
    parser.add_argument("--output", default="delivery_receipt.json")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "packages" / "host_client"))
    state = {}

    @check("the working tree is clean and the revision is known")
    def _():
        state["revision"], state["branch"] = git("rev-parse", "HEAD"), git("branch", "--show-current")
        state["dirty"] = bool(git("status", "--porcelain"))
        assert state["revision"], "not a git checkout: the receipt could not name a revision"
        assert args.allow_dirty or not state["dirty"], "uncommitted or untracked files present: a delivery is built from a committed revision"
        return "dirty (allowed for this run; NOT a delivery)" if state["dirty"] else ""

    @check("the host kit builds, is reproducible and its manifest matches its contents")
    def _():
        from hostkit_build.build_host_kit import build
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first, second = build(one), build(two)
            sha = hashlib.sha256(first.read_bytes()).hexdigest()
            assert sha == hashlib.sha256(second.read_bytes()).hexdigest(), "two builds of the same tree differ"
            with zipfile.ZipFile(first) as archive:
                manifest = json.loads(archive.read(next(n for n in archive.namelist() if n.endswith("KIT_MANIFEST.json"))))
                for name, digest in manifest["files"].items():
                    assert hashlib.sha256(archive.read(name)).hexdigest() == digest, f"{name} differs from the manifest"
                state["kit_files"] = sorted(manifest["files"])
                state["kit_texts"] = {n: archive.read(n).decode("utf-8", "replace") for n in manifest["files"]}
        state["kit_sha256"] = sha
        return ""

    @check("the host kit contains nothing powerful and nothing personal")
    def _():
        assert state.get("kit_files"), "no kit to inspect"
        outside = [n for n in state["kit_files"] if not n.startswith(("host_client/", "protocol/"))]
        assert not outside, f"unexpected files in the kit: {outside[:5]}"
        for name, text in state["kit_texts"].items():
            if not name.endswith(".py"):
                continue
            for node in ast.walk(ast.parse(text)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
                for imported in names:
                    top = imported.split(".")[0]
                    allowed_test_support = name == "host_client/testing.py" and top == "testkit"          # guarded fallback inside the repository only
                    assert top not in KIT_FORBIDDEN_IMPORTS or allowed_test_support, f"{name} imports {imported}"
            assert not PERSONAL.search(text), f"{name} contains a personal or machine-specific path"

    @check("the default installation cannot change anything")
    def _():
        from coding_service.config import parse_config as parse_service
        from device_agent.config import parse_config as parse_device
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "proj").mkdir()
            service = parse_service({"db_path": str(base / "s.db"), "model": "m.py:complete", "devices": {"d": {"url": "http://127.0.0.1:1", "token_env": "T"}},
                                     "hosts": {"h": {"credential_sha256": "a" * 64, "workspaces": {"w": {"device": "d", "workspace": "w"}}}}}, env={"T": "x"})
            assert service.allow_mutation is False, "the service default must be read/propose"
            device = parse_device({"device_id": "d", "token_sha256": "a" * 64, "state_dir": str(base / "state"), "workspaces": {"w": {"root": str(base / "proj"), "capabilities": ["read", "propose"]}}})
            assert not device.experimental_mutation and not getattr(device, "allow_remote_clients", False), "the device default must be local and non-experimental"
        from code_attach import make_router
        from code_attach.protocol_router import make_protocol_router
        for factory in (make_router, make_protocol_router):
            try:
                factory(lambda m, t: {"text": "", "tool_calls": []}, lambda r: "u", lambda p, w: None)
            except RuntimeError:
                continue
            raise AssertionError(f"{factory.__name__} mounted without the prototype acknowledgement")

    @check("every configuration example in the operator guide is accepted by the real loaders")
    def _():
        from coding_service.config import parse_config as parse_service
        from device_agent.config import parse_config as parse_device
        text = (ROOT / "docs" / "OPERATOR_GUIDE.md").read_text(encoding="utf-8")
        blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", text, re.S)]
        assert len(blocks) >= 2, "the operator guide lost its configuration examples"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()

            def concrete(value):
                if isinstance(value, dict):
                    return {k: concrete(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [concrete(v) for v in value]
                if isinstance(value, str):
                    if re.fullmatch(r"<sha256 hex[^>]*>", value):
                        return "a" * 64
                    if re.match(r"[A-Za-z]:/", value):
                        target = base / value[3:]
                        target.mkdir(parents=True, exist_ok=True) if "." not in target.name else target.parent.mkdir(parents=True, exist_ok=True)
                        return str(target)
                return value
            seen = set()
            for block in blocks:
                block = concrete(block)
                if "hosts" in block:
                    parse_service(block, env={"SVC_MODEL_KEY": "k", "SVC_LAPTOP_TOKEN": "t"})
                    seen.add("service")
                elif "workspaces" in block:
                    parse_device(block)
                    seen.add("device")
            assert seen == {"service", "device"}, f"examples found for {sorted(seen)}"

    @check("documents refer only to files that exist")
    def _():
        missing = []
        for doc in ("HOST_KIT.md", "OPERATOR_GUIDE.md", "HOST_WRAPPER_SPEC.md", "DELIVERY_POLICY.md"):
            for ref in DOC_PATH.findall((ROOT / "docs" / doc).read_text(encoding="utf-8")):
                if not (ROOT / ref.rstrip("/")).exists() and not any(c in ref for c in "<>*{}"):
                    missing.append(f"{doc}: {ref}")
        assert not missing, "; ".join(missing[:8])

    if args.full:
        heavy, rest = test_groups()
        started = time.time()
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(lambda group: run_tests(group, 2400), (heavy, rest)))
        total = sum(o["ran"] for o in outcomes)
        failures = [f for o in outcomes for f in o["failures"]]
        bad = [o for o in outcomes if o["returncode"] != 0 or o["ran"] == 0]
        RESULTS.append({"check": "the complete test suite", "outcome": "FAIL" if bad else "PASS", "seconds": round(time.time() - started, 1),
                        "detail": (f"failures: {failures[:10]}; {bad[0]['tail']}" if bad else f"{total} tests in {len(heavy) + len(rest)} modules")})
        print(f"{RESULTS[-1]['outcome']:4} the complete test suite  -- {RESULTS[-1]['detail'][:300]}", flush=True)
    else:
        RESULTS.append({"check": "the complete test suite", "outcome": "FAIL", "detail": "not run (--full is required for a delivery)", "seconds": 0})
        print("FAIL the complete test suite  -- not run (--full is required for a delivery)", flush=True)

    passed = all(r["outcome"] == "PASS" for r in RESULTS)
    receipt = {"delivery_ok": passed and not state.get("dirty"), "revision": state.get("revision"), "branch": state.get("branch"), "tree_dirty": state.get("dirty"),
               "host_kit_sha256": state.get("kit_sha256"), "python": platform.python_version(), "platform": platform.platform(),
               "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "command": "python -m hostkit_build.release_gate --full",
               "checks": RESULTS, "not_covered": NOT_COVERED}
    Path(args.output).write_text(json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{'DELIVERABLE' if receipt['delivery_ok'] else 'NOT DELIVERABLE'}: revision {str(state.get('revision'))[:10]}, receipt {args.output}")
    return 0 if receipt["delivery_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
