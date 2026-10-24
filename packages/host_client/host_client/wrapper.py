"""The wrapper the host maintainer finishes: this module is everything about it that is not specific to their product.

The host implements `HostEnvironment` (two required methods, two optional) and mounts the routes in `ROUTES` in whatever web
framework it already has. `CodingEndpoints.dispatch` does the rest: input validation, the host's workspace ACL, the feature
flag, error-to-status mapping, and calling `AgentGateway`. No task state, retries, file, Git, model or process code lives here
or may be added by the host.

    class MyHost(HostEnvironment):
        def can_access_workspace(self, user, workspace_id): return my_acl(user, workspace_id)
    endpoints = CodingEndpoints(AgentGateway(url, credential), MyHost())
    # in each route handler, after the host's own login check has produced `user`:
    status, body = endpoints.dispatch("POST", "/coding/start", user=user, body=parsed_json)

The full specification and acceptance checklist are in docs/HOST_WRAPPER_SPEC.md.
"""
from collections import namedtuple

from .errors import AgentUnavailable, ProtocolRefused
from .gateway import TaskHandle

Route = namedtuple("Route", "name method path")
Reply = namedtuple("Reply", "status body")

#: The browser-facing surface. `{handle}` is the opaque id returned by `start`. The host may add its own prefix.
ROUTES = (
    Route("availability", "GET", "/coding/availability"),
    Route("start", "POST", "/coding/start"),
    Route("events", "GET", "/coding/{handle}/events"),
    Route("decision", "POST", "/coding/{handle}/decision"),
    Route("cancel", "POST", "/coding/{handle}/cancel"),
)

MAX_TEXT = 20000
MAX_ID = 128

#: error token -> HTTP status. Anything not listed is a 502 (the service answered something the host does not know).
STATUS = {"forbidden": 403, "disabled": 404, "unavailable": 503, "not_found": 404, "bad_request": 400, "invalid_request": 400,
          "no_pending_action": 409, "conflict": 409, "busy": 429, "rate_limited": 429}


class HostEnvironment:
    """What only the host knows. Subclass it; everything else is provided."""

    def can_access_workspace(self, user, workspace_id):
        """REQUIRED. The host's own ACL decision: may this verified user use this workspace id? Must be a local decision
        (no call to the coding service). Return True/False; an exception counts as False."""
        raise NotImplementedError

    def coding_enabled(self, user):
        """OPTIONAL feature flag, OFF-able per user. When False every route except `availability` answers 404 `disabled`."""
        return True

    def audit(self, user, action, detail):
        """OPTIONAL. Called after each start/decision/cancel with a short action name and a small dict (ids only, no text)."""


class CodingAdapter:
    """Maps the gateway's typed results to plain JSON-able dicts. Kept separate from `dispatch` so a host can also call it
    directly from code that is not an HTTP handler."""

    def __init__(self, gateway, can_access_workspace):
        self.gateway, self.can_access_workspace = gateway, can_access_workspace

    @staticmethod
    def _failure(exc):
        if isinstance(exc, AgentUnavailable):
            return {"ok": False, "error": "unavailable", "reason": exc.reason}
        return {"ok": False, "error": exc.code.lower()}

    def _allowed(self, user, workspace_id):
        try:
            return bool(self.can_access_workspace(user, workspace_id))
        except Exception:
            return False

    def availability(self):
        got = self.gateway.availability()
        return {"available": got.ok, "reason": None if got.ok else got.reason, "modes": list(getattr(got, "modes", ())),
                "mutation": bool(getattr(got, "mutation", False))}

    def start(self, user, workspace_id, text, conversation_id, message_id):
        if not self._allowed(user, workspace_id):
            return {"ok": False, "error": "forbidden"}
        try:
            handle = self.gateway.start_coding(user, workspace_id, text, conversation_id=conversation_id, message_id=message_id)
        except (AgentUnavailable, ProtocolRefused) as exc:
            return self._failure(exc)
        return {"ok": True, "handle": handle.task_id}

    def poll(self, user, handle_id, after):
        try:
            result = self.gateway.poll_coding(user, TaskHandle(handle_id), after)
        except ValueError:
            return {"ok": False, "error": "bad_request"}      # a cursor the client cannot interpret
        except (AgentUnavailable, ProtocolRefused) as exc:
            return self._failure(exc)
        return {"ok": True, "terminal": result.terminal, "next": result.next_token,
                "events": [{"id": e.event_id, "type": e.type, "html": e.html_text, "synthetic": e.synthetic, "data": e.data} for e in result.events]}

    def decide(self, user, handle_id, action_id, fingerprint, approve):
        try:
            outcome = self.gateway.submit_decision(user, TaskHandle(handle_id), action_id, fingerprint, approve)
        except (AgentUnavailable, ProtocolRefused) as exc:
            return self._failure(exc)
        return {"ok": outcome.accepted}

    def cancel(self, user, handle_id):
        try:
            return {"ok": True, "state": self.gateway.cancel_coding(user, TaskHandle(handle_id)).state}
        except (AgentUnavailable, ProtocolRefused) as exc:
            return self._failure(exc)


def _text(body, key, limit):
    value = body.get(key) if isinstance(body, dict) else None
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


class CodingEndpoints:
    def __init__(self, gateway, environment):
        self.environment = environment
        self.adapter = CodingAdapter(gateway, environment.can_access_workspace)

    def _enabled(self, user):
        try:
            return bool(self.environment.coding_enabled(user))
        except Exception:
            return False

    def _audit(self, user, action, detail):
        try:
            self.environment.audit(user, action, detail)
        except Exception:
            pass        # an audit hook must never break the user's request

    def dispatch(self, method, path, *, user, query=None, body=None):
        """Handle one request for an ALREADY AUTHENTICATED `user`. Returns Reply(status, json_dict).
        `path` is the route path without the host's prefix; unknown routes answer 404 `not_found`."""
        if not isinstance(user, str) or not user:
            return Reply(401, {"ok": False, "error": "login_required"})
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2 or parts[0] != "coding":
            return Reply(404, {"ok": False, "error": "not_found"})
        query = query or {}
        if parts[1] == "availability" and len(parts) == 2 and method == "GET":
            return Reply(200, self.adapter.availability())
        if not self._enabled(user):
            return Reply(404, {"ok": False, "error": "disabled"})
        if parts[1] == "start" and len(parts) == 2 and method == "POST":
            fields = {k: _text(body, k, MAX_TEXT if k == "text" else MAX_ID) for k in ("workspace_id", "text", "conversation_id", "message_id")}
            if None in fields.values():
                return Reply(400, {"ok": False, "error": "bad_request"})
            out = self.adapter.start(user, fields["workspace_id"], fields["text"], fields["conversation_id"], fields["message_id"])
            if out["ok"]:
                self._audit(user, "start", {"workspace_id": fields["workspace_id"], "handle": out["handle"]})
            return self._reply(out)
        if len(parts) == 3 and len(parts[1]) <= MAX_ID:
            handle, action = parts[1], parts[2]
            if action == "events" and method == "GET":
                return self._reply(self.adapter.poll(user, handle, str(query.get("after", "0"))[:64]))
            if action == "decision" and method == "POST":
                action_id, fingerprint = _text(body, "action_id", MAX_ID), _text(body, "fingerprint", MAX_ID)
                approve = body.get("approve") if isinstance(body, dict) else None
                if action_id is None or fingerprint is None or not isinstance(approve, bool):
                    return Reply(400, {"ok": False, "error": "bad_request"})
                out = self.adapter.decide(user, handle, action_id, fingerprint, approve)
                self._audit(user, "decision", {"handle": handle, "approve": approve, "accepted": bool(out.get("ok"))})
                return self._reply(out)
            if action == "cancel" and method == "POST":
                out = self.adapter.cancel(user, handle)
                self._audit(user, "cancel", {"handle": handle})
                return self._reply(out)
        return Reply(404, {"ok": False, "error": "not_found"})

    @staticmethod
    def _reply(body):
        if body.get("ok", True) is not False:
            return Reply(200, body)
        return Reply(STATUS.get(body.get("error"), 502), body)
