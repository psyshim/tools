"""A ready-made test suite for the host's wrapper. Standard library only; runs inside the host's own test run, in-process, with no
network and no real coding service (a fake one is bundled).

Write one small harness and subclass the suite:

    import unittest
    from host_client.testing import WrapperContract

    class MyWrapperTests(WrapperContract, unittest.TestCase):
        USER_A, USER_B = "alice", "bob"               # ids exactly as your login produces them
        WORKSPACE_A, WORKSPACE_B = "w-alice", "w-bob"  # A may use only the first, B only the second (your ACL decides)
        CHAT = ("POST", "/chat", {"message": "hi"})   # optional: your ordinary chat route, checked while the service is broken

        def make_host(self, gateway):
            app = build_my_app(gateway)               # your app/wrapper, wired to the gateway you are given
            return MyHarness(app)

    class MyHarness:
        def request(self, method, path, user, body=None, query=None):
            ...   # call your app AS that user (user=None means not logged in); return (http_status, parsed_json_or_None)

Everything the suite asserts is specified in docs/HOST_WRAPPER_SPEC.md. A failing test names the rule it protects.
"""
import json
import re

from . import AgentGateway

try:
    from .fake_service import MockCodingService        # bundled in the kit
except ImportError:                                      # running from the repository
    from testkit.mock import MockCodingService

BASE = "http://coding-service.invalid"
CREDENTIAL = "wrapper-test-credential"
ABSOLUTE_PATH = re.compile(r"[A-Za-z]:[\\/]|/(?:Users|home)/")


class Wire:
    """Transport between the host's gateway and the fake service; counts calls and can break the service on demand."""

    def __init__(self, service):
        self.service, self.calls, self.mode, self.seen_headers = service, 0, "ok", []

    def __call__(self, method, url, headers, body, timeout):
        self.calls += 1
        self.seen_headers.append(dict(headers))
        if self.mode == "down":
            raise ConnectionRefusedError("service down")
        if self.mode == "reject_credential":
            return 401, b'{"error": {"code": "UNAUTHENTICATED", "message": "no"}}'
        if self.mode == "html":
            return 502, b"<html><body>Bad Gateway at C:\\inetpub\\secret</body></html>"
        if self.mode == "garbage":
            return 200, b"\x00not json at all"
        status, payload = self.service.handle(method, url[len(BASE):], headers, json.loads(body) if body else None)
        return status, json.dumps(payload).encode("utf-8")


class WrapperContract:
    USER_A = USER_B = WORKSPACE_A = WORKSPACE_B = None
    CHAT = None          # optional (method, path, body) of the host's ordinary chat route

    def make_host(self, gateway):
        raise NotImplementedError("return an object with request(method, path, user, body=None, query=None) -> (status, json)")

    # ---------------------------------------------------------------- plumbing
    def setUp(self):
        for name in ("USER_A", "USER_B", "WORKSPACE_A", "WORKSPACE_B"):
            if not getattr(self, name):
                self.fail(f"set {name} on your test class")
        self.service = MockCodingService({}, {f"host:{self.USER_A}": {self.WORKSPACE_A}, f"host:{self.USER_B}": {self.WORKSPACE_B}}, host_credentials={CREDENTIAL})
        self.wire = Wire(self.service)
        self.host = self.make_host(AgentGateway(BASE, CREDENTIAL, transport=self.wire, sleep=lambda s: None))
        self.responses = []

    def tearDown(self):
        for status, body in self.responses:
            self.assertLess(status, 500 if status != 503 else 504, f"the wrapper answered {status}: a failure must be a typed 4xx/503, not a crash")
            self.assertIsNone(ABSOLUTE_PATH.search(json.dumps(body)), "an absolute path reached the browser-facing response")
            self.assertNotIn(CREDENTIAL, json.dumps(body), "the service credential reached the browser-facing response")

    def call(self, method, path, user=None, body=None, query=None):
        status, payload = self.host.request(method, path, user, body=body, query=query)
        self.responses.append((status, payload))
        return status, payload

    def start(self, user=None, workspace=None, text="propose a change", message_id="m1", conversation_id="c1"):
        return self.call("POST", "/coding/start", user or self.USER_A,
                         {"workspace_id": workspace or self.WORKSPACE_A, "text": text, "conversation_id": conversation_id, "message_id": message_id})

    def events(self, handle, user=None, after="0"):
        return self.call("GET", f"/coding/{handle}/events", user or self.USER_A, query={"after": after})

    # ---------------------------------------------------------------- login (the host's own responsibility)
    def test_01_not_logged_in_is_refused_everywhere(self):
        """SPEC 6.3: authentication belongs to the host; the coding routes must not be reachable anonymously."""
        for method, path, body in (("GET", "/coding/availability", None), ("POST", "/coding/start", {"workspace_id": self.WORKSPACE_A, "text": "x", "conversation_id": "c", "message_id": "m"}),
                                   ("GET", "/coding/h/events", None), ("POST", "/coding/h/cancel", {})):
            status, _ = self.call(method, path, None, body)
            self.assertIn(status, (401, 403), f"{method} {path} answered {status} to an anonymous caller")
        self.assertEqual(self.wire.calls, 0, "an anonymous request reached the coding service")

    # ---------------------------------------------------------------- availability and degradation
    def test_02_availability_shape(self):
        status, body = self.call("GET", "/coding/availability", self.USER_A)
        self.assertEqual(status, 200)
        self.assertIs(body["available"], True)
        self.assertIsInstance(body["modes"], list)
        self.assertIs(body["mutation"], False)

    def test_03_service_down_is_a_typed_503_and_ordinary_chat_still_works(self):
        """SPEC 6.2: the coding feature may fail; the host's chat may not."""
        self.wire.mode = "down"
        status, body = self.call("GET", "/coding/availability", self.USER_A)
        self.assertEqual((status, body["available"]), (200, False), "availability answers even when the service is down")
        status, body = self.start()
        self.assertEqual((status, body["error"]), (503, "unavailable"))
        if self.CHAT:
            method, path, chat_body = self.CHAT
            status, _ = self.call(method, path, self.USER_A, chat_body)
            self.assertEqual(status, 200, "ordinary chat must keep working while the coding service is down")

    def test_04_a_rejected_service_credential_or_garbage_answer_is_unavailable_not_a_crash(self):
        for mode in ("reject_credential", "html", "garbage"):
            self.wire.mode = mode
            status, body = self.start(message_id="m-" + mode)
            self.assertEqual((status, body["error"]), (503, "unavailable"), mode)
            self.assertNotIn("inetpub", json.dumps(body), "raw server text leaked")

    # ---------------------------------------------------------------- start and authorisation
    def test_05_start_returns_a_handle_and_retries_do_not_create_second_tasks(self):
        status, first = self.start()
        self.assertEqual((status, first["ok"]), (200, True))
        self.assertEqual(self.start()[1]["handle"], first["handle"], "same conversation+message must be the same task")
        self.assertNotEqual(self.start(message_id="m2")[1]["handle"], first["handle"])

    def test_06_a_workspace_the_user_may_not_use_is_refused_locally(self):
        """SPEC 6.3: the host's ACL decides, before and without the service."""
        before = self.wire.calls
        status, body = self.start(workspace=self.WORKSPACE_B)
        self.assertEqual((status, body["error"]), (403, "forbidden"))
        self.assertEqual(self.wire.calls, before, "the ACL decision must not cost a service call")

    def test_07_malformed_input_is_400_not_500(self):
        good = {"workspace_id": self.WORKSPACE_A, "text": "x", "conversation_id": "c", "message_id": "m"}
        for bad in (None, {}, [], {**good, "text": ""}, {**good, "text": 7}, {**good, "workspace_id": None}, {**good, "message_id": "m" * 500}, {**good, "text": "t" * 30000}):
            status, body = self.call("POST", "/coding/start", self.USER_A, bad)
            self.assertEqual(status, 400, f"{str(bad)[:50]} -> {status}")
        self.assertEqual(self.wire.calls, 0, "invalid requests must be stopped before the service")

    def test_08_the_end_user_identity_reaches_the_service_not_the_host_credential_alone(self):
        self.start()
        sent = [h for h in self.wire.seen_headers if h.get("X-Principal") or h.get("x-principal")]
        self.assertTrue(sent, "the service must be told which user is asking (X-Principal)")

    # ---------------------------------------------------------------- isolation between users
    def test_09_another_user_cannot_read_cancel_or_decide(self):
        handle = self.start(text="approve it")[1]["handle"]
        self.assertEqual(self.events(handle, user=self.USER_B)[0], 404)
        self.assertEqual(self.call("POST", f"/coding/{handle}/cancel", self.USER_B, {})[0], 404)
        status, _ = self.call("POST", f"/coding/{handle}/decision", self.USER_B, {"action_id": "mock-action-1", "fingerprint": "mock-fingerprint-1", "approve": True})
        self.assertEqual(status, 404)
        self.assertEqual(self.events(handle)[1]["terminal"], False, "the owner's task is untouched")

    def test_10_unknown_and_odd_handles_are_4xx(self):
        for handle in ("no-such-task", "..", "a" * 300, "x%20y"):
            status, _ = self.events(handle)
            self.assertIn(status, (400, 404), handle[:20])

    # ---------------------------------------------------------------- events
    def test_11_events_are_ordered_resumable_and_terminal(self):
        handle = self.start()[1]["handle"]
        status, body = self.events(handle)
        self.assertEqual((status, body["terminal"]), (200, True))
        ids = [e["id"] for e in body["events"] if not e["synthetic"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([e["type"] for e in body["events"]][-1], "state")
        again = self.events(handle, after=body["next"])[1]
        self.assertEqual(again["events"], [], "resuming from `next` must not repeat events")
        for e in body["events"]:
            self.assertIn(e["type"], {"assistant_message", "tool_activity", "proposal_completed", "approval_required", "approval_decision",
                                      "mutation_applied", "test_result", "notice", "state", "gap"})
        self.assertNotIn("mutation_applied", [e["type"] for e in body["events"]], "a proposal is not a change")

    def test_12_a_bad_cursor_is_400(self):
        handle = self.start()[1]["handle"]
        self.assertEqual(self.events(handle, after="oops")[0], 400)

    def test_13_hostile_event_text_is_escaped_and_bounded(self):
        """SPEC 5: render `html`, never raw data. The fake service sends markup, control characters and a 60 kB body."""
        handle = self.start(text="hostile content")[1]["handle"]
        events = self.events(handle)[1]["events"]
        shown = next(e for e in events if e["type"] == "assistant_message" and "script" in json.dumps(e).lower())
        self.assertNotIn("<script", shown["html"].lower(), "markup reached the browser unescaped")
        self.assertNotIn("<img", shown["html"].lower())
        self.assertLess(len(shown["html"]), 40000, "an unbounded event body reached the browser")
        self.assertFalse(any(ord(c) < 32 and c not in "\n\t" for c in shown["html"]), "control characters reached the browser")

    # ---------------------------------------------------------------- approvals
    def test_14_the_approval_card_carries_what_the_ui_needs_and_decisions_are_exact(self):
        handle = self.start(text="approve it")[1]["handle"]
        events = self.events(handle)[1]["events"]
        card = next(e for e in events if e["type"] == "approval_required")["data"]
        self.assertTrue(card["action_id"] and card["fingerprint"], "the UI cannot answer a card that lacks action_id/fingerprint")
        path = f"/coding/{handle}/decision"
        wrong = self.call("POST", path, self.USER_A, {"action_id": card["action_id"], "fingerprint": "wrong", "approve": True})
        self.assertEqual(wrong[0], 409, "a wrong fingerprint must not be accepted")
        for bad in ({}, {"action_id": card["action_id"], "fingerprint": card["fingerprint"], "approve": "yes"}, {"action_id": 5, "fingerprint": "f", "approve": True}):
            self.assertEqual(self.call("POST", path, self.USER_A, bad)[0], 400, str(bad))
        right = self.call("POST", path, self.USER_A, {"action_id": card["action_id"], "fingerprint": card["fingerprint"], "approve": False})
        self.assertEqual((right[0], right[1]["ok"]), (200, True))
        self.assertEqual(self.call("POST", path, self.USER_A, {"action_id": card["action_id"], "fingerprint": card["fingerprint"], "approve": True})[0], 409, "a card can be answered once")
        self.assertTrue(self.events(handle)[1]["terminal"])

    def test_15_cancel_is_safe_to_repeat(self):
        handle = self.start(text="approve it")[1]["handle"]
        first = self.call("POST", f"/coding/{handle}/cancel", self.USER_A, {})
        second = self.call("POST", f"/coding/{handle}/cancel", self.USER_A, {})
        self.assertEqual((first[0], first[1]["state"]), (200, "CANCELLED"))
        self.assertEqual((second[0], second[1]["state"]), (200, "CANCELLED"))

    # ---------------------------------------------------------------- routing hygiene
    def test_16_unknown_routes_and_methods_do_not_reach_the_service(self):
        before = self.wire.calls
        for method, path in (("GET", "/coding"), ("DELETE", "/coding/start"), ("GET", "/coding/a/b/c"), ("POST", "/coding/availability")):
            status, _ = self.call(method, path, self.USER_A)
            self.assertIn(status, (404, 405), f"{method} {path}")
        self.assertEqual(self.wire.calls, before)

    def test_17_an_event_beyond_the_protocol_limit_is_refused_as_unavailable_not_forwarded(self):
        handle = self.start(text="oversize please")[1]["handle"]
        status, body = self.events(handle)
        self.assertEqual((status, body["error"]), (503, "unavailable"))
