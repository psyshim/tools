"""One task = one model/tool loop in a thread, with an append-only event list and approval gating."""
import json
import threading
import uuid
from .tools import EFFECT_TOOLS

SYSTEM = (
    "You modify code in the user's local project using the provided tools. Read before editing. "
    "Edits are proposals; the user must approve apply_proposal and run_tests. After applying, run tests if available "
    "and report the actual result. Never claim a file changed or tests passed unless a tool result says so."
)
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}

class Task:
    def __init__(self, principal, toolbox, complete, instruction, max_steps=20, approval_timeout=600):
        self.id = uuid.uuid4().hex
        self.principal = principal
        self.toolbox = toolbox
        self.complete = complete
        self.max_steps = max_steps
        self.approval_timeout = approval_timeout
        self.state = "RUNNING"
        self.events = []
        self.pending = None
        self._cancel = False
        self._cv = threading.Condition()
        self.messages = [{"role": "system", "text": SYSTEM}, {"role": "user", "text": instruction}]
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def emit(self, type_, data):
        with self._cv:
            self.events.append({"id": str(len(self.events) + 1), "type": type_, "data": data})
            self._cv.notify_all()

    def events_after(self, after=0):
        with self._cv:
            return list(self.events[after:]), self.state

    def cancel(self):
        with self._cv:
            self._cancel = True
            self._cv.notify_all()

    def decide(self, action_id, fingerprint, approve):
        with self._cv:
            p = self.pending
            if not p or p["action_id"] != action_id or p["fingerprint"] != fingerprint or p["decision"] is not None:
                return False
            p["decision"] = bool(approve)
            self._cv.notify_all()
            return True

    def _finish(self, state, detail=None):
        with self._cv:
            self.state = state
        self.emit("state", {"state": state, **({"detail": detail} if detail else {})})

    def _await(self, name, args, call_id):
        summary, fingerprint = self.toolbox.describe_effect(name, args)
        pending = {"action_id": call_id, "fingerprint": fingerprint, "decision": None}
        with self._cv:
            self.pending = pending
            self.state = "AWAITING_APPROVAL"
        self.emit("approval_required", {"action_id": call_id, "fingerprint": fingerprint, "tool": name, "summary": summary})
        with self._cv:
            self._cv.wait_for(lambda: pending["decision"] is not None or self._cancel, self.approval_timeout)
            approved = pending["decision"] is True and not self._cancel
            self.pending = None
            if not self._cancel:
                self.state = "RUNNING"
        return approved

    def _run(self):
        try:
            for _ in range(self.max_steps):
                if self._cancel:
                    return self._finish("CANCELLED")
                reply = self.complete(self.messages, self.toolbox.schemas())
                text, calls = reply.get("text") or "", reply.get("tool_calls") or []
                if text:
                    self.emit("assistant", {"text": text})
                self.messages.append({"role": "assistant", "text": text, "tool_calls": calls})
                if not calls:
                    return self._finish("COMPLETED")
                for call in calls:
                    result = self._execute(call)
                    if self._cancel:
                        return self._finish("CANCELLED")
                    self.messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": json.dumps(result, ensure_ascii=False)})
            self._finish("FAILED", "step limit reached")
        except Exception as exc:  # an unknown failure must never read as success
            self._finish("FAILED", type(exc).__name__)

    def _execute(self, call):
        name, args, cid = call["name"], call.get("args", {}), call["id"]
        try:
            if name in EFFECT_TOOLS and not self._await(name, args, cid):
                self.emit("tool_result", {"tool": name, "status": "NOT_APPROVED"})
                return {"status": "NOT_APPROVED"}
            result = self.toolbox.call(name, args)
        except (ValueError, KeyError, TypeError, OSError, UnicodeError) as exc:
            result = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
        self.emit("tool_result", {"tool": name, "result": result})
        return result
