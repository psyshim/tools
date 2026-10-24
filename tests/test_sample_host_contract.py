"""The wrapper test suite shipped to the host (host_client.testing.WrapperContract), run against the sample host. This file is also
the TEMPLATE for the host's own test: copy the harness class, point `make_host` at the real app, set the five constants.

It is also tested the other way round: deliberately broken wrappers must FAIL the suite, each on the rule it breaks."""
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "host_client"))

from fastapi.testclient import TestClient

from examples.sample_host.app import SampleHost, create_app
from host_client.testing import WrapperContract


class SampleHarness:
    """What the host writes: call the app as a given user and return (status, json)."""

    def __init__(self, app):
        self.client = TestClient(app, raise_server_exceptions=False)

    def request(self, method, path, user, body=None, query=None):
        headers = {"authorization": f"Bearer token-{user}"} if user else {}
        response = self.client.request(method, path, headers=headers, json=body if method == "POST" else None, params=query)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return response.status_code, payload


class SampleHostWrapperTests(WrapperContract, unittest.TestCase):
    USER_A, USER_B = "alice", "bob"
    WORKSPACE_A, WORKSPACE_B = "w1", "w2"
    CHAT = ("POST", "/chat", {"message": "hi"})

    def make_host(self, gateway):
        return SampleHarness(create_app(gateway))


# --------------------------------------------------------------------------------------- the suite must catch broken wrappers
class OpenAcl(SampleHost):
    def can_access_workspace(self, user, workspace_id):
        return True

class Broken:
    """Each variant is a plausible mistake. `expect` names the tests that must fail because of it."""

    class AclAllowsEverything(WrapperContract, unittest.TestCase):
        USER_A, USER_B, WORKSPACE_A, WORKSPACE_B = "alice", "bob", "w1", "w2"
        expect = {"test_06_a_workspace_the_user_may_not_use_is_refused_locally"}
        def make_host(self, gateway):
            return SampleHarness(create_app(gateway, OpenAcl()))

    class EveryoneIsAlice(WrapperContract, unittest.TestCase):
        USER_A, USER_B, WORKSPACE_A, WORKSPACE_B = "alice", "bob", "w1", "w2"
        expect = {"test_09_another_user_cannot_read_cancel_or_decide"}
        def make_host(self, gateway):
            harness = SampleHarness(create_app(gateway))
            original = harness.request
            harness.request = lambda method, path, user, body=None, query=None: original(method, path, "alice" if user else None, body, query)
            return harness

    class AnonymousIsLetIn(WrapperContract, unittest.TestCase):
        USER_A, USER_B, WORKSPACE_A, WORKSPACE_B = "alice", "bob", "w1", "w2"
        expect = {"test_01_not_logged_in_is_refused_everywhere"}
        def make_host(self, gateway):
            harness = SampleHarness(create_app(gateway))
            original = harness.request
            harness.request = lambda method, path, user, body=None, query=None: original(method, path, user or "alice", body, query)
            return harness

    class RendersRawEventText(WrapperContract, unittest.TestCase):
        USER_A, USER_B, WORKSPACE_A, WORKSPACE_B = "alice", "bob", "w1", "w2"
        expect = {"test_13_hostile_event_text_is_escaped_and_bounded"}
        def make_host(self, gateway):
            harness = SampleHarness(create_app(gateway))
            original = harness.request
            def request(method, path, user, body=None, query=None):
                status, payload = original(method, path, user, body, query)
                for event in (payload or {}).get("events", []) if isinstance(payload, dict) else []:
                    if isinstance(event.get("data"), dict) and "text" in event["data"]:
                        event["html"] = event["data"]["text"]          # the classic mistake: trusting the data field
                return status, payload
            harness.request = request
            return harness

    class CrashesWhenTheServiceIsDown(WrapperContract, unittest.TestCase):
        USER_A, USER_B, WORKSPACE_A, WORKSPACE_B = "alice", "bob", "w1", "w2"
        CHAT = ("POST", "/chat", {"message": "hi"})
        expect = {"test_03_service_down_is_a_typed_503_and_ordinary_chat_still_works", "test_04_a_rejected_service_credential_or_garbage_answer_is_unavailable_not_a_crash"}
        def make_host(self, gateway):
            harness = SampleHarness(create_app(gateway))
            original = harness.request
            def request(method, path, user, body=None, query=None):
                status, payload = original(method, path, user, body, query)
                if isinstance(payload, dict) and payload.get("error") == "unavailable":
                    return 500, {"detail": "Internal Server Error"}        # an unhandled exception in the host's handler
                return status, payload
            harness.request = request
            return harness


class WrapperSuiteShipsInTheKit(unittest.TestCase):
    """The host gets the suite from the wheel alone: no repository, no testkit, no framework, no site-packages."""
    def test_the_installed_kit_contains_a_working_suite_and_fake_service(self):
        from hostkit_build.build_host_kit import build
        with tempfile.TemporaryDirectory() as tmp:
            wheel = build(Path(tmp) / "out")
            target = Path(tmp) / "site"
            zipfile.ZipFile(wheel).extractall(target)
            probe = """
import sys, unittest
from host_client.testing import WrapperContract, MockCodingService
assert MockCodingService.__module__.startswith("host_client.fake_service"), MockCodingService.__module__

class H:
    def __init__(self, gw):
        self.gw = gw
    def request(self, method, path, user, body=None, query=None):
        if path.endswith("availability"):
            return 200, {"available": True, "modes": [], "mutation": False}
        return 404, {}

class T(WrapperContract, unittest.TestCase):
    USER_A, USER_B, WORKSPACE_A, WORKSPACE_B = "a", "b", "wa", "wb"
    def make_host(self, gw):
        return H(gw)

r = unittest.TestResult()
unittest.defaultTestLoader.loadTestsFromName("test_02_availability_shape", T).run(r)
assert r.testsRun == 1 and not r.failures and not r.errors, (r.failures, r.errors)
bad = sorted(m for m in sys.modules if m.split(".")[0] in {"testkit", "fastapi", "starlette", "pydantic", "code_attach", "coding_agent", "subprocess"})
assert not bad, bad
print("kit-suite-ok")
"""
            done = subprocess.run([sys.executable, "-S", "-c", probe], cwd=target, env={"PYTHONPATH": str(target), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")},
                                  capture_output=True, text=True, timeout=120)
            self.assertIn("kit-suite-ok", done.stdout, done.stderr[-800:])


class SuiteCatchesBrokenWrappers(unittest.TestCase):
    def test_each_broken_wrapper_fails_exactly_the_rule_it_breaks(self):
        for name in [n for n in vars(Broken) if not n.startswith("_")]:
            cls = getattr(Broken, name)
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromTestCase(cls).run(result)
            failed = {t.id().rsplit(".", 1)[1] for t, _ in result.failures + result.errors}
            with self.subTest(wrapper=name):
                self.assertTrue(cls.expect <= failed, f"the suite did not notice {name}: failed={sorted(failed)}")

    def test_the_unbroken_sample_passes_everything(self):
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(SampleHostWrapperTests).run(result)
        self.assertEqual(([str(f[0]) for f in result.failures], [str(e[0]) for e in result.errors]), ([], []))
        self.assertGreaterEqual(result.testsRun, 16)


if __name__ == "__main__":
    unittest.main()
