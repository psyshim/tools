"""The ENTIRE host-side integration: a thin adapter between the host's existing systems and host_client.AgentGateway.

The host already has: user sessions, a workspace ACL, and a way to show chat events. It supplies exactly these three callbacks:

    identity(request)                  -> verified user id (raise to refuse)
    can_access_workspace(user, ws_id)  -> bool          (the host's own ACL decision)
    deliver(user, conversation_id, event_dict)          (render in the chat UI; escape-safe fields only)

Nothing else is written by the host: no task state machine, no retries, no event reconstruction, no file/Git/test/model code.
If the coding service is down, absent or speaks another protocol, every method degrades to a typed 'unavailable' answer and
the host's ordinary chat is untouched.
"""
from host_client import AgentUnavailable, ProtocolRefused

class CodingAdapter:
    def __init__(self, gateway, can_access_workspace):
        self.gateway, self.can_access_workspace = gateway, can_access_workspace

    def availability(self):
        got = self.gateway.availability()
        return {"available": got.ok, "reason": None if got.ok else got.reason, "modes": list(getattr(got, "modes", ()))}

    def start(self, user, workspace_id, text, conversation_id, message_id):
        if not self.can_access_workspace(user, workspace_id):
            return {"ok": False, "error": "forbidden"}
        try:
            handle = self.gateway.start_coding(user, workspace_id, text, conversation_id=conversation_id, message_id=message_id)
        except AgentUnavailable as exc:
            return {"ok": False, "error": "unavailable", "reason": exc.reason}
        except ProtocolRefused as exc:
            return {"ok": False, "error": exc.code.lower()}
        return {"ok": True, "handle": handle.task_id}

    def poll(self, user, handle_id, after):
        from host_client import TaskHandle
        try:
            result = self.gateway.poll_coding(user, TaskHandle(handle_id), after)
        except AgentUnavailable as exc:
            return {"ok": False, "error": "unavailable", "reason": exc.reason}
        except ProtocolRefused as exc:
            return {"ok": False, "error": exc.code.lower()}
        return {"ok": True, "terminal": result.terminal, "next": result.next_token,
                "events": [{"id": e.event_id, "type": e.type, "html": e.html_text, "synthetic": e.synthetic} for e in result.events]}

    def decide(self, user, handle_id, action_id, fingerprint, approve):
        from host_client import TaskHandle
        try:
            outcome = self.gateway.submit_decision(user, TaskHandle(handle_id), action_id, fingerprint, approve)
        except AgentUnavailable as exc:
            return {"ok": False, "error": "unavailable", "reason": exc.reason}
        except ProtocolRefused as exc:
            return {"ok": False, "error": exc.code.lower()}
        return {"ok": outcome.accepted}

    def cancel(self, user, handle_id):
        from host_client import TaskHandle
        try:
            return {"ok": True, "state": self.gateway.cancel_coding(user, TaskHandle(handle_id)).state}
        except AgentUnavailable as exc:
            return {"ok": False, "error": "unavailable", "reason": exc.reason}
        except ProtocolRefused as exc:
            return {"ok": False, "error": exc.code.lower()}
