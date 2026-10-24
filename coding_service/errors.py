"""Errors shared by every task manager implementation (in-process prototype and the durable service)."""


class NotFound(Exception):
    """The task does not exist for this principal (also used for 'someone else's task': the two are indistinguishable)."""


class KeyConflict(Exception):
    """An idempotency key was reused with different content."""


class Busy(Exception):
    """Too many active tasks."""


class BadWorkspace(Exception):
    """The workspace cannot be used (unknown, unreachable or rejected by the device)."""


class LogUnavailable(OSError):
    """The activity sink or the durable store failed. Raised only for those failures so callers can tell them from tool errors."""
