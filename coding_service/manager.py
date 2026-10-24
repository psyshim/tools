"""The durable task manager: owns task identity, idempotency, the event log and terminal outcomes. Tools run on the device only.

Contract with the HTTP layer (protocol_router.make_protocol_router_for): start / protocol_snapshot / decide / cancel / modes.

Restart semantics (see recover()): nothing is replayed blindly. A task that never started its worker is started now; a task that
was running is closed with an explicit INTERRUPTED outcome, or NEEDS_RECONCILIATION if an approved effect may have been in
flight. Events written before the crash stay exactly as they were, so cursors keep their meaning.
"""
import os
import threading

from .device_client import DeviceUnavailable, RemoteToolbox
from .errors import Busy, LogUnavailable, NotFound, BadWorkspace
from .loop import Task
from .wire import map_event

UNKNOWN_STATUSES = ("UNKNOWN", "UNKNOWN_OUTCOME", "NEEDS_RECONCILIATION")

def failpoint(name):
    """Crash-test hook: die abruptly (no cleanup, no response) at a named point. Inert unless BOTH environment variables are set,
    which only the recovery tests do."""
    if os.environ.get("CODING_SERVICE_ALLOW_FAILPOINTS") == "1" and os.environ.get("CODING_SERVICE_FAILPOINT") == name:
        os._exit(137)

class DurableManager:
    def __init__(self, store, devices, hosts, model, allow_mutation=False, max_tasks=32):
        self.store, self.devices, self.hosts, self.model = store, devices, hosts, model
        self._allow, self._max = bool(allow_mutation), max_tasks
        self._live, self._threads, self._lock = {}, {}, threading.Lock()
        self._cancel_pending = set()

    @property
    def modes(self):
        return ["read", "propose", "apply", "test"] if self._allow else ["read", "propose"]

    @property
    def mutation_enabled(self):
        return self._allow

    # --- the engine contract ---
    def _target(self, principal, workspace_ref):
        host = self.hosts.get(principal.split(":", 1)[0], {})
        target = host.get("workspaces", {}).get(workspace_ref)
        if target is None or target["device"] not in self.devices:
            raise BadWorkspace()
        return target

    def start(self, principal, workspace_ref, key, instruction):
        target = self._target(principal, workspace_ref)
        with self._lock:
            if len(self._live) >= self._max and self.store.lookup(principal, key) is None:
                raise Busy()
            task_id, state, created = self.store.create_or_get(principal, workspace_ref, key, instruction)
            failpoint("after_accept")   # committed, not yet answered or started: the case recovery must close without loss or duplication
            if created:
                self._spawn(task_id, principal, target, instruction)
        return task_id, state

    def _own(self, principal, task_id):
        row = self.store.get(task_id) if isinstance(task_id, str) else None
        if row is None or row["principal"] != principal:
            raise NotFound()
        return row

    def protocol_snapshot(self, principal, task_id, after=0):
        self._own(principal, task_id)
        return self.store.snapshot(task_id, after)

    def decide(self, principal, task_id, action_id, fingerprint, approve):
        self._own(principal, task_id)
        task = self._live.get(task_id)
        return bool(task and task.decide(action_id, fingerprint, approve))   # no live worker (for example after a restart): nothing to decide

    def cancel(self, principal, task_id):
        row = self._own(principal, task_id)
        with self._lock:
            task = self._live.get(task_id)
            if task is None and task_id in self._live:
                self.store.append(task_id, "notice", {"code": "cancel_requested"})
                self._cancel_pending.add(task_id)
        if task is not None:
            task.cancel()
        return self.store.get(task_id)["state"] if row else None

    # --- workers ---
    def _sink(self, task_id):
        flags = {"unknown": False}
        def sink(_, event):
            if event.get("type") == "tool_result":
                result = (event.get("data") or {}).get("result")
                if isinstance(result, dict) and result.get("status") in UNKNOWN_STATUSES:
                    flags["unknown"] = True
            if event.get("type") == "state" and flags["unknown"] and (event.get("data") or {}).get("state") in {"COMPLETED", "FAILED", "CANCELLED"}:
                event = {**event, "data": {"state": "NEEDS_RECONCILIATION", "detail": "an action's outcome is unknown; inspect the workspace before relying on this result"}}
            for kind, data in map_event(event):
                self.store.append(task_id, kind, data)
        return sink

    def _spawn(self, task_id, principal, target, instruction):
        thread = threading.Thread(target=self._work, args=(task_id, principal, target, instruction), daemon=True, name=f"task-{task_id[:8]}")
        self._threads[task_id] = thread
        self._live[task_id] = None   # reserved: counted as active immediately
        thread.start()

    def _work(self, task_id, principal, target, instruction):
        toolbox = None
        try:
            self.store.append(task_id, "notice", {"code": "worker_started"})
            self.store.append(task_id, "state", {"state": "RUNNING"})
            toolbox = RemoteToolbox(self.devices[target["device"]], target["workspace"], task_id, allow_effects=self._allow)
            task = Task(principal, toolbox, self.model, instruction, event_sink=self._sink(task_id))
            task.id = task_id
            with self._lock:
                self._live[task_id] = task
                cancelled = task_id in self._cancel_pending
            if cancelled:
                task.cancel()
            task.run()
        except LogUnavailable:
            pass   # the store itself is failing: nothing can be recorded; recover() will close the task on the next start
        except (DeviceUnavailable, ValueError, OSError) as exc:
            self._fail(task_id, "device_unavailable" if isinstance(exc, DeviceUnavailable) else "workspace_refused")
        except Exception:
            self._fail(task_id, "internal_error")
        finally:
            if toolbox is not None:
                toolbox.close()
            with self._lock:
                self._live.pop(task_id, None)
                self._cancel_pending.discard(task_id)

    def _fail(self, task_id, detail):
        try:
            self.store.append(task_id, "state", {"state": "FAILED", "detail": detail})
        except LogUnavailable:
            pass

    # --- restart ---
    def recover(self):
        """Close or resume every task that was not terminal when the process last stopped. Returns a summary for the operator."""
        summary = {"resumed": [], "interrupted": [], "needs_reconciliation": []}
        for row in self.store.incomplete():
            events = self.store.all_events(row["id"])
            if not any(kind == "notice" and data.get("code") == "worker_started" for _, kind, data in events):
                if any(kind == "notice" and data.get("code") == "cancel_requested" for _, kind, data in events):
                    self.store.append(row["id"], "state", {"state": "CANCELLED", "detail": "cancellation was recorded before restart"})
                    summary["interrupted"].append(row["id"])
                    continue
                try:
                    target = self._target(row["principal"], row["workspace"])
                except BadWorkspace:
                    self.store.append(row["id"], "state", {"state": "FAILED", "detail": "workspace no longer configured"})
                    continue
                self._spawn(row["id"], row["principal"], target, row["instruction"])   # it never ran: starting it now loses nothing
                summary["resumed"].append(row["id"])
                continue
            in_flight, uncertain, cancelled = None, False, False
            for _, kind, data in events:
                if kind == "approval_decision" and data.get("approve"):
                    in_flight = data.get("action_id")
                elif kind == "tool_activity":
                    in_flight = None
                    uncertain = uncertain or data.get("status") in UNKNOWN_STATUSES
                elif kind == "notice" and data.get("code") == "cancel_requested":
                    cancelled = True
            if in_flight or uncertain:
                self.store.append(row["id"], "notice", {"code": "service_restarted_during_action", "action_id": in_flight})
                self.store.append(row["id"], "state", {"state": "NEEDS_RECONCILIATION", "detail": "the service stopped while an approved action was running; its outcome is unknown"})
                summary["needs_reconciliation"].append(row["id"])
            elif cancelled:
                self.store.append(row["id"], "state", {"state": "CANCELLED", "detail": "cancellation was recorded before restart"})
                summary["interrupted"].append(row["id"])
            else:
                self.store.append(row["id"], "notice", {"code": "service_restarted"})
                self.store.append(row["id"], "state", {"state": "FAILED", "detail": "interrupted by a service restart; send the request again as a new message"})
                summary["interrupted"].append(row["id"])
        return summary

    def shutdown(self, timeout=10):
        for task in list(self._live.values()):
            if task is not None:
                task.cancel()
        for thread in list(self._threads.values()):
            thread.join(timeout)
