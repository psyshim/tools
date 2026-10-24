"""Versioned transport contract between a host client and a coding service. Stdlib only, no I/O.

This package is the *public* surface: it must never import the task loop, tools, file or Git code (enforced by
tests/test_protocol_boundaries.py). Both the executable mock (testkit/mock) and the real service are validated against the
same fixtures (protocol/fixtures/*.json); a major-version mismatch fails closed.

Two outcomes are deliberately distinct and never conflated:
  proposal_completed  a patch artifact exists; NOTHING on disk changed
  mutation_applied    a receipt that a change really happened on disk
"""
import json

PROTOCOL_VERSION = "1.0"
MAJOR = 1

TASK_STATES = ("ACCEPTED", "RUNNING", "AWAITING_APPROVAL", "COMPLETED", "FAILED", "CANCELLED", "NEEDS_RECONCILIATION")
TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "NEEDS_RECONCILIATION"})
EVENT_TYPES = ("assistant_message", "tool_activity", "proposal_completed", "approval_required", "approval_decision",
               "mutation_applied", "test_result", "notice", "state", "gap")
MODES = ("read", "propose")          # protocol 1.0 advertises nothing else; an "execute" mode needs a new capability + version
MAX_EVENT_DATA_BYTES = 65536
MAX_INSTRUCTION = 16384

# code -> default HTTP status. Stable strings: clients branch on `code`, never on `message`.
ERROR_STATUS = {
    "BAD_REQUEST": 400, "UNAUTHENTICATED": 401, "FORBIDDEN": 403, "NOT_FOUND": 404, "IDEMPOTENCY_CONFLICT": 409,
    "NO_PENDING_ACTION": 409, "PAYLOAD_TOO_LARGE": 413, "VALIDATION_FAILED": 422, "VERSION_MISMATCH": 426,
    "UNSUPPORTED_MODE": 422, "RATE_LIMITED": 429, "UNAVAILABLE": 503, "LOG_UNAVAILABLE": 503, "CURSOR_EXPIRED": 410,
}
ERROR_CODES = tuple(ERROR_STATUS)

class ProtocolError(Exception):
    """A violation of the contract. `code` is one of ERROR_CODES."""
    def __init__(self, code, message="", field=None):
        super().__init__(message or code)
        assert code in ERROR_STATUS, code
        self.code, self.message, self.field = code, message or code, field

    @property
    def status(self):
        return ERROR_STATUS[self.code]

    def envelope(self):
        return error_envelope(self.code, self.message, self.field)

def error_envelope(code, message="", field=None):
    err = {"code": code, "message": message or code}
    if field:
        err["field"] = field
    return {"protocol": PROTOCOL_VERSION, "error": err}

def parse_version(text):
    try:
        major, _, minor = str(text).partition(".")
        return int(major), int(minor or 0)
    except ValueError:
        raise ProtocolError("BAD_REQUEST", "malformed protocol version", "protocol") from None

def check_version(requested):
    """A client that names a different major version is refused (fail closed); naming none means 'current'."""
    if requested in (None, ""):
        return
    if parse_version(requested)[0] != MAJOR:
        raise ProtocolError("VERSION_MISMATCH", f"this service speaks protocol {PROTOCOL_VERSION}; client asked for {requested}", "protocol")

def parse_cursor(raw):
    """The `after` query value: a non-negative decimal integer of at most 18 digits. Both mock and real service use this."""
    raw = "0" if raw in (None, "") else raw
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit() or len(raw) > 18:
        raise ProtocolError("BAD_REQUEST", "after must be a non-negative integer", "after")
    return int(raw)

def _require(obj, field, types, minimum=None, maximum=None):
    if field not in obj:
        raise ProtocolError("VALIDATION_FAILED", f"{field} is required", field)
    value = obj[field]
    if isinstance(value, bool) and bool not in types:
        raise ProtocolError("VALIDATION_FAILED", f"{field} has the wrong type", field)
    if not isinstance(value, types):
        raise ProtocolError("VALIDATION_FAILED", f"{field} has the wrong type", field)
    if isinstance(value, str) and (len(value) < (minimum or 0) or (maximum is not None and len(value) > maximum)):
        raise ProtocolError("VALIDATION_FAILED", f"{field} must be {minimum or 0} to {maximum} characters", field)
    return value

def validate_task_request(obj):
    """Return the normalized request or raise ProtocolError. Unknown fields are refused: a typo must not be silently ignored."""
    if not isinstance(obj, dict):
        raise ProtocolError("BAD_REQUEST", "body must be a JSON object")
    allowed = {"protocol", "workspace_id", "instruction", "idempotency_key", "mode"}
    extra = sorted(set(obj) - allowed)
    if extra:
        raise ProtocolError("VALIDATION_FAILED", f"unknown field {extra[0]!r}", extra[0])
    check_version(obj.get("protocol"))
    out = {"workspace_id": _require(obj, "workspace_id", (str,), 1, 128), "instruction": _require(obj, "instruction", (str,), 1, MAX_INSTRUCTION),
           "idempotency_key": _require(obj, "idempotency_key", (str,), 1, 128), "mode": obj.get("mode", "propose")}
    if out["mode"] not in MODES:
        raise ProtocolError("UNSUPPORTED_MODE", f"mode must be one of {', '.join(MODES)}", "mode")
    return out

def validate_decision(obj):
    if not isinstance(obj, dict):
        raise ProtocolError("BAD_REQUEST", "body must be a JSON object")
    extra = sorted(set(obj) - {"protocol", "action_id", "fingerprint", "approve"})
    if extra:
        raise ProtocolError("VALIDATION_FAILED", f"unknown field {extra[0]!r}", extra[0])
    check_version(obj.get("protocol"))
    return {"action_id": _require(obj, "action_id", (str,), 1, 256), "fingerprint": _require(obj, "fingerprint", (str,), 1, 256), "approve": _require(obj, "approve", (bool,))}

_PER_TYPE = {
    "assistant_message": (("text", str),),
    "tool_activity": (("tool", str), ("status", str)),
    "proposal_completed": (("proposal_id", str), ("files", list), ("applied", bool)),
    "approval_required": (("action_id", str), ("fingerprint", str), ("tool", str), ("summary", dict)),
    "approval_decision": (("action_id", str), ("approve", bool)),
    "mutation_applied": (("receipt", dict),),
    "test_result": (("status", str),),
    "notice": (("code", str),),
    "state": (("state", str),),
    "gap": (("missing_from", str),),
}

def validate_event(obj):
    """Check one event envelope. `applied` on proposal_completed MUST be false: a proposal never claims a change."""
    if not isinstance(obj, dict):
        raise ProtocolError("VALIDATION_FAILED", "event must be an object")
    if obj.get("protocol") != PROTOCOL_VERSION and parse_version(obj.get("protocol", "0"))[0] != MAJOR:
        raise ProtocolError("VERSION_MISMATCH", "event carries another major version", "protocol")
    _require(obj, "task_id", (str,), 1, 128)
    event_id = _require(obj, "event_id", (str,), 1, 20)
    if not event_id.isdigit():
        raise ProtocolError("VALIDATION_FAILED", "event_id must be a decimal string", "event_id")
    kind = _require(obj, "type", (str,))
    if kind not in EVENT_TYPES:
        raise ProtocolError("VALIDATION_FAILED", f"unknown event type {kind!r}", "type")
    data = _require(obj, "data", (dict,))
    try:
        data_size = len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProtocolError("VALIDATION_FAILED", "event data is not valid UTF-8 JSON", "data") from None
    if data_size > MAX_EVENT_DATA_BYTES:
        raise ProtocolError("PAYLOAD_TOO_LARGE", "event data exceeds the size cap", "data")
    for field, expected in _PER_TYPE[kind]:
        _require(data, field, (expected,))
    if kind == "state" and data["state"] not in TASK_STATES:
        raise ProtocolError("VALIDATION_FAILED", f"unknown task state {data['state']!r}", "data.state")
    if kind == "proposal_completed" and data["applied"] is not False:
        raise ProtocolError("VALIDATION_FAILED", "a proposal must never claim to be applied", "data.applied")
    return obj

def validate_events_response(obj):
    if not isinstance(obj, dict):
        raise ProtocolError("VALIDATION_FAILED", "response must be an object")
    check_version(obj.get("protocol"))
    _require(obj, "events", (list,))
    state = _require(obj, "state", (str,))
    if state not in TASK_STATES:
        raise ProtocolError("VALIDATION_FAILED", f"unknown task state {state!r}", "state")
    terminal = _require(obj, "terminal", (bool,))
    if terminal != (state in TERMINAL_STATES):
        raise ProtocolError("VALIDATION_FAILED", "terminal must agree with state", "terminal")
    _require(obj, "next_cursor", (int,))
    last = 0
    for event in obj["events"]:
        validate_event(event)
        if int(event["event_id"]) <= last:
            raise ProtocolError("VALIDATION_FAILED", "event ids must be strictly increasing", "events")
        last = int(event["event_id"])
    return obj

def validate_capabilities(obj):
    if not isinstance(obj, dict):
        raise ProtocolError("VALIDATION_FAILED", "capabilities must be an object")
    check_version(obj.get("protocol"))
    if _require(obj, "available", (bool,)) and not obj.get("modes"):
        raise ProtocolError("VALIDATION_FAILED", "an available service must advertise modes", "modes")
    modes = _require(obj, "modes", (list,))
    if not set(modes) <= {"read", "propose", "apply", "test"}:
        raise ProtocolError("VALIDATION_FAILED", "unknown mode advertised", "modes")
    mutation = _require(obj, "mutation", (bool,))
    if mutation != bool(set(modes) & {"apply", "test"}):
        raise ProtocolError("VALIDATION_FAILED", "mutation must agree with the advertised modes", "mutation")
    return obj

def validate_error(obj):
    if not isinstance(obj, dict) or not isinstance(obj.get("error"), dict):
        raise ProtocolError("VALIDATION_FAILED", "error response must carry an `error` object")
    check_version(obj.get("protocol"))
    code = obj["error"].get("code")
    if code not in ERROR_STATUS or not isinstance(obj["error"].get("message"), str):
        raise ProtocolError("VALIDATION_FAILED", "error needs a known code and a message", "error")
    return obj
