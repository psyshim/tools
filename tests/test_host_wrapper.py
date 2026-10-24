"""The host-facing wrapper kit: the interface the host implements, the generic endpoints, and the black-box acceptance tool that
a host runs against its finished wrapper. The acceptance tool is itself tested against the sample host (good) and against
deliberately broken hosts (it must FAIL them)."""
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "host_client"))

import uvicorn
from fastapi.responses import JSONResponse

from examples.sample_host.app import SampleHost, create_app
from host_client import ROUTES, AgentGateway, CodingEndpoints, HostEnvironment
from host_client import acceptance
from testkit.mock import MockCodingService

BASE = "http://service.invalid"
CREDENTIAL = "svc-credential"

def mock_gateway(service):
    def transport(method, url, headers, body, timeout):
        status, payload = service.handle(method, url[len(BASE):], headers, json.loads(body) if body else None)
        return status, json.dumps(payload).encode()
    return AgentGateway(BASE, CREDENTIAL, transport=transport, sleep=lambda s: None)

def fresh_service():
    return MockCodingService({}, {"host:alice": {"w1"}, "host:bob": {"w2"}}, host_credentials={CREDENTIAL})

class Env(HostEnvironment):
    def __init__(self):
        self.enabled, self.log, self.boom, self.audit_boom = True, [], False, False

    def can_access_workspace(self, user, workspace_id):
        if self.boom:
            raise RuntimeError("acl backend down")
        return {"alice": {"w1"}, "bob": {"w2"}}.get(user, set()) >= {workspace_id}

    def coding_enabled(self, user):
        return self.enabled

    def audit(self, user, action, detail):
        self.log.append((user, action, detail))
        if self.audit_boom:
            raise RuntimeError("audit sink down")

START = {"workspace_id": "w1", "text": "propose x", "conversation_id": "c", "message_id": "m"}

class Dispatch(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.endpoints = CodingEndpoints(mock_gateway(fresh_service()), self.env)

    def call(self, method, path, user="alice", **kw):
        return self.endpoints.dispatch(method, path, user=user, **kw)

    def test_routes_are_the_documented_five(self):
        self.assertEqual([(r.method, r.path) for r in ROUTES], [("GET", "/coding/availability"), ("POST", "/coding/start"), ("GET", "/coding/{handle}/events"),
                                                                 ("POST", "/coding/{handle}/decision"), ("POST", "/coding/{handle}/cancel")])

    def test_start_poll_and_the_approval_card_carries_what_the_ui_needs(self):
        started = self.call("POST", "/coding/start", body={**START, "text": "approve it"})
        self.assertEqual(started.status, 200)
        events = self.call("GET", f"/coding/{started.body['handle']}/events", query={"after": "0"})
        card = [e for e in events.body["events"] if e["type"] == "approval_required"]
        self.assertTrue(card and {"action_id", "fingerprint"} <= set(card[0]["data"]), "the UI must be able to answer the card")
        decision = self.call("POST", f"/coding/{started.body['handle']}/decision", body={"action_id": card[0]["data"]["action_id"], "fingerprint": card[0]["data"]["fingerprint"], "approve": False})
        self.assertEqual((decision.status, decision.body), (200, {"ok": True}))
        self.assertEqual([a for _, a, _ in self.env.log], ["start", "decision"])
        self.assertTrue(all("text" not in d for _, _, d in self.env.log), "audit records ids only, never message text")

    def test_the_acl_is_local_and_a_failing_acl_means_no(self):
        self.assertEqual(self.call("POST", "/coding/start", user="bob", body=START).status, 403)
        self.env.boom = True
        self.assertEqual(self.call("POST", "/coding/start", body=START).status, 403)

    def test_a_failing_audit_hook_never_breaks_the_request(self):
        self.env.audit_boom = True
        self.assertEqual(self.call("POST", "/coding/start", body={**START, "message_id": "m2"}).status, 200)

    def test_the_feature_flag_hides_everything_but_availability(self):
        self.env.enabled = False
        self.assertEqual(self.call("GET", "/coding/availability").status, 200)
        for method, path, body in (("POST", "/coding/start", START), ("GET", "/coding/x/events", None), ("POST", "/coding/x/cancel", {})):
            self.assertEqual(self.call(method, path, body=body).body, {"ok": False, "error": "disabled"})

    def test_input_is_validated_before_anything_else(self):
        for bad in (None, {}, {**START, "text": ""}, {**START, "text": 5}, {**START, "message_id": "m" * 500}, {**START, "workspace_id": None}, []):
            self.assertEqual(self.call("POST", "/coding/start", body=bad).status, 400, bad)
        self.assertEqual(self.call("POST", "/coding/x/decision", body={"action_id": "a", "fingerprint": "f", "approve": "yes"}).status, 400)
        self.assertEqual(self.call("GET", "/coding/x/events", query={"after": "oops"}).status, 400)

    def test_other_users_see_not_found_and_unknown_routes_are_404(self):
        handle = self.call("POST", "/coding/start", body=START).body["handle"]
        self.assertEqual(self.call("GET", f"/coding/{handle}/events", user="bob").status, 404)
        for method, path in (("GET", "/other"), ("DELETE", "/coding/start"), ("GET", "/coding"), ("GET", "/coding/a/b/c"), ("POST", "/coding/availability")):
            self.assertEqual(self.call(method, path).status, 404, (method, path))

    def test_a_missing_user_is_401_and_a_dead_service_is_503(self):
        self.assertEqual(self.endpoints.dispatch("GET", "/coding/availability", user=None).status, 401)
        def down(*a):
            raise ConnectionRefusedError("x")
        dead = CodingEndpoints(AgentGateway(BASE, CREDENTIAL, transport=down, sleep=lambda s: None), self.env)
        reply = dead.dispatch("POST", "/coding/start", user="alice", body=START)
        self.assertEqual((reply.status, reply.body["error"]), (503, "unavailable"))
        self.assertEqual(dead.dispatch("GET", "/coding/availability", user="alice").status, 200, "availability answers even when the service is down")

    def test_the_environment_base_class_forces_the_acl(self):
        with self.assertRaises(NotImplementedError):
            HostEnvironment().can_access_workspace("u", "w")

def serve(app):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", lambda: (setattr(server, "should_exit", True), thread.join(10))

def acceptance_args(base, **extra):
    argv = ["--base", base, "--token-a", "token-alice", "--token-b", "token-bob", "--workspace-a", "w1", "--workspace-denied", "w2", "--text", "propose a change"]
    for key, value in extra.items():
        argv += [f"--{key.replace('_', '-')}", *([] if value is True else [str(value)])]
    return argv

class AcceptanceTool(unittest.TestCase):
    def run_against(self, app, **extra):
        base, stop = serve(app)
        self.addCleanup(stop)
        out = Path(__file__).with_name("_acceptance_tmp.json")
        self.addCleanup(lambda: out.exists() and out.unlink())
        code = acceptance.main(acceptance_args(base, output=str(out), **extra))
        return code, json.loads(out.read_text(encoding="utf-8"))

    def test_the_sample_host_passes_every_check(self):
        code, report = self.run_against(create_app(mock_gateway(MockCodingService({}, {"host:alice": {"w1"}, "host:bob": {"w2"}}, host_credentials={CREDENTIAL}))), chat_path="/chat")
        self.assertEqual((code, report["summary"]["fail"]), (0, 0), report)
        self.assertEqual(report["summary"]["skip"], 0, "a healthy host leaves nothing skipped")

    def test_with_the_service_down_the_tool_skips_and_says_so_instead_of_passing(self):
        def down(*a):
            raise ConnectionRefusedError("x")
        code, report = self.run_against(create_app(AgentGateway(BASE, CREDENTIAL, transport=down, sleep=lambda s: None)))
        self.assertEqual(code, 1, "skips are not a pass")
        self.assertGreaterEqual(report["summary"]["skip"], 4)
        code, report = self.run_against(create_app(AgentGateway(BASE, CREDENTIAL, transport=down, sleep=lambda s: None)), allow_skip=True)
        self.assertEqual(code, 0)

    def test_it_fails_a_host_whose_acl_is_missing(self):
        class OpenHost(SampleHost):
            def can_access_workspace(self, user, workspace_id):
                return True
        code, report = self.run_against(create_app(mock_gateway(fresh_service()), OpenHost()))
        self.assertEqual(code, 1)
        self.assertTrue(any(r["outcome"] == "FAIL" and "may not use" in r["check"] for r in report["results"]))

    def test_it_fails_a_host_that_leaks_one_users_task_to_another(self):
        app = create_app(mock_gateway(fresh_service()))
        # a broken wrapper: the same service identity for every user, so ownership is not enforced by the host's mapping
        broken = CodingEndpoints(mock_gateway(fresh_service()), SampleHost())
        original = broken.dispatch
        broken.dispatch = lambda method, path, **kw: original(method, path, **{**kw, "user": "alice"})
        from fastapi import FastAPI, Request
        leaky = FastAPI()
        @leaky.api_route("/{rest:path}", methods=["GET", "POST"])
        async def anything(rest: str, request: Request):
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer token-"):
                return JSONResponse({"ok": False}, status_code=401)
            body = None
            if request.method == "POST":
                try:
                    body = await request.json()
                except ValueError:
                    body = None
            reply = broken.dispatch(request.method, "/" + rest, user="alice", query=dict(request.query_params), body=body)
            return JSONResponse(reply.body, status_code=reply.status)
        code, report = self.run_against(leaky)
        self.assertEqual(code, 1)
        self.assertTrue(any(r["outcome"] == "FAIL" and "another user" in r["check"] for r in report["results"]), report)
        del app

if __name__ == "__main__":
    unittest.main()
