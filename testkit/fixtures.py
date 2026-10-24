"""Loading and checking the shared contract fixtures (protocol/fixtures/contract.json)."""
import json
from pathlib import Path

from protocol import schema

FIXTURE_FILE = Path(__file__).resolve().parents[1] / "protocol" / "fixtures" / "contract.json"

VALIDATORS = {
    "task_request": schema.validate_task_request,
    "decision": schema.validate_decision,
    "event": schema.validate_event,
    "events_response": schema.validate_events_response,
    "capabilities": schema.validate_capabilities,
    "error": schema.validate_error,
}

def expand(value):
    """Expand {"$repeat": [text, n]} markers so large payloads need not be stored in the fixture file."""
    if isinstance(value, dict):
        if set(value) == {"$repeat"}:
            text, count = value["$repeat"]
            return text * count
        return {k: expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v) for v in value]
    return value

def load_cases(kind=None):
    cases = json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))["cases"]
    return [{**c, "body": expand(c["body"])} for c in cases if kind is None or c["kind"] == kind]

def check_case(case):
    """Run one fixture through its validator. Returns None when it behaves as declared, else a description of the mismatch."""
    validator = VALIDATORS[case["kind"]]
    try:
        validator(case["body"])
    except schema.ProtocolError as exc:
        if case["valid"]:
            return f"{case['name']}: valid fixture refused ({exc.code}: {exc.message})"
        if exc.code != case.get("expect_code"):
            return f"{case['name']}: refused with {exc.code}, expected {case.get('expect_code')}"
        if case.get("expect_field") and exc.field != case["expect_field"]:
            return f"{case['name']}: refused on field {exc.field!r}, expected {case['expect_field']!r}"
        return None
    return None if case["valid"] else f"{case['name']}: invalid fixture was accepted"
