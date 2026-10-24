"""Black-box acceptance check for a host's finished wrapper. Standard library only; run it against the host's running backend.

    python -m host_client.acceptance --base https://chat.example/api --token-a TOKEN --token-b TOKEN2 \\
        --workspace-a WS_A_MAY_USE --workspace-denied WS_A_MAY_NOT_USE [--prefix ""] [--text "list the project files"] [--output result.json]

`--token-*` are two different test users' ordinary login credentials, sent as `Authorization: Bearer <token>`; use the
`--auth-header` option for another scheme. Each check prints PASS / FAIL / SKIP with the reason. SKIP is never a pass: the
exit code is 0 only when nothing FAILED and nothing the host must provide was skipped (use --allow-skip to accept skips when the
coding service is deliberately not running). See docs/HOST_WRAPPER_SPEC.md for what each check protects.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TERMINAL_WAIT = 60


class Host:
    def __init__(self, base, prefix, header, timeout=30):
        self.base, self.prefix, self.header, self.timeout = base.rstrip("/"), prefix.rstrip("/"), header, timeout

    def call(self, method, path, token=None, body=None):
        headers = {"content-type": "application/json"}
        if token:
            headers[self.header] = token if self.header.lower() != "authorization" else "Bearer " + token
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + self.prefix + urllib.parse.quote(path, safe="/%?=&"), data=data, method=method, headers=headers)
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        return status, payload, time.time() - started


def run(args):
    host = Host(args.base, args.prefix, args.auth_header)
    results = []

    def check(name, must_provide=True):
        def wrap(fn):
            try:
                outcome = fn()
                results.append({"check": name, "outcome": "PASS" if outcome is None else outcome[0], "reason": "" if outcome is None else outcome[1], "must_provide": must_provide})
            except AssertionError as exc:
                results.append({"check": name, "outcome": "FAIL", "reason": str(exc)[:300], "must_provide": must_provide})
            except Exception as exc:
                results.append({"check": name, "outcome": "FAIL", "reason": f"{type(exc).__name__}: {str(exc)[:200]}", "must_provide": must_provide})
            print(f"{results[-1]['outcome']:5} {name}" + (f"  -- {results[-1]['reason']}" if results[-1]["reason"] else ""))
        return wrap

    A, B = args.token_a, args.token_b
    ids = {"conversation_id": "acceptance-conv", "message_id": f"acceptance-{int(time.time())}"}
    good = {"workspace_id": args.workspace_a, "text": args.text, **ids}
    state = {}

    @check("availability requires a login")
    def _():
        status, _, _ = host.call("GET", "/coding/availability")
        assert status in (401, 403), f"anonymous caller got {status}"

    @check("availability answers a stable shape for a logged-in user")
    def _():
        status, body, _ = host.call("GET", "/coding/availability", A)
        assert status == 200 and isinstance(body, dict), f"status {status}"
        assert isinstance(body.get("available"), bool) and isinstance(body.get("modes"), list), f"unexpected shape {body}"
        state["available"] = body["available"]
        if not body["available"]:
            return ("SKIP", f"coding service unavailable ({body.get('reason')}); that is a valid answer, later checks that need it are skipped")

    @check("anonymous start is refused")
    def _():
        status, _, _ = host.call("POST", "/coding/start", None, good)
        assert status in (401, 403), f"anonymous caller got {status}"

    @check("a workspace the user may not use is refused by the host (403), quickly, without the service")
    def _():
        status, body, took = host.call("POST", "/coding/start", A, {**good, "workspace_id": args.workspace_denied})
        assert status == 403 and (body or {}).get("error") == "forbidden", f"got {status} {body}"
        assert took < 5, f"took {took:.1f}s: the ACL decision must be local"

    @check("malformed start bodies are 400, not 500")
    def _():
        for bad in ({}, {"text": "x"}, {**good, "text": ""}, {**good, "text": 5}, {**good, "workspace_id": "x" * 500}, []):
            status, _, _ = host.call("POST", "/coding/start", A, bad)
            assert status == 400, f"{str(bad)[:40]!r} -> {status}"

    def needs_service(fn):
        def inner():
            if not state.get("available"):
                return ("SKIP", "coding service not available")
            return fn()
        return inner

    @check("start returns an opaque handle and the same message id returns the same one")
    @needs_service
    def _():
        s1, b1, _ = host.call("POST", "/coding/start", A, good)
        assert s1 == 200 and b1 and b1.get("ok") and isinstance(b1.get("handle"), str), f"got {s1} {b1}"
        s2, b2, _ = host.call("POST", "/coding/start", A, good)
        assert s2 == 200 and b2.get("handle") == b1["handle"], "a retry created a second task"
        state["handle"] = b1["handle"]

    @check("events are ordered, resumable, escape-safe, and the task reaches a terminal state")
    @needs_service
    def _():
        after, last_id, deadline, seen = "0", 0, time.time() + TERMINAL_WAIT, []
        while time.time() < deadline:
            status, body, _ = host.call("GET", f"/coding/{state['handle']}/events?after={after}", A)
            assert status == 200 and body.get("ok"), f"poll got {status} {body}"
            for event in body["events"]:
                if not event.get("synthetic"):
                    assert event["id"] > last_id, "event ids are not increasing"
                    last_id = event["id"]
                assert "<script" not in str(event.get("html", "")).lower().replace("&lt;script", ""), "unescaped markup reached the browser"
                seen.append(event["type"])
            after = body["next"]
            if body["terminal"]:
                state["events"] = seen
                return None
            time.sleep(0.5)
        return ("SKIP", f"no terminal state within {TERMINAL_WAIT}s (model or approval pending); events so far: {seen[-5:]}")

    @check("another user cannot read or cancel this task")
    @needs_service
    def _():
        status, body, _ = host.call("GET", f"/coding/{state['handle']}/events", B)
        assert status == 404, f"other user read events: {status}"
        status, _, _ = host.call("POST", f"/coding/{state['handle']}/cancel", B, {})
        assert status == 404, f"other user cancelled: {status}"
        status, _, _ = host.call("POST", f"/coding/{state['handle']}/decision", B, {"action_id": "a", "fingerprint": "f", "approve": True})
        assert status in (404, 409), f"other user decided: {status}"

    @check("a bad cursor is 400 and a malformed decision is 400")
    @needs_service
    def _():
        status, _, _ = host.call("GET", f"/coding/{state['handle']}/events?after=oops", A)
        assert status == 400, f"bad cursor -> {status}"
        for bad in ({}, {"action_id": "a", "fingerprint": "f", "approve": "yes"}, {"action_id": 1, "fingerprint": "f", "approve": True}):
            status, _, _ = host.call("POST", f"/coding/{state['handle']}/decision", A, bad)
            assert status == 400, f"{bad} -> {status}"

    @check("unknown handles are 404, not 500")
    @needs_service
    def _():
        for handle in ("no-such-task", "../x", "a" * 300):
            status, _, _ = host.call("GET", f"/coding/{handle}/events", A)
            assert status in (400, 404), f"{handle[:20]!r} -> {status}"

    @check("the host's own chat is unaffected (informational: supply --chat-path to test)", must_provide=False)
    def _():
        if not args.chat_path:
            return ("SKIP", "no --chat-path given")
        status, _, _ = host.call("POST", args.chat_path, A, json.loads(args.chat_body))
        assert status == 200, f"chat returned {status}"

    failed = [r for r in results if r["outcome"] == "FAIL"]
    skipped = [r for r in results if r["outcome"] == "SKIP" and r["must_provide"]]
    summary = {"pass": sum(r["outcome"] == "PASS" for r in results), "fail": len(failed), "skip": sum(r["outcome"] == "SKIP" for r in results)}
    print("summary:", summary)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump({"summary": summary, "results": results}, handle, ensure_ascii=False, indent=1)
    return 1 if failed or (skipped and not args.allow_skip) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True)
    parser.add_argument("--prefix", default="")
    parser.add_argument("--token-a", required=True)
    parser.add_argument("--token-b", required=True)
    parser.add_argument("--workspace-a", required=True)
    parser.add_argument("--workspace-denied", required=True)
    parser.add_argument("--text", default="list the project files")
    parser.add_argument("--auth-header", default="Authorization")
    parser.add_argument("--chat-path", default="")
    parser.add_argument("--chat-body", default='{"message": "hello"}')
    parser.add_argument("--allow-skip", action="store_true")
    parser.add_argument("--output")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
