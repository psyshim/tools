"""Speed and weight of the delivered parts, measured the same way every time and judged against `perf_budgets.json`.

    python -m hostkit_build.perf [--stack]      # prints the measurements and any budget violations; exit 1 on a violation

Timings are noisy (antivirus, cold disks, shared CI machines), so: every figure is the BEST of several runs for latencies that
should be constant, the p95 of many calls where a distribution matters, and the budgets are deliberately loose (several times
the usual value). A budget is there to catch an order-of-magnitude regression, not to benchmark. The release gate records the raw
numbers in the delivery receipt so a slow drift is visible long before a budget trips.
"""
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUDGETS = json.loads((Path(__file__).with_name("perf_budgets.json")).read_text(encoding="utf-8"))


def p95(values):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


def measure_kit(repeats=5):
    from hostkit_build.build_host_kit import build
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        wheel = build(tmp)
        out["kit_wheel_kib"] = round(os.path.getsize(wheel) / 1024, 1)
        target = Path(tmp) / "unpacked"
        with zipfile.ZipFile(wheel) as archive:
            out["kit_unpacked_kib"] = round(sum(i.file_size for i in archive.infolist()) / 1024, 1)
            archive.extractall(target)
        code = "import time; t = time.perf_counter(); import host_client, host_client.wrapper; print((time.perf_counter() - t) * 1000)"
        env = {"PYTHONPATH": str(target), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "PATH": os.environ.get("PATH", "")}
        times = [float(subprocess.run([sys.executable, "-S", "-c", code], cwd=target, env=env, capture_output=True, text=True, timeout=60).stdout) for _ in range(repeats)]
        out["kit_import_ms_best"] = round(min(times), 1)
    return out


def measure_wrapper(calls=200):
    sys.path.insert(0, str(ROOT / "packages" / "host_client"))
    sys.path.insert(0, str(ROOT))
    from host_client import AgentGateway, CodingEndpoints, HostEnvironment
    from testkit.mock import MockCodingService
    import json as _json

    class Open(HostEnvironment):
        def can_access_workspace(self, user, workspace_id):
            return True
    service = MockCodingService({}, {"host:u": {"w"}}, host_credentials={"c"})

    def transport(method, url, headers, body, timeout):
        status, payload = service.handle(method, url[len("http://x"):], headers, _json.loads(body) if body else None)
        return status, _json.dumps(payload).encode()
    endpoints = CodingEndpoints(AgentGateway("http://x", "c", transport=transport, sleep=lambda s: None), Open())
    starts, polls = [], []
    handle = None
    for i in range(calls):
        t = time.perf_counter()
        handle = endpoints.dispatch("POST", "/coding/start", user="u", body={"workspace_id": "w", "text": "propose", "conversation_id": "c", "message_id": f"m{i}"}).body["handle"]
        starts.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter()
        endpoints.dispatch("GET", f"/coding/{handle}/events", user="u", query={"after": "0"})
        polls.append((time.perf_counter() - t) * 1000)
    return {"wrapper_start_ms_p95": round(p95(starts), 2), "wrapper_events_ms_p95": round(p95(polls), 2)}


class _Owner:
    """Just enough of a TestCase for the process-stack harness."""
    def __init__(self):
        self.cleanups = []

    def addCleanup(self, fn, *args):
        self.cleanups.append((fn, args))

    def close(self):
        for fn, args in reversed(self.cleanups):
            try:
                fn(*args)
            except Exception:
                pass


def measure_stack(repeats=5):
    sys.path.insert(0, str(ROOT / "tests"))
    sys.path.insert(0, str(ROOT / "packages" / "host_client"))
    sys.path.insert(0, str(ROOT))
    from stack_harness import Stack
    owner = _Owner()
    try:
        began = time.perf_counter()
        stack = Stack(owner).start()
        ready = time.perf_counter() - began
        gateway = stack.gateway()
        t = time.perf_counter()
        handle = gateway.start_coding("alice", "shop", "propose a change", conversation_id="c", message_id="first")
        first_request = time.perf_counter() - t
        first_event = None
        t = time.perf_counter()
        for event in gateway.observe_coding("alice", handle, poll_interval=0.01, max_seconds=30):
            if first_event is None:
                first_event = time.perf_counter() - t
            if event.type == "state":
                break
        warm, polls = [], []
        for i in range(repeats * 2):
            t = time.perf_counter()
            started = gateway.start_coding("alice", "shop", "propose a change", conversation_id="c", message_id=f"warm{i}")
            warm.append((time.perf_counter() - t) * 1000)
            for _ in gateway.observe_coding("alice", started, poll_interval=0.01, max_seconds=30):      # let it finish: the service rightly caps concurrent tasks
                pass
        for _ in range(40):
            t = time.perf_counter()
            gateway.poll_coding("alice", handle, 0)
            polls.append((time.perf_counter() - t) * 1000)
        return {"stack_ready_s": round(ready, 1), "stack_first_request_ms": round(first_request * 1000), "stack_first_event_ms": round((first_event or 0) * 1000),
                "stack_warm_start_ms_best": round(min(warm)), "stack_poll_ms_p95": round(p95(polls), 1), "stack_poll_ms_median": round(statistics.median(polls), 1)}
    finally:
        owner.close()


def evaluate(measured, budgets=None):
    """Return [(name, value, limit)] for every measurement above its budget. A budget without a measurement is a violation too."""
    budgets = BUDGETS["limits"] if budgets is None else budgets
    bad = []
    for name, limit in budgets.items():
        if name not in measured:
            bad.append((name, None, limit))
        elif measured[name] > limit:
            bad.append((name, measured[name], limit))
    return bad


def measure(include_stack=True):
    out = {**measure_kit(), **measure_wrapper()}
    if include_stack:
        out.update(measure_stack())
    return out


def main(argv=None):
    include_stack = "--stack" in (argv if argv is not None else sys.argv[1:])
    measured = measure(include_stack)
    limits = BUDGETS["limits"] if include_stack else {k: v for k, v in BUDGETS["limits"].items() if not k.startswith("stack_")}
    violations = evaluate(measured, limits)
    for name in sorted(measured):
        limit = limits.get(name)
        print(f"{name:32} {measured[name]:>10}   budget {limit if limit is not None else '-'}")
    for name, value, limit in violations:
        print(f"OVER BUDGET: {name} = {value} (limit {limit})")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
