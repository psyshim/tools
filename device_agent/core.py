"""Framework-independent device logic: sessions over granted workspaces, the effect journal, the single-executor locks.

Everything here is decided by the device's own configuration. The service names a workspace *id* and a tool; it cannot name a
path, a root, a command line or a capability.
"""
import json
import os
import re
import sqlite3
import threading
import time

from code_attach.tools import ToolBox
from coding_agent.execution_owner import OwnerBusy, SingleExecutor
from coding_agent.journal import OperationConflict, OperationJournal

SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
OP_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
MAX_SESSIONS = 64
SESSION_IDLE_SECONDS = 3600
MAX_RESULT_BYTES = 1024 * 1024
HANDLED = (ValueError, KeyError, TypeError, OSError, UnicodeError)

def failpoint(name):
    """Crash-test hook: die abruptly (no cleanup, no response) at a named point of an effect. Inert unless BOTH variables are set."""
    if os.environ.get("DEVICE_AGENT_ALLOW_FAILPOINTS") == "1" and os.environ.get("DEVICE_AGENT_FAILPOINT") == name:
        os._exit(137)

class DeviceError(Exception):
    """A refusal with an HTTP-ish meaning: status is 400/404/409/413."""
    def __init__(self, status, code, message=""):
        super().__init__(message or code)
        self.status, self.code, self.message = status, code, message or code

def _error(exc):
    return {"type": type(exc).__name__ if isinstance(exc, HANDLED) else "InternalError",
            "message": str(exc)[:2000] if isinstance(exc, HANDLED) else "internal error"}

class DeviceService:
    def __init__(self, config):
        self.config = config
        self.state_dir = config.state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.journal = OperationJournal(self.state_dir / "journal.db", check_same_thread=False)   # requests run on worker threads
        self._jlock = threading.Lock()                                                               # ... so journal access is serialized here
        self._sessions, self._lock = {}, threading.Lock()
        self._owners = []
        # The owner lease lives outside every workspace, in a per-user location shared by ALL executors on this account (so a second
        # device agent, or any other SingleExecutor user, on the same folder is refused). `lock_dir` in the grants overrides it.
        previous = os.environ.get("CODING_AGENT_STATE_DIR")
        if config.lock_dir is not None:
            os.environ["CODING_AGENT_STATE_DIR"] = str(config.lock_dir)
        try:
            for workspace in config.workspaces.values():
                self._owners.append(SingleExecutor(workspace.root))   # a second device agent on the same folder refuses to start
        except OwnerBusy:
            self.close()
            raise
        finally:
            if config.lock_dir is not None:
                if previous is None:
                    os.environ.pop("CODING_AGENT_STATE_DIR", None)
                else:
                    os.environ["CODING_AGENT_STATE_DIR"] = previous

    def close(self):
        for session in list(self._sessions.values()):
            session["toolbox"].request_cancel()
        for owner in self._owners:
            owner.close()
        self._owners = []
        self.journal.close()

    # --- information (never includes roots) ---
    def info(self):
        return {"device_id": self.config.device_id, "protocol": "device-1",
                "workspaces": {wid: {"capabilities": sorted(w.capabilities)} for wid, w in self.config.workspaces.items()}}

    # --- sessions ---
    def _evict_idle(self):
        now = time.monotonic()
        for sid in [s for s, v in self._sessions.items() if now - v["used"] > SESSION_IDLE_SECONDS]:
            self._sessions.pop(sid, None)

    def open_session(self, workspace_id, session_id):
        if not isinstance(session_id, str) or not SESSION_ID.match(session_id):
            raise DeviceError(400, "bad_request", "session_id must match [A-Za-z0-9_-]{1,64}")
        workspace = self.config.workspaces.get(workspace_id) if isinstance(workspace_id, str) else None
        if workspace is None:
            raise DeviceError(404, "unknown_workspace", "this device has no such workspace")
        with self._lock:
            self._evict_idle()
            existing = self._sessions.get(session_id)
            if existing is not None:
                if existing["workspace"].id != workspace.id:
                    raise DeviceError(409, "session_conflict", "session id is bound to another workspace")
                existing["used"] = time.monotonic()
                return self._describe(session_id, existing)
            if len(self._sessions) >= MAX_SESSIONS:
                raise DeviceError(409, "too_many_sessions", "too many open sessions")
            toolbox = ToolBox(workspace.root, workspace.test_commands if "test" in workspace.capabilities else None, 180, None,
                              recycle_dir=self.state_dir / "recycle" / workspace.id, allow_mutation=set(workspace.effects))
            entry = {"workspace": workspace, "toolbox": toolbox, "used": time.monotonic()}
            self._sessions[session_id] = entry
            return self._describe(session_id, entry)

    @staticmethod
    def _describe(session_id, entry):
        toolbox = entry["toolbox"]
        return {"session_id": session_id, "workspace_id": entry["workspace"].id, "capabilities": sorted(entry["workspace"].capabilities),
                "mutation": toolbox.allow_mutation, "effect_tools": sorted(n for n in ("apply_proposal", "run_tests") if toolbox._tool_granted(n))}

    def _session(self, session_id):
        entry = self._sessions.get(session_id) if isinstance(session_id, str) else None
        if entry is None:
            raise DeviceError(404, "unknown_session", "no such session")
        entry["used"] = time.monotonic()
        return entry

    def close_session(self, session_id):
        entry = self._sessions.pop(session_id, None)
        if entry is not None:
            entry["toolbox"].request_cancel()
        return {"closed": entry is not None}

    def schemas(self, session_id):
        return {"schemas": self._session(session_id)["toolbox"].schemas()}

    def cancel(self, session_id):
        self._session(session_id)["toolbox"].request_cancel()
        return {"cancelled": True}

    def describe_effect(self, session_id, tool, args):
        toolbox = self._session(session_id)["toolbox"]
        if not isinstance(tool, str) or not isinstance(args, dict):
            raise DeviceError(400, "bad_request", "tool must be a string and args an object")
        try:
            summary, fingerprint = toolbox.describe_effect(tool, args)
        except HANDLED as exc:
            return {"error": _error(exc)}
        return {"summary": summary, "fingerprint": fingerprint}

    # --- calls ---
    def call(self, session_id, tool, args, op_id=None):
        entry = self._session(session_id)
        toolbox = entry["toolbox"]
        if not isinstance(tool, str) or not isinstance(args, dict):
            raise DeviceError(400, "bad_request", "tool must be a string and args an object")
        if toolbox.is_effect(tool):
            return self._effect(entry, session_id, tool, args, op_id)
        return self._plain(toolbox, tool, args)

    def _plain(self, toolbox, tool, args):
        try:
            result = toolbox.call(tool, args)
        except HANDLED as exc:
            return {"error": _error(exc)}
        except Exception as exc:   # a tool bug is reported as an internal error, never as a crash of the device
            return {"error": _error(exc)}
        if len(json.dumps(result, ensure_ascii=False, default=str)) > MAX_RESULT_BYTES:
            return {"error": {"type": "ValueError", "message": "result too large; narrow the request"}}
        return {"result": result}

    def _effect(self, entry, session_id, tool, args, op_id):
        """Effects are journaled: prepare -> claim (once, globally) -> execute -> record. A repeated op_id returns the
        recorded outcome and never executes again; a crash between claim and record leaves UNKNOWN, resolved by inspection,
        never by silently repeating."""
        if not isinstance(op_id, str) or not OP_ID.match(op_id):
            raise DeviceError(400, "bad_request", "effects need an op_id ([A-Za-z0-9_.:-]{1,128}) chosen before the request is sent")
        request = {"workspace": entry["workspace"].id, "session": session_id, "tool": tool, "args": args}
        try:
            with self._jlock:
                row = self.journal.prepare(op_id, request)
        except OperationConflict:
            raise DeviceError(409, "op_conflict", "op_id was already used for a different request") from None
        if row["state"] in ("SUCCEEDED", "FAILED"):
            return {**row["result"], "replayed": True}
        with self._jlock:
            won = self.journal.claim(op_id) if row["state"] == "PREPARED" else False
        if not won:
            return {"error": {"type": "UnknownOutcome", "message": "an earlier attempt may or may not have taken effect; inspect the workspace and GET /v1/operations/" + op_id},
                    "unknown_outcome": True, "op_id": op_id}
        failpoint("after_claim")      # claimed, nothing done yet
        outcome, succeeded = None, False
        try:
            result = entry["toolbox"].call(tool, args)
            failpoint("after_effect")   # done, not yet recorded: the dangerous moment
            status = result.get("status") if isinstance(result, dict) else None
            if status in ("NEEDS_RECONCILIATION", "UNKNOWN", "UNKNOWN_OUTCOME"):
                return {"result": result, "unknown_outcome": True, "op_id": op_id}
            outcome, succeeded = {"result": result}, status != "CONFLICT"
        except Exception:
            # Once dispatched, even OSError/ValueError may follow a real write.
            # Keep the claim UNKNOWN; neither retry nor assert a clean failure.
            return {"error": {"type": "UnknownOutcome", "message": "action raised after dispatch; inspect the workspace"},
                    "unknown_outcome": True, "op_id": op_id}
        try:
            with self._jlock:
                self.journal.reconcile(op_id, outcome, succeeded)
        except (sqlite3.Error, OSError, ValueError, OperationConflict):
            return {"error": {"type": "UnknownOutcome", "message": "action ran but its receipt could not be confirmed"},
                    "unknown_outcome": True, "op_id": op_id}
        failpoint("after_record")     # recorded, response not yet sent: a retry must replay, never repeat
        return outcome

    def operation(self, op_id):
        if not isinstance(op_id, str) or not OP_ID.match(op_id):
            raise DeviceError(400, "bad_request", "invalid op_id")
        with self._jlock:
            row = self.journal.conn.execute("SELECT state, result FROM operations WHERE id=?", (op_id,)).fetchone()
        if row is None:
            raise DeviceError(404, "unknown_operation", "no such operation")
        return {"op_id": op_id, "state": row[0], "result": json.loads(row[1]) if row[1] else None}
