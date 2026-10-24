"""Typed failures. The host maps these to its own UI; none of them carries authority or internal detail."""


class AgentError(Exception):
    """Base class: anything the coding agent integration can raise."""


class AgentUnavailable(AgentError):
    """The service cannot be used right now (down, timing out, wrong protocol major, or violating the contract).

    The host's ordinary chat must keep working: catch this, show 'coding assistant unavailable', carry on.
    `reason` is a short stable token, never raw server text.
    """

    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


class ProtocolRefused(AgentError):
    """The service understood the request and said no, with a stable machine-readable `code` (see protocol.ERROR_CODES)."""

    def __init__(self, code, message="", field=None, status=0):
        super().__init__(f"{code}: {message}" if message else code)
        self.code, self.message, self.field, self.status = code, message, field, status
