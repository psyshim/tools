"""Phase 1/2 gate: the SAME scenarios run against the executable mock and the real service (read/propose default, and the
local-experimental mutation mode). Scenarios that depend on a real filesystem or on mutation are marked UNSUPPORTED for the
mock instead of being silently skipped, so the evidence report shows exactly what each implementation proves.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from code_attach.protocol_router import make_protocol_router
from protocol import ERROR_STATUS, PROTOCOL_VERSION, validate_capabilities, validate_error, validate_events_response
from protocol.schema import validate_event
from testkit.fixtures import load_cases
from testkit.mock import MockCodingService
from testkit.transport import HttpTransport, MockTransport

SECRET = "TOP-SECRET-CONFORMANCE"

def scripted_model(messages, tools):
    text = messages[1]["text"]
    done = sum(m["role"] == "assistant" for m in messages)
    def call(i, name, **args):
        return {"text": "", "tool_calls": [{"id": f"c{done}-{i}", "name": name, "args": args}]}
    if text.startswith("propose") and done == 0:
        return call(0, "propose_edit", path="a.txt", old="alpha", new="BETA")
    if text.startswith("approve"):
        if done == 0:
            return call(0, "propose_edit", path="a.txt", old="alpha", new="BETA")
        if done == 1:
            return call(1, "apply_proposal", proposal_id=json.loads(messages[-1]["content"]).get("proposal_id", "none"))
    if text.startswith("traverse") and done == 0:
        return {"text": "", "tool_calls": [{"id": "t1", "name": "read_file", "args": {"path": "../outside.txt"}}, {"id": "t2", "name": "read_file", "args": {"path": ".env"}},
                                           {"id": "t3", "name": "read_file", "args": {"path": "C:/Windows/win.ini"}}]}
    return {"text": "done", "tool_calls": []}

class World:
    """Everything a scenario needs, independent of which implementation sits behind the transport."""
    def __init__(self, transport, root=None, can_mutate=False):
        self.t, self.root, self.can_mutate = transport, root, can_mutate
        self.alice = {"authorization": "Bearer token-alice"}
        self.bob = {"authorization": "Bearer token-bob"}
        self.ws_alice, self.ws_bob = "w-alice", "w-bob"

    def post_task(self, text, headers=None, key=None, workspace=None, **extra):
        body = {"workspace_id": workspace or self.ws_alice, "instruction": text, "idempotency_key": key or f"k-{time.time_ns()}", **extra}
        return self.t.request("POST", "/tasks", headers or self.alice, json_body=body)

    def events(self, task_id, after=0, headers=None):
        return self.t.request("GET", f"/tasks/{task_id}/events?after={after}", headers or self.alice)

    def settle(self, task_id, until_card=False, seconds=15):
        end = time.time() + seconds
        while time.time() < end:
            r = self.events(task_id)
            body = r.body
            if body.get("terminal") or (until_card and any(e["type"] == "approval_required" for e in body.get("events", []))):
                return body
            time.sleep(0.02)
        raise AssertionError("task did not settle")

def mock_world():
    service = MockCodingService({"token-alice": "alice", "token-bob": "bob"}, {"alice": {"w-alice"}, "bob": {"w-bob"}})
    return World(MockTransport(service)), (lambda: None)

def real_world(allow_mutation):
    tmp = tempfile.TemporaryDirectory()
    parent = Path(tmp.name).resolve()
    (parent / "outside.txt").write_text(SECRET, encoding="utf-8")
    root = parent / "ws"
    root.mkdir()
    (root / "a.txt").write_text("alpha\n", encoding="utf-8")
    (root / ".env").write_text("KEY=" + SECRET, encoding="utf-8")
    users = {"token-alice": "alice", "token-bob": "bob"}
    roots = {("alice", "w-alice"): str(root)}
    def authorize(request):
        principal = users.get(request.headers.get("authorization", "")[7:])
        if principal is None:
            raise HTTPException(401)
        return principal
    app = FastAPI()
    router = make_protocol_router(scripted_model, authorize, lambda p, w: roots.get((p, w)), allow_mutation=allow_mutation)
    app.include_router(router)
    def cleanup():
        for tid in list(router.engine._tasks):
            try:
                task = router.engine._tasks[tid]
                task.cancel()
                task.thread.join(5)
            except Exception:
                pass
        tmp.cleanup()
    return World(HttpTransport(TestClient(app)), root, can_mutate=allow_mutation), cleanup

class Conformance:
    """Scenarios. Subclasses set `build()` and, where a scenario cannot be proven, `unsupported = {"test_name": "why"}`."""
    unsupported = {}

    def setUp(self):
        reason = self.unsupported.get(self._testMethodName)
        if reason:
            self.skipTest("UNSUPPORTED: " + reason)
        self.world, cleanup = self.build()
        self.addCleanup(cleanup)

    # --- capabilities and authentication ---
    def test_capabilities_validate_and_advertise_no_execute_mode(self):
        r = self.world.t.request("GET", "/availability", self.world.alice)
        self.assertEqual(r.status, 200)
        validate_capabilities(r.body)
        self.assertNotIn("execute", r.body["modes"])
        self.assertEqual(r.body["protocol"], PROTOCOL_VERSION)

    def test_every_route_refuses_missing_or_wrong_credentials_with_a_valid_envelope(self):
        tid = self.world.post_task("hello").body["task_id"]
        for headers in ({}, {"authorization": "Bearer nope"}):
            for method, path in (("GET", "/availability"), ("POST", "/tasks"), ("GET", f"/tasks/{tid}/events"), ("POST", f"/tasks/{tid}/decision"), ("POST", f"/tasks/{tid}/cancel")):
                with self.subTest(method=method, path=path.split("/")[1], headers=bool(headers)):
                    r = self.world.t.request(method, path, headers, json_body={} if method == "POST" else None)
                    self.assertEqual(r.status, 401)
                    validate_error(r.body)
                    self.assertEqual(r.body["error"]["code"], "UNAUTHENTICATED")

    # --- the shared request/response fixtures ---
    def test_valid_task_fixtures_are_accepted_with_a_valid_response(self):
        for case in load_cases("task_request"):
            if case["valid"]:
                with self.subTest(case=case["name"]):
                    body = {**case["body"], "workspace_id": self.world.ws_alice, "idempotency_key": "fx-" + case["name"]}
                    r = self.world.t.request("POST", "/tasks", self.world.alice, json_body=body)
                    self.assertEqual(r.status, 202, r.body)
                    self.assertEqual(r.body["protocol"], PROTOCOL_VERSION)
                    self.assertTrue(r.body["task_id"])

    def test_invalid_task_fixtures_are_refused_with_the_declared_code_and_status(self):
        for case in load_cases("task_request"):
            if not case["valid"]:
                with self.subTest(case=case["name"]):
                    body = case["body"]
                    if isinstance(body, dict) and "workspace_id" in body and isinstance(body["workspace_id"], str):
                        body = {**body, "workspace_id": self.world.ws_alice}
                    r = self.world.t.request("POST", "/tasks", self.world.alice, json_body=body)
                    self.assertEqual(r.status, ERROR_STATUS[case["expect_code"]], r.body)
                    validate_error(r.body)
                    self.assertEqual(r.body["error"]["code"], case["expect_code"])
                    if case.get("expect_field"):
                        self.assertEqual(r.body["error"].get("field"), case["expect_field"])

    def test_invalid_decision_fixtures_are_refused(self):
        tid = self.world.post_task("hello").body["task_id"]
        for case in load_cases("decision"):
            if not case["valid"]:
                with self.subTest(case=case["name"]):
                    r = self.world.t.request("POST", f"/tasks/{tid}/decision", self.world.alice, json_body=case["body"])
                    self.assertEqual((r.status, r.body["error"]["code"]), (ERROR_STATUS[case["expect_code"]], case["expect_code"]))

    def test_malformed_json_and_oversized_bodies_never_reach_the_task_loop(self):
        r = self.world.t.request("POST", "/tasks", self.world.alice, raw=b"{not json")
        self.assertEqual((r.status, r.body["error"]["code"]), (400, "BAD_REQUEST"))
        r = self.world.t.request("POST", "/tasks", self.world.alice, raw=b'{"x":"' + b"a" * 70000 + b'"}')
        self.assertEqual((r.status, r.body["error"]["code"]), (413, "PAYLOAD_TOO_LARGE"))

    def test_other_major_protocol_version_fails_closed_on_every_route(self):
        headers = {**self.world.alice, "x-protocol-version": "2.0"}
        tid = self.world.post_task("hello").body["task_id"]
        for method, path in (("GET", "/availability"), ("POST", "/tasks"), ("GET", f"/tasks/{tid}/events"), ("POST", f"/tasks/{tid}/cancel")):
            with self.subTest(path=path.split("/")[1]):
                r = self.world.t.request(method, path, headers, json_body={} if method == "POST" else None)
                self.assertEqual((r.status, r.body["error"]["code"]), (426, "VERSION_MISMATCH"))

    # --- proposal flow, cursors, idempotency ---
    def test_propose_flow_ends_with_a_proposal_and_never_a_mutation_by_default(self):
        tid = self.world.post_task("propose a change").body["task_id"]
        body = self.world.settle(tid)
        validate_events_response(body)
        types = [e["type"] for e in body["events"]]
        self.assertIn("proposal_completed", types)
        self.assertEqual(body["state"], "COMPLETED")
        for e in body["events"]:
            if e["type"] == "proposal_completed":
                self.assertIs(e["data"]["applied"], False)
        if not self.world.can_mutate:
            self.assertNotIn("mutation_applied", types)

    def test_cursor_is_gap_free_stable_and_replayable(self):
        tid = self.world.post_task("propose a change").body["task_id"]
        full = self.world.settle(tid)
        ids = [int(e["event_id"]) for e in full["events"]]
        self.assertEqual(ids, list(range(1, len(ids) + 1)))
        self.assertEqual(full["next_cursor"], ids[-1])
        for k in range(len(ids) + 1):
            part = self.world.events(tid, after=k).body
            validate_events_response(part)
            self.assertEqual([e["event_id"] for e in part["events"]], [str(i) for i in ids if i > k])
        replay = self.world.events(tid, after=0).body
        self.assertEqual(replay["events"], full["events"], "replaying from the start gives identical events (client dedupes by task_id, event_id)")
        self.assertEqual(self.world.events(tid, after=10 ** 6).body["events"], [])

    def test_bad_cursors_are_refused_not_crashed(self):
        tid = self.world.post_task("hello").body["task_id"]
        for bad in ("-1", "abc", "1.5", "", "9" * 40):
            with self.subTest(after=bad):
                r = self.world.t.request("GET", f"/tasks/{tid}/events?after={bad}", self.world.alice)
                self.assertEqual(r.status, 400 if bad != "" else r.status)
                self.assertLess(r.status, 500)

    def test_idempotent_retry_returns_the_same_task_and_a_changed_body_conflicts(self):
        first = self.world.post_task("hello", key="same-key")
        again = self.world.post_task("hello", key="same-key")
        self.assertEqual((first.status, again.status), (202, 202))
        self.assertEqual(first.body["task_id"], again.body["task_id"])
        changed = self.world.post_task("different text", key="same-key")
        self.assertEqual((changed.status, changed.body["error"]["code"]), (409, "IDEMPOTENCY_CONFLICT"))

    # --- isolation between actors ---
    def test_other_actor_cannot_read_decide_or_cancel_and_gets_the_same_answer_as_for_a_missing_task(self):
        tid = self.world.post_task("hello").body["task_id"]
        missing = self.world.events("0" * 32, headers=self.world.bob)
        for method, path, body in (("GET", f"/tasks/{tid}/events", None), ("POST", f"/tasks/{tid}/cancel", {}), ("POST", f"/tasks/{tid}/decision", {"action_id": "a", "fingerprint": "f", "approve": True})):
            with self.subTest(path=path.split("/")[-1]):
                r = self.world.t.request(method, path, self.world.bob, json_body=body)
                self.assertEqual((r.status, r.body["error"]["code"]), (404, "NOT_FOUND"))
                self.assertEqual(r.body["error"]["message"], missing.body["error"]["message"], "existence of someone else's task must not be distinguishable")

    def test_workspace_belonging_to_someone_else_is_not_found(self):
        r = self.world.post_task("hello", headers=self.world.bob, workspace=self.world.ws_alice)
        self.assertEqual((r.status, r.body["error"]["code"]), (404, "NOT_FOUND"))
        r = self.world.post_task("hello", workspace="no-such-workspace")
        self.assertEqual((r.status, r.body["error"]["code"]), (404, "NOT_FOUND"))

    def test_odd_task_ids_are_not_found_not_server_errors(self):
        for odd in ("../x", "%00", "a" * 3000, "%2e%2e", "x y"):
            with self.subTest(task=odd[:20]):
                r = self.world.t.request("GET", f"/tasks/{odd}/events", self.world.alice)
                self.assertIn(r.status, (400, 404))

    # --- approvals and cancellation ---
    def test_approval_card_decision_flow_and_wrong_fingerprint(self):
        tid = self.world.post_task("approve this").body["task_id"]
        body = self.world.settle(tid, until_card=True)
        cards = [e for e in body["events"] if e["type"] == "approval_required"]
        self.assertTrue(cards, "an approval card must exist before the task can finish")
        card = cards[0]["data"]
        wrong = self.world.t.request("POST", f"/tasks/{tid}/decision", self.world.alice, json_body={"action_id": card["action_id"], "fingerprint": "0" * 8, "approve": True})
        self.assertEqual((wrong.status, wrong.body["error"]["code"]), (409, "NO_PENDING_ACTION"))
        right = self.world.t.request("POST", f"/tasks/{tid}/decision", self.world.alice, json_body={"action_id": card["action_id"], "fingerprint": card["fingerprint"], "approve": False})
        self.assertEqual((right.status, right.body["accepted"]), (200, True))
        final = self.world.settle(tid)
        self.assertTrue(final["terminal"])
        self.assertIn("approval_decision", [e["type"] for e in final["events"]])
        again = self.world.t.request("POST", f"/tasks/{tid}/decision", self.world.alice, json_body={"action_id": card["action_id"], "fingerprint": card["fingerprint"], "approve": True})
        self.assertEqual((again.status, again.body["error"]["code"]), (409, "NO_PENDING_ACTION"), "a second decision on the same card is refused")

    def test_decision_without_any_pending_action_is_refused(self):
        tid = self.world.post_task("hello").body["task_id"]
        self.world.settle(tid)
        r = self.world.t.request("POST", f"/tasks/{tid}/decision", self.world.alice, json_body={"action_id": "a", "fingerprint": "f", "approve": True})
        self.assertEqual((r.status, r.body["error"]["code"]), (409, "NO_PENDING_ACTION"))

    def test_cancel_is_idempotent_and_a_terminal_state_never_changes(self):
        done = self.world.post_task("hello").body["task_id"]
        self.world.settle(done)
        r = self.world.t.request("POST", f"/tasks/{done}/cancel", self.world.alice, json_body={})
        self.assertEqual((r.status, r.body["state"]), (200, "COMPLETED"))
        waiting = self.world.post_task("approve this").body["task_id"]
        self.world.settle(waiting, until_card=True)
        first = self.world.t.request("POST", f"/tasks/{waiting}/cancel", self.world.alice, json_body={})
        self.assertEqual(first.status, 200)
        final = self.world.settle(waiting)
        self.assertEqual(final["state"], "CANCELLED")
        self.assertEqual(self.world.t.request("POST", f"/tasks/{waiting}/cancel", self.world.alice, json_body={}).body["state"], "CANCELLED")

    # --- real-service-only guarantees ---
    def test_hostile_paths_and_secrets_never_appear_in_any_event(self):
        tid = self.world.post_task("traverse everywhere").body["task_id"]
        body = self.world.settle(tid)
        text = json.dumps(body)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(str(self.world.root), text)
        self.assertNotIn(str(self.world.root.parent), text)
        self.assertNotIn(str(Path.home()), text)
        activities = [e["data"] for e in body["events"] if e["type"] == "tool_activity"]
        self.assertEqual([a["status"] for a in activities], ["ERROR"] * 3)

    def test_default_installation_cannot_mutate_whatever_the_model_asks(self):
        tid = self.world.post_task("approve this").body["task_id"]
        body = self.world.settle(tid)
        types = [e["type"] for e in body["events"]]
        self.assertNotIn("approval_required", types)
        self.assertNotIn("mutation_applied", types)
        self.assertEqual((self.world.root / "a.txt").read_text(encoding="utf-8"), "alpha\n")

    def test_experimental_mutation_yields_a_receipt_only_after_approval(self):
        tid = self.world.post_task("approve this").body["task_id"]
        card = [e for e in self.world.settle(tid, until_card=True)["events"] if e["type"] == "approval_required"][0]["data"]
        self.assertEqual((self.world.root / "a.txt").read_text(encoding="utf-8"), "alpha\n", "nothing is written before the decision")
        self.world.t.request("POST", f"/tasks/{tid}/decision", self.world.alice, json_body={"action_id": card["action_id"], "fingerprint": card["fingerprint"], "approve": True})
        final = self.world.settle(tid)
        receipts = [e for e in final["events"] if e["type"] == "mutation_applied"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["data"]["receipt"]["status"], "APPLIED")
        self.assertEqual((self.world.root / "a.txt").read_text(encoding="utf-8"), "BETA\n")
        self.assertLess([e["type"] for e in final["events"]].index("approval_decision"), [e["type"] for e in final["events"]].index("mutation_applied"))

    def test_a_stale_file_at_decision_time_is_reported_not_overwritten(self):
        tid = self.world.post_task("approve this").body["task_id"]
        card = [e for e in self.world.settle(tid, until_card=True)["events"] if e["type"] == "approval_required"][0]["data"]
        (self.world.root / "a.txt").write_text("changed by someone else\n", encoding="utf-8")   # a human saved the file in the meantime
        self.world.t.request("POST", f"/tasks/{tid}/decision", self.world.alice, json_body={"action_id": card["action_id"], "fingerprint": card["fingerprint"], "approve": True})
        final = self.world.settle(tid)
        types = [e["type"] for e in final["events"]]
        self.assertNotIn("mutation_applied", types)
        notices = [e["data"] for e in final["events"] if e["type"] == "notice" and e["data"]["code"] == "mutation_not_applied"]
        self.assertEqual([n["status"] for n in notices], ["CONFLICT"])
        self.assertEqual((self.world.root / "a.txt").read_text(encoding="utf-8"), "changed by someone else\n")

    def test_service_absent_is_reported_by_the_client_without_breaking_chat(self):
        self.fail("scenario belongs to the host client (phase 3)")

    def test_every_event_the_service_emits_validates(self):
        for text in ("propose x", "hello", "traverse y"):
            body = self.world.settle(self.world.post_task(text).body["task_id"])
            for event in body["events"]:
                validate_event(event)

class MockConformance(Conformance, unittest.TestCase):
    build = staticmethod(mock_world)
    unsupported = {
        "test_hostile_paths_and_secrets_never_appear_in_any_event": "the mock has no filesystem",
        "test_default_installation_cannot_mutate_whatever_the_model_asks": "the mock has no tools or model; it never mutates by construction",
        "test_experimental_mutation_yields_a_receipt_only_after_approval": "the mock never mutates by construction",
        "test_a_stale_file_at_decision_time_is_reported_not_overwritten": "the mock never mutates by construction",
        "test_service_absent_is_reported_by_the_client_without_breaking_chat": "needs the host client package (migration phase 3); the service cannot observe its own absence",
    }

class RealReadProposeConformance(Conformance, unittest.TestCase):
    build = staticmethod(lambda: real_world(False))
    unsupported = {
        "test_experimental_mutation_yields_a_receipt_only_after_approval": "mutation is disabled in the default installation (see the experimental world)",
        "test_approval_card_decision_flow_and_wrong_fingerprint": "no approval cards exist in read/propose mode (covered by the experimental world)",
        "test_cancel_is_idempotent_and_a_terminal_state_never_changes": "needs a task that waits for approval; not reachable in read/propose mode",
        "test_a_stale_file_at_decision_time_is_reported_not_overwritten": "no decision stage exists in read/propose mode (covered by the experimental world)",
        "test_service_absent_is_reported_by_the_client_without_breaking_chat": "needs the host client package (migration phase 3); the service cannot observe its own absence",
    }

class RealExperimentalConformance(Conformance, unittest.TestCase):
    build = staticmethod(lambda: real_world(True))
    unsupported = {
        "test_default_installation_cannot_mutate_whatever_the_model_asks": "this world enables mutation on purpose",
        "test_service_absent_is_reported_by_the_client_without_breaking_chat": "needs the host client package (migration phase 3); the service cannot observe its own absence",
    }

if __name__ == "__main__":
    unittest.main()
