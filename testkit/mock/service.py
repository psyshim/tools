"""An executable mock of the coding service, written only against `protocol`.

It exists so that a host client can be developed and tested without any agent, filesystem or model, and so that the real
service can be held to the same behaviour. It deliberately shares NO code with the real implementation: it must not
import the agent, tool, file or Git packages (tests/test_protocol_contract.py enforces this by scanning imports).

Scripted behaviour keyed by the instruction text:
  "propose ..."  assistant message, one proposal_completed (applied=false), COMPLETED
  "approve ..."  waits for a decision on a (mock) approval card, then COMPLETED; the mock NEVER reports a mutation
  anything else  one assistant message, COMPLETED
"""
import hashlib
import json
import threading
import uuid
from urllib.parse import parse_qs, urlsplit

from protocol import schema
from protocol.schema import ProtocolError

class MockCodingService:
    def __init__(self, tokens, workspaces):
        """tokens: {bearer_token: principal}; workspaces: {principal: {workspace_id, ...}}."""
        self.tokens, self.workspaces = dict(tokens), {k: set(v) for k, v in workspaces.items()}
        self._tasks, self._keys = {}, {}
        self._lock = threading.Lock()

    # --- single entry point: (method, path, headers, body) -> (status, json) ---
    def handle(self, method, path, headers=None, body=None):
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        try:
            principal = self._authenticate(headers)
            schema.check_version(headers.get("x-protocol-version"))
            parts = urlsplit(path)
            segments = [s for s in parts.path.split("/") if s]
            query = parse_qs(parts.query)
            if method == "GET" and segments == ["availability"]:
                return 200, {"protocol": schema.PROTOCOL_VERSION, "available": True, "modes": ["read", "propose"], "mutation": False}
            if method == "POST" and segments == ["tasks"]:
                return 202, self._create(principal, body)
            if len(segments) == 3 and segments[0] == "tasks":
                task = self._own(principal, segments[1])
                if method == "GET" and segments[2] == "events":
                    return 200, self._events(task, query)
                if method == "POST" and segments[2] == "decision":
                    return 200, self._decide(task, body)
                if method == "POST" and segments[2] == "cancel":
                    return 200, self._cancel(task)
            raise ProtocolError("NOT_FOUND", "no such route")
        except ProtocolError as exc:
            return exc.status, exc.envelope()

    # --- behaviour ---
    def _authenticate(self, headers):
        supplied = headers.get("authorization", "")
        principal = self.tokens.get(supplied[7:]) if supplied.startswith("Bearer ") else None
        if principal is None:
            raise ProtocolError("UNAUTHENTICATED", "missing or unknown credentials")
        return principal

    def _own(self, principal, task_id):
        task = self._tasks.get(task_id)
        if task is None or task["principal"] != principal:
            raise ProtocolError("NOT_FOUND", "task not found")   # same answer for 'absent' and 'someone else's'
        return task

    def _emit(self, task, kind, data):
        event = {"protocol": schema.PROTOCOL_VERSION, "task_id": task["id"], "event_id": str(len(task["events"]) + 1), "type": kind, "data": data}
        task["events"].append(event)

    def _create(self, principal, body):
        request = schema.validate_task_request(body)
        if request["workspace_id"] not in self.workspaces.get(principal, ()):
            raise ProtocolError("NOT_FOUND", "workspace not found")
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        with self._lock:
            known = self._keys.get((principal, request["idempotency_key"]))
            if known:
                task = self._tasks[known]
                if task["digest"] != digest:
                    raise ProtocolError("IDEMPOTENCY_CONFLICT", "idempotency key reused with different content", "idempotency_key")
                return {"protocol": schema.PROTOCOL_VERSION, "task_id": task["id"], "state": task["state"]}
            task = {"id": uuid.uuid4().hex, "principal": principal, "digest": digest, "events": [], "state": "RUNNING", "pending": None}
            self._tasks[task["id"]] = task
            self._keys[(principal, request["idempotency_key"])] = task["id"]
        text = request["instruction"]
        self._emit(task, "assistant_message", {"text": "mock: working on it"})
        if text.startswith("approve"):
            task["pending"] = {"action_id": "mock-action-1", "fingerprint": "mock-fingerprint-1"}
            task["state"] = "AWAITING_APPROVAL"
            self._emit(task, "approval_required", {**task["pending"], "tool": "mock_action", "summary": {"kind": "mock"}})
        else:
            if text.startswith("propose"):
                self._emit(task, "proposal_completed", {"proposal_id": "mock-proposal-1", "files": [{"path": "example.txt", "op": "write"}], "applied": False})
            self._finish(task, "COMPLETED")
        return {"protocol": schema.PROTOCOL_VERSION, "task_id": task["id"], "state": task["state"]}

    def _finish(self, task, state):
        task["state"] = state
        self._emit(task, "state", {"state": state})

    def _events(self, task, query):
        after = schema.parse_cursor((query.get("after") or ["0"])[0])
        events = [e for e in task["events"] if int(e["event_id"]) > after]
        return {"protocol": schema.PROTOCOL_VERSION, "events": events, "state": task["state"], "terminal": task["state"] in schema.TERMINAL_STATES,
                "next_cursor": int(task["events"][-1]["event_id"]) if task["events"] else 0}

    def _decide(self, task, body):
        decision = schema.validate_decision(body)
        pending = task["pending"]
        if not pending or pending["action_id"] != decision["action_id"] or pending["fingerprint"] != decision["fingerprint"]:
            raise ProtocolError("NO_PENDING_ACTION", "no matching pending action")
        task["pending"] = None
        self._emit(task, "approval_decision", {"action_id": decision["action_id"], "approve": decision["approve"]})
        self._finish(task, "COMPLETED")
        return {"protocol": schema.PROTOCOL_VERSION, "accepted": True}

    def _cancel(self, task):
        if task["state"] not in schema.TERMINAL_STATES:
            task["pending"] = None
            self._finish(task, "CANCELLED")
        return {"protocol": schema.PROTOCOL_VERSION, "state": task["state"]}
