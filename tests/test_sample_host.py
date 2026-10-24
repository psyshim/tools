"""Phase 3 gates: a fake host with only the thin adapter, detachable from the coding service, and the same client working
against the REAL service surface (not only the mock)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "host_client"))

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from code_attach.protocol_router import make_protocol_router
from examples.sample_host.app import create_app
from host_client import AgentGateway
from testkit.mock import MockCodingService

BASE = "http://service.invalid"
CREDENTIAL = "svc-credential"
ALICE = {"authorization": "Bearer token-alice"}
BOB = {"authorization": "Bearer token-bob"}

def direct_to(client):
    """Gateway transport that sends the request through a Starlette TestClient (no sockets)."""
    def transport(method, url, headers, body, timeout):
        response = client.request(method, url[len(BASE):], headers=headers, content=body)
        return response.status_code, response.content
    return transport

def mock_transport(service):
    def transport(method, url, headers, body, timeout):
        status, payload = service.handle(method, url[len(BASE):], headers, json.loads(body) if body else None)
        return status, json.dumps(payload).encode()
    return transport

def gateway(transport):
    return AgentGateway(BASE, CREDENTIAL, transport=transport, sleep=lambda s: None)

class DetachableHost(unittest.TestCase):
    def test_host_boots_and_chats_with_the_coding_service_absent(self):
        def down(*args):
            raise ConnectionRefusedError("no service")
        host = TestClient(create_app(gateway(down)))
        self.assertEqual(host.post("/chat", json={"message": "hello"}, headers=ALICE).json()["reply"], "echo for alice: hello")
        self.assertEqual(host.get("/coding/availability", headers=ALICE).json(), {"available": False, "reason": "unreachable", "modes": []})
        started = host.post("/coding/start", json={"workspace_id": "w1", "text": "x", "conversation_id": "c", "message_id": "m"}, headers=ALICE).json()
        self.assertEqual((started["ok"], started["error"], started["reason"]), (False, "unavailable", "unreachable"))
        self.assertEqual(host.post("/chat", json={"message": "still fine"}, headers=ALICE).status_code, 200, "ordinary chat is unaffected")

    def test_the_host_acl_is_the_hosts_own_decision_and_costs_no_network_call(self):
        calls = []
        def spy(*args):
            calls.append(args)
            raise AssertionError("must not be reached")
        host = TestClient(create_app(gateway(spy)))
        refused = host.post("/coding/start", json={"workspace_id": "w1", "text": "x", "conversation_id": "c", "message_id": "m"}, headers=BOB).json()
        self.assertEqual(refused, {"ok": False, "error": "forbidden"})
        self.assertEqual(calls, [])

    def test_full_flow_through_the_host_with_the_mock(self):
        service = MockCodingService({}, {"host:alice": {"w1"}}, host_credentials={CREDENTIAL})
        host = TestClient(create_app(gateway(mock_transport(service))))
        self.assertTrue(host.get("/coding/availability", headers=ALICE).json()["available"])
        handle = host.post("/coding/start", json={"workspace_id": "w1", "text": "propose <b>x</b>", "conversation_id": "c", "message_id": "m"}, headers=ALICE).json()["handle"]
        polled = host.get(f"/coding/{handle}/events?after=0", headers=ALICE).json()
        self.assertTrue(polled["ok"] and polled["terminal"])
        self.assertEqual([e["type"] for e in polled["events"]], ["assistant_message", "proposal_completed", "state"])
        self.assertEqual(host.get(f"/coding/{handle}/events?after={polled['next']}", headers=ALICE).json()["events"], [])
        self.assertEqual(host.get(f"/coding/{handle}/events?after=oops", headers=ALICE).json(), {"ok": False, "error": "bad_request"})
        self.assertEqual(host.get(f"/coding/{handle}/events", headers=BOB).json(), {"ok": False, "error": "not_found"})
        waiting = host.post("/coding/start", json={"workspace_id": "w1", "text": "approve it", "conversation_id": "c", "message_id": "m2"}, headers=ALICE).json()["handle"]
        card = [e for e in host.get(f"/coding/{waiting}/events", headers=ALICE).json()["events"] if e["type"] == "approval_required"]
        self.assertTrue(card)
        self.assertEqual(host.post(f"/coding/{waiting}/cancel", headers=ALICE).json(), {"ok": True, "state": "CANCELLED"})

class ClientAgainstTheRealService(unittest.TestCase):
    """The same gateway code against code_attach's protocol router with the credential + X-Principal authentication the
    coding service will use. Read/propose by default; mutation only in the explicitly experimental configuration."""
    def build(self, allow_mutation):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        (root / "a.txt").write_text("alpha\n", encoding="utf-8")
        def authorize(request):
            if request.headers.get("authorization") != "Bearer " + CREDENTIAL:
                raise HTTPException(401)
            user = unquote(request.headers.get("x-principal", ""))
            if not user and not request.url.path.endswith("/availability"):
                raise HTTPException(401)
            return "host:" + user if user else "host-level"
        def model(messages, tools):
            done = sum(m["role"] == "assistant" for m in messages)
            text = messages[1]["text"]
            if text.startswith("propose") and done == 0:
                return {"text": "", "tool_calls": [{"id": "p1", "name": "propose_edit", "args": {"path": "a.txt", "old": "alpha", "new": "BETA"}}]}
            if text.startswith("approve") and done == 0:
                return {"text": "", "tool_calls": [{"id": "p1", "name": "propose_edit", "args": {"path": "a.txt", "old": "alpha", "new": "BETA"}}]}
            if text.startswith("approve") and done == 1:
                return {"text": "", "tool_calls": [{"id": "p2", "name": "apply_proposal", "args": {"proposal_id": json.loads(messages[-1]["content"]).get("proposal_id", "none")}}]}
            return {"text": "done", "tool_calls": []}
        app = FastAPI()
        router = make_protocol_router(model, authorize, lambda p, w: str(root) if (p, w) == ("host:alice", "w1") else None, allow_mutation=allow_mutation)
        app.include_router(router)
        self.addCleanup(lambda: [t.cancel() or t.thread.join(5) for t in list(router.engine._tasks.values())])
        return gateway(direct_to(TestClient(app))), root

    def test_propose_flow_end_to_end_without_any_mutation(self):
        gw, root = self.build(False)
        self.assertEqual((gw.availability().modes, gw.availability().mutation), (("read", "propose"), False))
        handle = gw.start_coding("alice", "w1", "propose a change", conversation_id="c", message_id="m")
        events = list(gw.observe_coding("alice", handle, poll_interval=0.01, max_seconds=20))
        types = [e.type for e in events]
        self.assertIn("proposal_completed", types)
        self.assertNotIn("mutation_applied", types)
        self.assertEqual(events[-1].data["state"], "COMPLETED")
        self.assertEqual((root / "a.txt").read_text(encoding="utf-8"), "alpha\n")
        self.assertEqual(handle, gw.start_coding("alice", "w1", "propose a change", conversation_id="c", message_id="m"), "same message, same task")

    def test_experimental_mutation_flow_through_the_client(self):
        gw, root = self.build(True)
        handle = gw.start_coding("alice", "w1", "approve this", conversation_id="c", message_id="m")
        card = None
        for event in gw.observe_coding("alice", handle, poll_interval=0.01, max_seconds=20):
            if event.type == "approval_required":
                card = event.data
                break
        self.assertIsNotNone(card)
        self.assertEqual((root / "a.txt").read_text(encoding="utf-8"), "alpha\n", "nothing is written before the decision")
        self.assertTrue(gw.submit_decision("alice", handle, card["action_id"], card["fingerprint"], True).accepted)
        events = list(gw.observe_coding("alice", handle, resume_token=0, poll_interval=0.01, max_seconds=20))
        self.assertIn("mutation_applied", [e.type for e in events])
        self.assertEqual((root / "a.txt").read_text(encoding="utf-8"), "BETA\n")

    def test_other_users_and_unknown_workspaces_are_refused_by_the_service(self):
        gw, _ = self.build(False)
        from host_client import ProtocolRefused
        with self.assertRaises(ProtocolRefused) as caught:
            gw.start_coding("mallory", "w1", "propose x", conversation_id="c", message_id="m")
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        handle = gw.start_coding("alice", "w1", "propose x", conversation_id="c", message_id="m1")
        with self.assertRaises(ProtocolRefused) as caught:
            list(gw.observe_coding("mallory", handle, poll_interval=0))
        self.assertEqual(caught.exception.code, "NOT_FOUND")

    def test_wrong_service_credential_is_reported_as_a_configuration_problem(self):
        gw, _ = self.build(False)
        bad = AgentGateway(BASE, "wrong", transport=gw._transport, sleep=lambda s: None)
        self.assertEqual(bad.availability().reason, "credential_rejected")

if __name__ == "__main__":
    unittest.main()
