"""Best-effort structured reading of command output (test runners, linters, compilers). Pure functions, no I/O.

Anything unrecognised yields empty `failures`/`diagnostics`; the raw (scrubbed, bounded) output is always returned beside
the parsed form, so a parser miss never hides a result. The process exit code stays the authority for PASS/FAIL.
"""
import re

MAX_FAILURES = 20
MAX_DIAGNOSTICS = 40
MAX_MESSAGE = 300

def _clip(text):
    return " ".join(str(text).split())[:MAX_MESSAGE]

_DIAG = [
    # gcc/clang/javac/mypy/rustc-style:  path:line[:col]: error|warning|note: message
    re.compile(r"^(?P<file>[^\s:()][^:()\n]*?\.\w+):(?P<line>\d+)(?::(?P<col>\d+))?:\s*(?P<sev>fatal error|error|warning|note)\b:?\s*(?P<msg>.*)$", re.M),
    # tsc / msbuild:  path(line,col): error TS1234: message
    re.compile(r"^(?P<file>[^\s()][^()\n]*?\.\w+)\((?P<line>\d+)(?:,(?P<col>\d+))?\):\s*(?P<sev>error|warning)\s+(?P<code>[A-Za-z]+\d+):?\s*(?P<msg>.*)$", re.M),
    # flake8/ruff/pylint-style:  path:line:col: CODE message
    re.compile(r"^(?P<file>[^\s:()][^:()\n]*?\.\w+):(?P<line>\d+):(?P<col>\d+):\s*(?P<code>[A-Z]{1,4}\d{2,5})\s+(?P<msg>.*)$", re.M),
    # go build/vet:  ./path.go:line:col: message
    re.compile(r"^(?P<file>\.?/?[\w./\\-]+\.go):(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.*)$", re.M),
]

def diagnostics(text):
    """File/line/severity/message records found anywhere in the output (compiler, linter and type-checker lines)."""
    found, seen = [], set()
    for rx in _DIAG:
        for m in rx.finditer(text):
            d = m.groupdict()
            key = (d["file"], d["line"], d.get("col"), d.get("msg"))
            if key in seen:
                continue
            seen.add(key)
            entry = {"file": d["file"].strip(), "line": int(d["line"]), "message": _clip(d.get("msg") or ""), "severity": (d.get("sev") or "error").replace("fatal ", "")}
            if d.get("col"):
                entry["column"] = int(d["col"])
            if d.get("code"):
                entry["code"] = d["code"]
            found.append(entry)
            if len(found) >= MAX_DIAGNOSTICS:
                return found
    return found

def _unittest(text):
    if not (re.search(r"^Ran \d+ tests? in ", text, re.M) and re.search(r"^(?:OK|FAILED|NO TESTS RAN)\b", text, re.M)):
        return None
    summary = {"ran": int(re.search(r"^Ran (\d+) tests? in ", text, re.M).group(1))}
    m = re.search(r"^FAILED \((.*)\)$", text, re.M)
    if m is None:
        m = re.search(r"^OK \((.*)\)$", text, re.M)
    for key, value in re.findall(r"(\w+)=(\d+)", m.group(1)) if m else []:
        summary[key] = int(value)
    failures = []
    for m in re.finditer(r"^(FAIL|ERROR): (\S+) \(([^)]*)\)\n-{10,}\n(.*?)(?=\n={10,}|\n-{10,}\nRan |\Z)", text, re.M | re.S):
        body = m.group(4)
        frames = re.findall(r'File "([^"]+)", line (\d+)', body)
        last = [ln for ln in body.strip().splitlines() if ln.strip()][-1:] or [""]
        entry = {"test": m.group(3) or m.group(2), "kind": m.group(1).lower(), "message": _clip(last[0])}
        if frames:
            entry["file"], entry["line"] = frames[-1][0], int(frames[-1][1])
        failures.append(entry)
    return "unittest", summary, failures

def _pytest(text):
    m = re.search(r"^(?:=+ )?(\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)[^\n=]*?|no tests ran) in [\d.]+s", text, re.M)
    failed_lines = list(re.finditer(r"^FAILED (\S+?)(?:::(\S+))?(?: - (.*))?$", text, re.M))
    if not m and not failed_lines:
        return None
    summary = {}
    if m and m.group(1) == "no tests ran":
        summary["ran"] = 0
    for n, label in re.findall(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)", m.group(1) if m else ""):
        summary["error" if label.startswith("error") else label.rstrip("s") if label.startswith("warning") else label] = int(n)
    failures = []
    for f in failed_lines:
        entry = {"test": f.group(1) + ("::" + f.group(2) if f.group(2) else ""), "message": _clip(f.group(3) or ""), "file": f.group(1)}
        at = re.search(re.escape(f.group(1)) + r":(\d+):", text)
        if at:
            entry["line"] = int(at.group(1))
        failures.append(entry)
    return "pytest", summary, failures

def _jest(text):
    m = re.search(r"^Tests:\s+(.+?)\s+total", text, re.M)
    if not m:
        return None
    summary = {label: int(n) for n, label in re.findall(r"(\d+) (failed|passed|skipped|todo)", m.group(1))}
    return "jest", summary, [{"test": _clip(x.group(1))} for x in re.finditer(r"^\s*● (.+?)$", text, re.M)]

def _go(text):
    if not (re.search(r"^\s*--- FAIL: \w+", text, re.M) or re.search(r"^(?:ok|FAIL)\s+\S+\s+[\d.]+s", text, re.M)):
        return None
    failures = [{"test": x.group(1)} for x in re.finditer(r"^\s*--- FAIL: (\S+)", text, re.M)]
    for x in re.finditer(r"^\s+(\S+\.go):(\d+): (.*)$", text, re.M):
        pending = [f for f in failures if "file" not in f]
        if pending:
            pending[0].update({"file": x.group(1), "line": int(x.group(2)), "message": _clip(x.group(3))})
    return "go", {"failed": len(failures)}, failures

def _cargo(text):
    m = re.search(r"test result: \w+\. (\d+) passed; (\d+) failed", text)
    if not m:
        return None
    return "cargo", {"passed": int(m.group(1)), "failed": int(m.group(2))}, [{"test": x.group(1)} for x in re.finditer(r"^test (\S+) \.\.\. FAILED", text, re.M)]

def _maven(text):
    m = re.search(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+)", text)
    if not m:
        return None
    failures = [{"test": x.group(1), "message": _clip(x.group(2))} for x in re.finditer(r"^\[ERROR\]\s+(\S+(?:\.\S+)+):\d*\s*(.*)$", text, re.M)]
    return "maven", {"run": int(m.group(1)), "failures": int(m.group(2)), "errors": int(m.group(3))}, failures

def _dotnet(text):
    m = re.search(r"(?:Failed|Passed)!\s+-\s+Failed:\s+(\d+),\s+Passed:\s+(\d+)", text)
    if not m:
        return None
    return "dotnet", {"failed": int(m.group(1)), "passed": int(m.group(2))}, [{"test": x.group(1)} for x in re.finditer(r"^\s*Failed (\S+)", text, re.M)]

_RUNNERS = (_unittest, _pytest, _jest, _go, _cargo, _maven, _dotnet)

def parse(output):
    """Return {"runner": str|None, "summary": {...}, "failures": [...], "diagnostics": [...]}; never raises on odd input."""
    text = output if isinstance(output, str) else ""
    runner, summary, failures = None, {}, []
    for reader in _RUNNERS:
        found = reader(text)
        if found:
            runner, summary, failures = found
            break
    return {"runner": runner, "summary": summary, "failures": failures[:MAX_FAILURES], "diagnostics": diagnostics(text)}


def verification_status(parsed):
    """Conservative evidence qualifiers; process exit remains available separately."""
    runner, summary = parsed["runner"], parsed["summary"]
    if not runner:
        return "COMMAND_EXIT_ONLY"
    if parsed["failures"] or any(summary.get(k, 0) for k in ("failed", "failures", "error", "errors")):
        return "REPORTED_FAILURES"
    total = summary.get("ran", summary.get("run"))
    if total == 0:
        return "NO_TESTS"
    if total is not None and total <= summary.get("skipped", 0):
        return "ALL_SKIPPED"
    if runner in {"pytest", "jest"} and not summary.get("passed", 0):
        if summary.get("skipped", 0) or summary.get("xfailed", 0) or summary.get("todo", 0):
            return "ALL_SKIPPED"
    if runner in {"cargo", "dotnet"} and summary.get("passed") == 0:
        return "NO_TESTS"
    return "RUNNER_SUMMARY"
