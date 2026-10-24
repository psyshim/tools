"""AgentGateway: everything a host needs to talk to the coding service, and nothing else.

The host supplies only what it already owns: a verified user identity, its own workspace ACL decision, and a way to show
chat events. This module owns the protocol: stable idempotency keys, bounded retry, event cursor and de-duplication, gap
markers, capability/version checks, typed errors. It imports no agent, tool, file, Git, subprocess or model code, holds no
task state machine, and cannot execute anything.

Four lifecycle operations (+ optional decision forwarding), per ADR-004:
    start_coding   -> TaskHandle
    observe_coding -> iterator of SafeDisplayEvent (resumable)
    cancel_coding  -> CancelResult
    availability   -> Available | Unavailable        (never raises)
    submit_decision (optional; only if the host has a reliable approval UI)
"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from protocol import (PROTOCOL_VERSION, TERMINAL_STATES, ProtocolError, check_version, validate_capabilities, validate_error, validate_events_response)

from .errors import AgentError, AgentUnavailable, ProtocolRefused
from .events import SafeDisplayEvent, clean_data

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
RETRYABLE_STATUS = (502, 503, 504)

@dataclass(frozen=True)
class Available:
    modes: tuple
    mutation: bool
    ok: bool = True

@dataclass(frozen=True)
class Unavailable:
    reason: str
    ok: bool = False

@dataclass(frozen=True)
class TaskHandle:
    """Opaque and non-authorizing: every call re-authenticates the principal on the service."""
    task_id: str

    def __post_init__(self):
        if not isinstance(self.task_id, str) or not HANDLE_PATTERN.match(self.task_id):
            raise ValueError("invalid task handle")

@dataclass(frozen=True)
class PollResult:
    events: tuple
    terminal: bool
    next_token: int

@dataclass(frozen=True)
class CancelResult:
    state: str
    terminal: bool

@dataclass(frozen=True)
class DecisionResult:
    accepted: bool
    already_decided: bool = False

def urllib_transport(method, url, headers, body, timeout):
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        return error.code, error.read(MAX_RESPONSE_BYTES + 1)

def _clean_principal(principal):
    if not isinstance(principal, str) or not principal or len(principal) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in principal):
        raise ValueError("principal must be a non-empty string without control characters (at most 256 characters)")
    return principal

class AgentGateway:
    def __init__(self, base_url, credential, *, transport=None, timeout=10.0, retries=3, backoff=0.2, sleep=time.sleep):
        if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        if not isinstance(credential, str) or not credential:
            raise ValueError("a service credential is required")
        self._base, self._credential = base_url.rstrip("/"), credential
        self._transport, self._timeout, self._retries, self._backoff, self._sleep = transport or urllib_transport, timeout, max(1, int(retries)), backoff, sleep

    # --- transport with bounded retry (only for calls that are safe to repeat) ---
    def _request(self, method, path, principal=None, body=None, repeatable=True):
        headers = {"Authorization": "Bearer " + self._credential, "X-Protocol-Version": PROTOCOL_VERSION, "Accept": "application/json"}
        if principal is not None:
            headers["X-Principal"] = urllib.parse.quote(_clean_principal(principal), safe="")
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        attempts = self._retries if repeatable else 1
        failure = None
        for attempt in range(attempts):
            try:
                status, raw = self._transport(method, self._base + path, headers, data, self._timeout)
            except OSError as exc:   # refused, reset, DNS, timeout
                failure = AgentUnavailable("unreachable", type(exc).__name__)
            else:
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise AgentUnavailable("response_too_large")
                if status in RETRYABLE_STATUS and attempt + 1 < attempts:
                    failure = AgentUnavailable("service_busy", str(status))
                else:
                    return self._parse(status, raw)
            if attempt + 1 < attempts:
                self._sleep(self._backoff * (2 ** attempt))
        raise failure

    @staticmethod
    def _parse(status, raw):
        try:
            body = json.loads(raw)
        except (ValueError, RecursionError):
            raise AgentUnavailable("bad_gateway" if status >= 500 else "malformed_response", f"status {status}") from None
        if not isinstance(body, dict):
            raise AgentUnavailable("malformed_response", f"status {status}")
        if "error" in body:
            try:
                validate_error(body)
            except ProtocolError as bad:
                raise AgentUnavailable("protocol_mismatch" if bad.code == "VERSION_MISMATCH" else "contract_violation", f"status {status}") from None
            err = body["error"]
            code = err["code"]
            if code == "VERSION_MISMATCH":
                raise AgentUnavailable("protocol_mismatch")
            if code == "UNAUTHENTICATED":
                raise AgentUnavailable("credential_rejected")   # a host misconfiguration, not something the end user can fix
            if code in ("UNAVAILABLE", "LOG_UNAVAILABLE"):
                raise AgentUnavailable(code.lower())
            raise ProtocolRefused(code, err.get("message", ""), err.get("field"), status)
        if not 200 <= status < 300:
            raise AgentUnavailable("unexpected_status", str(status))
        try:
            check_version(body.get("protocol"))
        except ProtocolError:
            raise AgentUnavailable("protocol_mismatch") from None
        return body

    # --- the lifecycle ---
    def availability(self):
        try:
            body = self._request("GET", "/availability")
            validate_capabilities(body)
        except AgentUnavailable as exc:
            return Unavailable(exc.reason)
        except (AgentError, ProtocolError):
            return Unavailable("contract_violation")
        return Available(tuple(body["modes"]), bool(body["mutation"])) if body["available"] else Unavailable("service_reports_unavailable")

    def start_coding(self, principal, workspace_id, text, *, conversation_id, message_id, mode="propose"):
        """Submit one chat message as a coding task. The idempotency key derives from (conversation_id, message_id), so a retry
        after a lost response, a host restart or a double click is the SAME task, never a second one."""
        _clean_principal(principal)
        for name, value in (("workspace_id", workspace_id), ("text", text), ("conversation_id", conversation_id), ("message_id", message_id)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        key = hashlib.sha256(f"{conversation_id}\x1f{message_id}".encode("utf-8")).hexdigest()[:48]
        body = self._request("POST", "/tasks", principal, {"protocol": PROTOCOL_VERSION, "workspace_id": workspace_id, "instruction": text, "idempotency_key": key, "mode": mode})
        task_id = body.get("task_id")
        if not isinstance(task_id, str) or not HANDLE_PATTERN.match(task_id):
            raise AgentUnavailable("contract_violation", "task_id missing or malformed")
        return TaskHandle(task_id)

    def poll_coding(self, principal, handle, resume_token=None):
        """One request: the events after `resume_token` that have not been rendered, a gap marker if some are missing,
        whether the task is terminal, and the token to pass next time (the last rendered event_id). For hosts that poll
        from their own UI loop instead of holding a stream open."""
        _clean_principal(principal)
        cursor = seen_up_to = self._cursor(resume_token)
        for _ in range(2):   # the second pass only happens after a CURSOR_EXPIRED answer
            try:
                body = self._request("GET", f"/tasks/{handle.task_id}/events?after={cursor}", principal)
                validate_events_response(body)
                break
            except ProtocolRefused as exc:
                if exc.code != "CURSOR_EXPIRED":
                    raise
                # Retention dropped our position: restart from what the service still has. De-duplication below skips what was
                # already rendered and a visible gap marker is emitted if anything in between is gone.
                cursor = 0
            except ProtocolError:
                raise AgentUnavailable("contract_violation") from None
        else:
            raise AgentUnavailable("contract_violation", "cursor expired repeatedly")
        out = []
        for event in body["events"]:
            event_id = int(event["event_id"])
            if event_id <= seen_up_to:
                continue   # duplicate delivery is legal; render once
            if event_id > seen_up_to + 1:
                out.append(SafeDisplayEvent(handle.task_id, 0, "gap", {"missing_from": str(seen_up_to + 1), "missing_to": str(event_id - 1)}, synthetic=True))
            seen_up_to = event_id
            out.append(SafeDisplayEvent(handle.task_id, event_id, event["type"], clean_data(event["data"]), terminal=body["terminal"] and event_id == body["next_cursor"]))
        finished = body["terminal"] and seen_up_to >= body["next_cursor"]
        return PollResult(tuple(out), finished, seen_up_to)

    def observe_coding(self, principal, handle, resume_token=None, *, poll_interval=0.5, max_seconds=3600.0, should_continue=None):
        """Yield SafeDisplayEvents until the task is terminal (see poll_coding), stopping quietly when `should_continue()` turns
        false (for example because the host revoked the user's session). Resume with the last event_id you rendered. An empty
        stream, a timeout or HTTP success is never reported as task success."""
        token = resume_token
        deadline = time.monotonic() + max_seconds
        while True:
            if should_continue is not None and not should_continue():
                return
            result = self.poll_coding(principal, handle, token)
            yield from result.events
            if result.terminal:
                return
            token = result.next_token
            if time.monotonic() > deadline:
                raise AgentUnavailable("observe_timeout", f"resume from event {token}")
            self._sleep(poll_interval)

    def cancel_coding(self, principal, handle):
        """Ask the service to stop. The answer is the service's persisted state (a task that already finished stays finished);
        this client never invents CANCELLED."""
        _clean_principal(principal)
        body = self._request("POST", f"/tasks/{handle.task_id}/cancel", principal, {})
        state = body.get("state")
        if not isinstance(state, str):
            raise AgentUnavailable("contract_violation")
        return CancelResult(state, state in TERMINAL_STATES)

    def submit_decision(self, principal, handle, action_id, fingerprint, approve):
        """Forward the user's explicit answer to an approval card, echoing action_id and fingerprint unchanged.
        Not retried blindly: if the first attempt's outcome is unknown, the event log decides."""
        _clean_principal(principal)
        payload = {"protocol": PROTOCOL_VERSION, "action_id": action_id, "fingerprint": fingerprint, "approve": bool(approve)}
        path = f"/tasks/{handle.task_id}/decision"
        try:
            self._request("POST", path, principal, payload, repeatable=False)
            return DecisionResult(True)
        except AgentUnavailable:
            pass   # outcome unknown: look at the log instead of repeating a side-effecting request
        except ProtocolRefused as exc:
            if exc.code != "NO_PENDING_ACTION":
                raise
            return DecisionResult(False, self._decision_recorded(principal, handle, action_id))
        return DecisionResult(self._decision_recorded(principal, handle, action_id), True)

    def _decision_recorded(self, principal, handle, action_id):
        body = self._request("GET", f"/tasks/{handle.task_id}/events?after=0", principal)
        validate_events_response(body)
        return any(e["type"] == "approval_decision" and e["data"].get("action_id") == action_id for e in body["events"])

    @staticmethod
    def _cursor(token):
        if token in (None, ""):
            return 0
        if isinstance(token, bool) or not (isinstance(token, int) or (isinstance(token, str) and token.isascii() and token.isdigit())):
            raise ValueError("resume_token must be the last rendered event_id")
        value = int(token)
        if value < 0 or value > 10 ** 15:
            raise ValueError("resume_token must be the last rendered event_id")
        return value
