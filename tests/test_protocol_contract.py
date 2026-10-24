"""Phase 1/2 gates, contract level: the fixtures, the validators and the dependency boundary of the public packages."""
import ast
import unittest
from pathlib import Path

from protocol import ERROR_STATUS, EVENT_TYPES, PROTOCOL_VERSION, ProtocolError, check_version, error_envelope, validate_event, validate_error
from testkit.fixtures import check_case, load_cases

ROOT = Path(__file__).resolve().parents[1]

class FixtureTests(unittest.TestCase):
    def test_every_fixture_behaves_as_declared(self):
        problems = [p for p in (check_case(c) for c in load_cases()) if p]
        self.assertEqual(problems, [])

    def test_fixtures_cover_every_event_type_and_a_refusal_for_each_kind(self):
        events = {c["body"]["type"] for c in load_cases("event") if c["valid"]}
        self.assertEqual(events, set(EVENT_TYPES))
        for kind in ("task_request", "decision", "event", "events_response", "capabilities", "error"):
            cases = load_cases(kind)
            self.assertTrue([c for c in cases if c["valid"]] and [c for c in cases if not c["valid"]], kind)

    def test_every_invalid_fixture_names_its_expected_error_code(self):
        for case in load_cases():
            if not case["valid"]:
                self.assertIn(case.get("expect_code"), ERROR_STATUS, case["name"])

class VersioningTests(unittest.TestCase):
    def test_major_mismatch_fails_closed_and_minor_is_tolerated(self):
        check_version(None)
        check_version("1.0")
        check_version("1.9")
        for bad in ("2.0", "0.9", "10.0"):
            with self.assertRaises(ProtocolError) as caught:
                check_version(bad)
            self.assertEqual(caught.exception.code, "VERSION_MISMATCH")
        with self.assertRaises(ProtocolError) as caught:
            check_version("one")
        self.assertEqual(caught.exception.code, "BAD_REQUEST")

    def test_error_envelopes_validate_and_codes_map_to_statuses(self):
        for code, status in ERROR_STATUS.items():
            body = error_envelope(code, "m", "f")
            self.assertEqual(validate_error(body), body)
            self.assertEqual(body["protocol"], PROTOCOL_VERSION)
            self.assertIn(status, range(400, 600))

    def test_events_from_a_future_minor_version_still_validate(self):
        event = {"protocol": "1.4", "task_id": "t", "event_id": "1", "type": "state", "data": {"state": "RUNNING", "extra_future_field": 1}}
        self.assertEqual(validate_event(event), event)

class BoundaryTests(unittest.TestCase):
    """The public packages must stay powerless: no agent, tool, file, Git, process or model dependencies."""
    STDLIB_OK = {"json", "re", "threading", "hashlib", "uuid", "urllib", "pathlib", "math", "__future__"}

    def imports(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    yield a.name.split(".")[0], 0
            elif isinstance(node, ast.ImportFrom):
                yield (node.module or "").split(".")[0], node.level

    def test_protocol_imports_only_stdlib_and_itself(self):
        for path in (ROOT / "protocol").glob("*.py"):
            for top, level in self.imports(path):
                with self.subTest(file=path.name, top=top):
                    self.assertTrue(level == 1 or top in self.STDLIB_OK or top == "protocol", f"{path.name} imports {top}")

    def test_testkit_depends_only_on_protocol_and_stdlib(self):
        forbidden = {"code_attach", "coding_agent", "fastapi", "starlette", "pydantic", "subprocess", "os", "shutil", "socket", "http", "httpx", "requests", "openai", "anthropic"}
        for path in (ROOT / "testkit").rglob("*.py"):
            for top, level in self.imports(path):
                with self.subTest(file=str(path.relative_to(ROOT)), top=top):
                    self.assertNotIn(top, forbidden)

    def test_mock_shares_no_code_with_the_real_service(self):
        source = (ROOT / "testkit" / "mock" / "service.py").read_text(encoding="utf-8")
        for name in ("ToolBox", "Engine", "Workspace", "Workflow"):
            self.assertNotIn(name, source.replace("MockCodingService", ""), name)

if __name__ == "__main__":
    unittest.main()
