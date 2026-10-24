"""One task = one model/tool loop in a thread, with an append-only event list and approval gating."""
import json
import threading
import uuid
from .schemas import EFFECT_TOOLS

SYSTEM = (
    "You modify code in the user's local project using the provided tools. Read before editing. "
    "Edits are proposals; the user must approve apply_proposal and run_tests. After applying, run tests if available "
    "and report the actual result. Never claim a file changed or tests passed unless a tool result says so."
)
SYSTEM_PROPOSE_ONLY = (
    "You help with code in the user's local project using the provided tools. You can read, search and PROPOSE changes, but you "
    "cannot apply them or run anything: every proposal is a patch for the user to review. Say so plainly and never claim that a "
    "file changed or that tests passed."
)
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
MAX_CALLS_PER_TURN = 32      # a model cannot make one turn arbitrarily expensive
MAX_TEXT = 65536             # assistant text kept in events and history
MAX_ARGS_JSON = 262144       # per tool call
KEEP_FULL_TOOL_RESULTS = 6   # older tool results are shortened in what the model sees (the event log keeps everything)
SHORT_TOOL_RESULT = 600

class LogUnavailable(OSError):
    """The activity sink failed. Raised only for sink failures, so callers can tell them from tool errors."""

class Task:
    """One model/tool loop running in its own thread. States: RUNNING, AWAITING_APPROVAL, then COMPLETED, FAILED or CANCELLED."""
    def __init__(self, principal, toolbox, complete, instruction, max_steps=20, approval_timeout=600, event_sink=None):
        self.id = uuid.uuid4().hex
        self.principal = principal
        self.toolbox = toolbox
        self.complete = complete
        self.max_steps = max_steps
        self.approval_timeout = approval_timeout
        self.state = "RUNNING"
        self.events = []
        self._event_sink = event_sink
        self.pending = None
        self._cancel = False
        self._private = 0
        self._cv = threading.Condition()   # guards state and the visible event list; never held across sink I/O
        self._emit_lock = threading.Lock()  # serializes emitters so write-ahead logging keeps event order
        system = SYSTEM if getattr(toolbox, "allow_mutation", True) else SYSTEM_PROPOSE_ONLY
        self.messages = [{"role": "system", "text": system}, {"role": "user", "text": instruction}]
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    # --- events ---
    def emit(self, type_, data, private=False):
        """Write-ahead: the sink sees the event first; if it fails the event is not published (LogUnavailable).
        Private events go to the sink only and are never visible through events_after()."""
        with self._emit_lock:
            with self._cv:
                if private:
                    self._private += 1
                    event_id = f"p{self._private}"
                else:
                    event_id = str(len(self.events) + 1)
            event = {"id": event_id, "type": type_, "data": data}
            if self._event_sink is not None:
                try:
                    self._event_sink(self.id, event)
                except (OSError, ValueError, TypeError, UnicodeError) as exc:  # unserializable data is a log failure too: fail closed
                    raise LogUnavailable(type(exc).__name__) from None
            if not private:
                with self._cv:
                    self.events.append(event)
                    self._cv.notify_all()

    def _emit_best_effort(self, type_, data, fallback):
        """Publish even when the sink is down; the event then carries `fallback` so the failure is visible."""
        try:
            self.emit(type_, data)
        except LogUnavailable:
            with self._emit_lock, self._cv:
                self.events.append({"id": str(len(self.events) + 1), "type": type_, "data": fallback})
                self._cv.notify_all()

    def events_after(self, after=0):
        with self._cv:
            return list(self.events[after:]), self.state

    # --- control ---
    def cancel(self):
        with self._cv:
            if self.state in TERMINAL or self._cancel:
                return
            self._cancel = True   # takes effect first: a broken log must never prevent stopping work
            self._cv.notify_all()
        self.toolbox.request_cancel()
        self._emit_best_effort("cancel_requested", {}, {"log_error": "activity log unavailable"})

    def decide(self, action_id, fingerprint, approve):
        with self._cv:
            p = self.pending
            if not p or p["action_id"] != action_id or p["fingerprint"] != fingerprint or p["decision"] is not None or p["claimed"]:
                return False
            p["claimed"] = True   # exactly one decision wins even while the log write below is in flight
        try:
            self.emit("approval_decision", {"action_id": action_id, "fingerprint": fingerprint, "approve": bool(approve)})
        except LogUnavailable:
            with self._cv:
                p["claimed"] = False
            raise
        with self._cv:
            p["decision"] = bool(approve)
            self._cv.notify_all()
        return True

    def _finish(self, state, detail=None):
        data = {"state": state, **({"detail": detail} if detail else {})}
        try:
            self.emit("state", data)
        except LogUnavailable:
            # Logging failure cannot leave a worker stuck or read as success.
            state, data = "FAILED", {"state": "FAILED", "detail": "activity log unavailable"}
            with self._emit_lock, self._cv:
                self.events.append({"id": str(len(self.events) + 1), "type": "state", "data": data})
        with self._cv:
            self.state = state
            self._cv.notify_all()

    # --- loop ---
    def _await(self, name, args, call_id):
        summary, fingerprint = self.toolbox.describe_effect(name, args)
        pending = {"action_id": call_id, "fingerprint": fingerprint, "decision": None, "claimed": False}
        with self._cv:
            self.pending = pending  # decidable from the moment the card is visible
        self.emit("approval_required", {"action_id": call_id, "fingerprint": fingerprint, "tool": name, "summary": summary})
        with self._cv:
            if pending["decision"] is None and not pending["claimed"]:
                self.state = "AWAITING_APPROVAL"  # never advertised before the card exists, so state==AWAITING implies the card is in the events
            self._cv.wait_for(lambda: pending["decision"] is not None or self._cancel, self.approval_timeout)
            expired = pending["decision"] is None and not self._cancel
            approved = pending["decision"] is True and not self._cancel
            self.pending = None
            if not self._cancel:
                self.state = "RUNNING"
        if expired:
            self._emit_best_effort("approval_expired", {"action_id": call_id}, {"action_id": call_id, "log_error": "activity log unavailable"})
        return approved

    def _model_view(self):
        """What the model sees: full history, but old tool results shortened so long tasks stay within context."""
        tool_idx = [i for i, m in enumerate(self.messages) if m["role"] == "tool"]
        old = set(tool_idx[:-KEEP_FULL_TOOL_RESULTS]) if len(tool_idx) > KEEP_FULL_TOOL_RESULTS else set()
        view = []
        for i, m in enumerate(self.messages):
            if i in old and len(m["content"]) > SHORT_TOOL_RESULT:
                m = {**m, "content": m["content"][:SHORT_TOOL_RESULT] + " ...[older result shortened]"}
            view.append(m)
        return view

    @staticmethod
    def _safe_args(args):
        """Arguments must survive json.dumps (the host converts history to provider JSON): reject too deep or too large."""
        if args is None:
            return {}
        try:
            if len(json.dumps(args, ensure_ascii=False)) <= MAX_ARGS_JSON:
                return args
        except (RecursionError, ValueError, TypeError):
            pass
        return {"_rejected": "arguments too large, too deep or not serializable"}

    @staticmethod
    def _normalize_calls(raw, step):
        """Tolerate sloppy model output: every call gets a dict shape, a string name, a unique id and bounded arguments."""
        calls = []
        for i, c in enumerate(raw if isinstance(raw, list) else []):
            c = c if isinstance(c, dict) else {}
            calls.append({"id": str(c.get("id") or f"auto-{step}-{i}"), "name": str(c.get("name") or "")[:128],
                          "args": Task._safe_args(c.get("args"))})
        return calls

    def _run(self):
        try:
            for step in range(self.max_steps):
                if self._cancel:
                    return self._finish("CANCELLED")
                reply = self.complete(self._model_view(), self.toolbox.schemas())
                if self._cancel:
                    return self._finish("CANCELLED")
                text, calls = str(reply.get("text") or ""), self._normalize_calls(reply.get("tool_calls"), step)
                if len(text) > MAX_TEXT:
                    text = text[:MAX_TEXT] + " ...[truncated]"
                if len(calls) > MAX_CALLS_PER_TURN:
                    self.emit("tool_calls_dropped", {"count": len(calls) - MAX_CALLS_PER_TURN, "limit": MAX_CALLS_PER_TURN})
                    calls = calls[:MAX_CALLS_PER_TURN]
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
        name, args, cid = call["name"], call["args"], call["id"]
        try:
            if name in EFFECT_TOOLS and not self._await(name, args, cid):
                self.emit("tool_result", {"tool": name, "status": "NOT_APPROVED"})
                return {"status": "NOT_APPROVED"}
            result = self.toolbox.call(name, args)
        except LogUnavailable:
            raise  # a broken log is not a tool error
        except (ValueError, KeyError, TypeError, OSError, UnicodeError) as exc:
            result = {"status": "ERROR", "error": self.toolbox.scrub(f"{type(exc).__name__}: {exc}")}
        self.emit("tool_result", {"tool": name, "result": result})
        return result
