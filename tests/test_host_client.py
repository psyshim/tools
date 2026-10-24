"""Phase 3: the host client against the executable mock, with injected faults, over real sockets, and in isolation."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "host_client"))

from host_client import AgentGateway, AgentUnavailable, ProtocolRefused, Unavailable
from host_client.events import SafeDisplayEvent, clean_data
from hostkit_build.build_host_kit import build
from testkit.mock import MockCodingService

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://mock.invalid"
CREDENTIAL = "svc-credential"

def mock_service():
    return MockCodingService({}, {"host:alice": {"w1"}, "host:bob": {"w2"}}, host_credentials={CREDENTIAL})

class Direct:
    """In-memory transport: calls the mock service directly. Subclass or wrap to inject faults."""
    def __init__(self, service):
        self.service, self.calls = service, []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        status, payload = self.service.handle(method, url[len(BASE):], headers, json.loads(body) if body else None)
        return status, json.dumps(payload).encode()

def gateway(transport, **kw):
    return AgentGateway(BASE, CREDENTIAL, transport=transport, sleep=lambda s: None, **kw)

def drain(gen):
    return list(gen)

def until_card(gw, principal, handle):
    """Events up to and including the first approval card. A task that waits for a decision never becomes terminal,
    so the observer must not be drained to the end."""
    seen = []
    for event in gw.observe_coding(principal, handle, poll_interval=0, max_seconds=5):
        seen.append(event)
        if event.type == "approval_required":
            return seen
    raise AssertionError("no approval card appeared")

class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.service = mock_service()
        self.t = Direct(self.service)
        self.gw = gateway(self.t)

    def start(self, text="propose a change", message="m1", principal="alice"):
        return self.gw.start_coding(principal, "w1", text, conversation_id="c1", message_id=message)

    def test_availability_reports_capabilities_without_raising(self):
        got = self.gw.availability()
        self.assertEqual((got.ok, got.modes, got.mutation), (True, ("read", "propose"), False))

    def test_propose_flow_streams_safe_events_to_a_terminal_state(self):
        handle = self.start()
        events = drain(self.gw.observe_coding("alice", handle, poll_interval=0))
        self.assertEqual([e.type for e in events], ["assistant_message", "proposal_completed", "state"])
        self.assertTrue(events[-1].terminal and not events[0].terminal)
        self.assertEqual([e.event_id for e in events], [1, 2, 3])
        self.assertIs(events[1].data["applied"], False)
        self.assertIn("nothing was changed", events[1].text)

    def test_the_same_message_is_the_same_task_even_after_a_lost_response(self):
        lost = {"armed": True}
        def lose_first_start_response(method, url, headers, body, timeout):
            result = self.t(method, url, headers, body, timeout)
            if method == "POST" and url.endswith("/tasks") and lost["armed"]:
                lost["armed"] = False
                raise ConnectionResetError("response lost after the service accepted the task")
            return result
        gw = gateway(lose_first_start_response)
        handle = gw.start_coding("alice", "w1", "propose x", conversation_id="c", message_id="m")
        again = gw.start_coding("alice", "w1", "propose x", conversation_id="c", message_id="m")
        self.assertEqual(handle, again)
        self.assertEqual(len(self.service._tasks), 1, "retrying must not create a second task")
        other = gw.start_coding("alice", "w1", "propose x", conversation_id="c", message_id="m2")
        self.assertNotEqual(handle, other)

    def test_duplicate_delivery_is_rendered_once(self):
        handle = self.start()
        def replaying(method, url, headers, body, timeout):
            if "/events" in url:
                url = url.split("?")[0] + "?after=0"   # the service redelivers from the start however far the client has read
            return self.t(method, url, headers, body, timeout)
        events = drain(gateway(replaying).observe_coding("alice", handle, resume_token=1, poll_interval=0))
        self.assertEqual([e.event_id for e in events], [2, 3], "redelivered events are rendered once and never repeated")
        events = drain(gateway(replaying).observe_coding("alice", handle, poll_interval=0))
        self.assertEqual([e.event_id for e in events], [1, 2, 3])

    def test_a_missing_event_produces_a_visible_gap_marker(self):
        handle = self.start()
        def dropping(method, url, headers, body, timeout):
            status, raw = self.t(method, url, headers, body, timeout)
            if "/events" in url:
                payload = json.loads(raw)
                payload["events"] = [e for e in payload["events"] if e["event_id"] != "2"]
                raw = json.dumps(payload).encode()
            return status, raw
        events = drain(gateway(dropping).observe_coding("alice", handle, poll_interval=0))
        self.assertEqual([(e.type, e.event_id) for e in events], [("assistant_message", 1), ("gap", 0), ("state", 3)])
        self.assertTrue(events[1].synthetic)
        self.assertEqual(events[1].data["missing_from"], "2")

    def test_resume_continues_after_the_last_rendered_event(self):
        handle = self.start()
        first = []
        for event in self.gw.observe_coding("alice", handle, poll_interval=0):
            first.append(event)
            if len(first) == 1:
                break
        rest = drain(self.gw.observe_coding("alice", handle, resume_token=first[-1].event_id, poll_interval=0))
        self.assertEqual([e.event_id for e in first + rest], [1, 2, 3])

    def test_expired_cursor_restarts_from_what_the_service_still_has(self):
        handle = self.start()
        self.service.fail_next = ["CURSOR_EXPIRED"]
        events = drain(self.gw.observe_coding("alice", handle, resume_token=1, poll_interval=0))
        self.assertEqual([e.event_id for e in events], [2, 3], "only events not yet rendered, without repeats")

    def test_revocation_stops_delivery_immediately(self):
        handle = self.start()
        allowed = iter([True, False])
        events = drain(self.gw.observe_coding("alice", handle, poll_interval=0, should_continue=lambda: next(allowed, False)))
        self.assertLessEqual(len(events), 3)
        calls_before = len(self.t.calls)
        drain(self.gw.observe_coding("alice", handle, poll_interval=0, should_continue=lambda: False))
        self.assertEqual(len(self.t.calls), calls_before, "a revoked session makes no further request")

    def test_other_user_cannot_observe_cancel_or_decide(self):
        handle = self.start()
        for call in (lambda: drain(self.gw.observe_coding("bob", handle, poll_interval=0)), lambda: self.gw.cancel_coding("bob", handle),
                     lambda: self.gw.submit_decision("bob", handle, "a", "f", True)):
            with self.assertRaises(ProtocolRefused) as caught:
                call()
            self.assertEqual(caught.exception.code, "NOT_FOUND")
        with self.assertRaises(ProtocolRefused) as caught:
            self.gw.start_coding("bob", "w1", "propose x", conversation_id="c", message_id="mx")
        self.assertEqual(caught.exception.code, "NOT_FOUND")

    def test_cancel_reports_the_services_state_and_never_invents_one(self):
        done = self.start()
        drain(self.gw.observe_coding("alice", done, poll_interval=0))
        result = self.gw.cancel_coding("alice", done)
        self.assertEqual((result.state, result.terminal), ("COMPLETED", True), "a finished task stays finished")
        waiting = self.start("approve this", message="m2")
        result = self.gw.cancel_coding("alice", waiting)
        self.assertEqual((result.state, result.terminal), ("CANCELLED", True))

    def test_decision_flow_and_an_ambiguous_retry_is_resolved_from_the_log(self):
        handle = self.start("approve this")
        card = until_card(self.gw, "alice", handle)[-1].data
        lost = {"armed": True}
        def lose_decision_response(method, url, headers, body, timeout):
            result = self.t(method, url, headers, body, timeout)
            if url.endswith("/decision") and lost["armed"]:
                lost["armed"] = False
                raise TimeoutError("timed out after the service processed it")
            return result
        gw = gateway(lose_decision_response)
        outcome = gw.submit_decision("alice", handle, card["action_id"], card["fingerprint"], False)
        self.assertEqual((outcome.accepted, outcome.already_decided), (True, True))
        final = drain(gw.observe_coding("alice", handle, poll_interval=0))
        self.assertEqual([e.type for e in final].count("approval_decision"), 1, "the decision was applied exactly once")

    def test_wrong_fingerprint_is_not_accepted(self):
        handle = self.start("approve this")
        card = until_card(self.gw, "alice", handle)[-1].data
        outcome = self.gw.submit_decision("alice", handle, card["action_id"], "wrong", True)
        self.assertEqual((outcome.accepted, outcome.already_decided), (False, False))

class Failure(unittest.TestCase):
    def test_service_absent_never_raises_from_availability_and_start_is_typed(self):
        def down(*a):
            raise ConnectionRefusedError("nothing is listening")
        gw = gateway(down, retries=2)
        self.assertEqual(gw.availability(), Unavailable("unreachable"))
        with self.assertRaises(AgentUnavailable) as caught:
            gw.start_coding("alice", "w1", "x", conversation_id="c", message_id="m")
        self.assertEqual(caught.exception.reason, "unreachable")
        with self.assertRaises(AgentUnavailable):
            drain(gw.observe_coding("alice", gateway_handle(), poll_interval=0))

    def test_transient_gateway_errors_are_retried_with_backoff_then_surface(self):
        service, sleeps, attempts = mock_service(), [], {"n": 0}
        direct = Direct(service)
        def flaky(method, url, headers, body, timeout):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                return 503, json.dumps({"protocol": "1.0", "error": {"code": "UNAVAILABLE", "message": "restarting"}}).encode()
            return direct(method, url, headers, body, timeout)
        gw = AgentGateway(BASE, CREDENTIAL, transport=flaky, sleep=sleeps.append, backoff=0.1)
        self.assertTrue(gw.availability().ok)
        self.assertEqual(sleeps, [0.1, 0.2])
        always = AgentGateway(BASE, CREDENTIAL, transport=lambda *a: (503, b"<html>bad gateway</html>"), sleep=lambda s: None, retries=2)
        self.assertEqual(always.availability(), Unavailable("bad_gateway"))

    def test_other_major_version_means_unavailable_never_a_fallback(self):
        service = mock_service()
        def v2(method, url, headers, body, timeout):
            return 426, json.dumps({"protocol": "2.0", "error": {"code": "VERSION_MISMATCH", "message": "x"}}).encode()
        self.assertEqual(gateway(v2).availability(), Unavailable("protocol_mismatch"))
        def v2_body(method, url, headers, body, timeout):
            status, raw = Direct(service)(method, url, headers, body, timeout)
            return status, raw.replace(b'"protocol": "1.0"', b'"protocol": "2.0"')
        self.assertEqual(gateway(v2_body).availability(), Unavailable("protocol_mismatch"))

    def test_a_service_that_violates_the_contract_fails_closed(self):
        service = mock_service()
        direct = Direct(service)
        gw0 = gateway(direct)
        handle = gw0.start_coding("alice", "w1", "propose x", conversation_id="c", message_id="m")
        def wrong_terminal(method, url, headers, body, timeout):
            status, raw = direct(method, url, headers, body, timeout)
            if "/events" in url:
                payload = json.loads(raw)
                payload["terminal"] = not payload["terminal"]
                raw = json.dumps(payload).encode()
            return status, raw
        with self.assertRaises(AgentUnavailable) as caught:
            drain(gateway(wrong_terminal).observe_coding("alice", handle, poll_interval=0))
        self.assertEqual(caught.exception.reason, "contract_violation")
        for garbage in (b"not json", b"[]", b"", b"null", b"\xff\xfe"):
            self.assertEqual(gateway(lambda *a, g=garbage: (200, g)).availability(), Unavailable("malformed_response"))

    def test_rejected_service_credential_is_a_configuration_problem_not_a_user_problem(self):
        gw = AgentGateway(BASE, "wrong-credential", transport=Direct(mock_service()), sleep=lambda s: None)
        self.assertEqual(gw.availability(), Unavailable("credential_rejected"))

    def test_observation_times_out_with_a_resumable_position(self):
        service = mock_service()
        gw = gateway(Direct(service))
        handle = gw.start_coding("alice", "w1", "approve this", conversation_id="c", message_id="m")
        with self.assertRaises(AgentUnavailable) as caught:
            drain(gw.observe_coding("alice", handle, poll_interval=0, max_seconds=0.05))
        self.assertEqual(caught.exception.reason, "observe_timeout")
        self.assertIn("resume from event", caught.exception.detail)

    def test_oversized_responses_are_refused(self):
        big = gateway(lambda *a: (200, b"x" * (4 * 1024 * 1024 + 2)))
        self.assertEqual(big.availability(), Unavailable("response_too_large"))

def gateway_handle():
    from host_client import TaskHandle
    return TaskHandle("t123")

class InputHygiene(unittest.TestCase):
    def test_local_validation_blocks_injection_before_any_request(self):
        t = Direct(mock_service())
        gw = gateway(t)
        for bad in ("", "a\nb", "a\x00b", "x" * 300, None, 5):
            with self.subTest(principal=repr(bad)[:20]):
                with self.assertRaises(ValueError):
                    gw.start_coding(bad, "w1", "x", conversation_id="c", message_id="m")
        for kwargs in ({"workspace_id": ""}, {"text": ""}, {"conversation_id": ""}, {"message_id": None}):
            args = {"workspace_id": "w1", "text": "x", "conversation_id": "c", "message_id": "m", **kwargs}
            with self.assertRaises(ValueError):
                gw.start_coding("alice", args["workspace_id"], args["text"], conversation_id=args["conversation_id"], message_id=args["message_id"])
        from host_client import TaskHandle
        for bad in ("../x", "a/b", "a b", "", "a" * 200, "%00"):
            with self.assertRaises(ValueError):
                TaskHandle(bad)
        for bad in ("abc", "-1", 1.5, []):
            with self.assertRaises(ValueError):
                drain(gw.observe_coding("alice", gateway_handle(), resume_token=bad))
        self.assertEqual(t.calls, [], "nothing hostile reached the network")

    def test_unicode_principals_survive_the_header_round_trip(self):
        service = MockCodingService({}, {"host:山田 太郎": {"w1"}}, host_credentials={CREDENTIAL})
        gw = gateway(Direct(service))
        handle = gw.start_coding("山田 太郎", "w1", "propose x", conversation_id="c", message_id="m")
        self.assertEqual(drain(gw.observe_coding("山田 太郎", handle, poll_interval=0))[-1].type, "state")

    def test_untrusted_event_content_is_cleaned_bounded_and_escapable(self):
        raw = {"text": "<script>alert(1)</script>\x00\x1b[31mred\x07" + "x" * 50000, "nested": {"deep": [[[[[[[["bottom"]]]]]]]]}, "big": list(range(1000)), "lone": "\ud800"}
        cleaned = clean_data(raw)
        self.assertNotIn("\x00", cleaned["text"])
        self.assertNotIn("\x1b", cleaned["text"])
        self.assertLessEqual(len(cleaned["text"]), 20020)
        self.assertEqual(len(cleaned["big"]), 100)
        self.assertEqual(cleaned["lone"], "")
        event = SafeDisplayEvent("t", 1, "assistant_message", cleaned)
        self.assertNotIn("<script>", event.html_text)
        self.assertIn("&lt;script&gt;", event.html_text)

class OverSockets(unittest.TestCase):
    """Same behaviour through the default urllib transport against the mock served over real HTTP."""
    def test_full_flow_over_http(self):
        from examples.mock_server import serve
        server = serve(mock_service(), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        url = f"http://127.0.0.1:{server.server_address[1]}"
        gw = AgentGateway(url, CREDENTIAL, timeout=5)
        self.assertTrue(gw.availability().ok)
        handle = gw.start_coding("alice", "w1", "propose a change", conversation_id="c", message_id="m")
        self.assertEqual([e.type for e in gw.observe_coding("alice", handle, poll_interval=0.01)], ["assistant_message", "proposal_completed", "state"])
        with self.assertRaises(ProtocolRefused):
            gw.cancel_coding("bob", handle)
        server.shutdown()
        server.server_close()
        self.assertEqual(AgentGateway(url, CREDENTIAL, timeout=1, retries=1).availability().ok, False)

class Kit(unittest.TestCase):
    def test_build_is_reproducible_and_verifiable(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first, second = build(a), build(b)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                names = set(archive.namelist())
                self.assertTrue(any(n.startswith("host_client/") for n in names) and any(n.startswith("protocol/") for n in names))
                allowed = ("host_client/", "protocol/", "code_host_kit-")
                self.assertTrue(all(n.startswith(allowed) for n in names), names)
                manifest = json.loads(archive.read(next(n for n in names if n.endswith("KIT_MANIFEST.json"))))
                self.assertEqual(manifest["name"], "code-host-kit")
                import hashlib
                for name, digest in manifest["files"].items():
                    self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), digest)
            self.assertIn(first.name, (Path(a) / "SHA256SUMS").read_text())

    def test_installed_kit_works_with_no_agent_or_web_framework_present(self):
        """Install the wheel into an empty directory, run Python in isolated mode WITHOUT site-packages (so FastAPI, pydantic,
        the agent and the tools are all unreachable), talk to a real HTTP mock, and assert what got imported."""
        from examples.mock_server import serve
        server = serve(mock_service(), port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as target:
            wheel = build(out)
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(target)
            script = (
                "import sys, json\n"
                "from host_client import AgentGateway\n"
                "gw = AgentGateway(sys.argv[1], 'svc-credential', timeout=5)\n"
                "assert gw.availability().ok\n"
                "h = gw.start_coding('alice', 'w1', 'propose x', conversation_id='c', message_id='m')\n"
                "events = [e.type for e in gw.observe_coding('alice', h, poll_interval=0.01)]\n"
                "bad = sorted(m for m in sys.modules if m.split('.')[0] in {'code_attach','coding_agent','device_agent','coding_service','fastapi','starlette','pydantic','uvicorn','subprocess','testkit','tests','examples'})\n"
                "print(json.dumps({'events': events, 'forbidden_loaded': bad, 'path': [p for p in sys.path if 'site-packages' in p]}))\n")
            env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "PATH", "TEMP", "TMP"}}
            env["PYTHONPATH"] = target
            result = subprocess.run([sys.executable, "-S", "-c", script, f"http://127.0.0.1:{server.server_address[1]}"], capture_output=True, text=True, env={**env, "PYTHONPATH": target},
                                    cwd=target, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr[-800:])
            report = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(report["events"], ["assistant_message", "proposal_completed", "state"])
            self.assertEqual(report["forbidden_loaded"], [])
            self.assertEqual(report["path"], [], "site-packages must not be on the path in this isolated run")

    def test_host_client_sources_import_nothing_powerful(self):
        import ast
        forbidden = {"code_attach", "coding_agent", "device_agent", "coding_service", "fastapi", "starlette", "pydantic", "subprocess", "shutil", "os", "socket", "ctypes", "multiprocessing", "openai", "anthropic", "httpx", "requests"}
        for path in (ROOT / "packages" / "host_client" / "host_client").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
                for name in names:
                    with self.subTest(file=path.name, imports=name):
                        self.assertNotIn(name.split(".")[0], forbidden)

if __name__ == "__main__":
    unittest.main()
