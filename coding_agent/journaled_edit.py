"""Fail-closed journaled facade for experimental local edits.

UNKNOWN never automatically retries. This does not solve external editor
TOCTOU races; production use requires an exclusive file-operation owner.
"""
import threading

from .journal import OperationConflict
from .workflow import Conflict

_GUARD = threading.RLock()

class JournaledEdit:
    def __init__(self, workflow, journal):
        self.workflow = workflow
        self.journal = journal

    def replace_text(self, operation_id, relative, expected_sha256, original, replacement):
        request = {"kind": "replace_text_v1", "path": relative,
                   "expected_sha256": expected_sha256, "original": original,
                   "replacement": replacement}
        with _GUARD:
            recorded = self.journal.prepare(operation_id, request)
            if recorded["state"] == "SUCCEEDED":
                return recorded["result"]
            if recorded["state"] != "PREPARED":
                raise OperationConflict("operation not safely replayable; reconciliation required")
            # Only one process may claim the effect; UNKNOWN remains fail-closed.
            if not self.journal.claim(operation_id):
                raise OperationConflict("operation already claimed")
            try:
                result = self.workflow.replace_text(relative, expected_sha256, original, replacement)
            except (Conflict, ValueError, UnicodeError) as exc:
                raise OperationConflict("edit not confirmed: " + type(exc).__name__) from exc
            verified = self.workflow.inspect(relative)
            if verified["sha256"] != result["sha256"]:
                raise OperationConflict("post-write verification failed")
            answer = {"operation_id": operation_id, "path": relative,
                      "sha256": verified["sha256"]}
            self.journal.reconcile(operation_id, answer, True)
            return answer
