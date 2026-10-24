"""Agent-side facade. Owns task registry, idempotency and ownership. Knows nothing about HTTP or the host's auth."""
import hashlib
import threading
from coding_service.wire import to_protocol_events
from coding_service.errors import BadWorkspace, Busy, KeyConflict, LogUnavailable, NotFound  # noqa: F401  (re-exported)
from .session import Task, TERMINAL
from .testrun import normalize_commands
from .tools import ToolBox

def _ref(value):
    """Opaque stable reference; raw paths and user ids must not appear in events or logs."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]

class Engine:
    """Task registry for one host: start/observe/decide/cancel with per-principal ownership, idempotency keys,
    a cap on ACTIVE tasks and bounded retention of finished ones. Knows nothing about HTTP or authentication."""
    def __init__(self, complete, test_commands=None, max_tasks=64, test_timeout=180, event_sink=None, test_env=None, keep_finished=256, recycle_dir=None, allow_mutation=True):
        self._complete = complete
        self._tests = normalize_commands(test_commands)
        self._max = max_tasks
        self._timeout = test_timeout
        self._test_env = test_env
        self._recycle_dir = recycle_dir
        self._allow_mutation = bool(allow_mutation)
        self._keep = keep_finished
        self._event_sink = event_sink
        self._tasks, self._keys, self._content = {}, {}, {}
        self._lock = threading.Lock()

    @property
    def modes(self):
        if not self._allow_mutation:
            return ["read", "propose"]
        return ["read", "propose", "apply"] + (["test"] if self._tests else [])

    @property
    def mutation_enabled(self):
        return self._allow_mutation

    def start(self, principal, root, idempotency_key, instruction):
        """root is a directory the host already authorized for principal. Returns (task_id, state)."""
        key, content = (principal, idempotency_key), (str(root), instruction)
        with self._lock:
            if key in self._keys:
                task = self._tasks[self._keys[key]]
                if self._content[task.id] != content:
                    raise KeyConflict()
                return task.id, task.state
            self._evict_finished()
            if sum(t.state not in TERMINAL for t in self._tasks.values()) >= self._max:
                raise Busy()
            try:
                toolbox = ToolBox(root, self._tests, self._timeout, self._test_env, recycle_dir=self._recycle_dir, allow_mutation=self._allow_mutation)
            except (OSError, ValueError):
                raise BadWorkspace()
            task = Task(principal, toolbox, self._complete, instruction, event_sink=self._event_sink)
            self._tasks[task.id], self._keys[key], self._content[task.id] = task, task.id, content
        try:  # sink I/O happens outside the registry lock
            task.emit("task_started", {"instruction": instruction, "workspace_ref": _ref(root), "principal_ref": _ref(principal)}, private=True)
        except LogUnavailable:
            with self._lock:
                self._tasks.pop(task.id, None)
                self._content.pop(task.id, None)
                self._keys.pop(key, None)
            raise
        task.start()
        return task.id, task.state

    def _evict_finished(self):
        """Keep memory bounded: drop the oldest finished tasks (their idempotency keys go with them)."""
        finished = [tid for tid, t in self._tasks.items() if t.state in TERMINAL]
        for tid in finished[:max(len(finished) - self._keep, 0)]:
            del self._tasks[tid], self._content[tid]
            for key in [k for k, v in self._keys.items() if v == tid]:
                del self._keys[key]

    def _own(self, principal, task_id):
        task = self._tasks.get(task_id)
        if task is None or task.principal != principal:
            raise NotFound()
        return task

    def events(self, principal, task_id, after=0):
        events, state = self._own(principal, task_id).events_after(max(after, 0))
        return {"events": events, "state": state, "terminal": state in TERMINAL}

    def protocol_snapshot(self, principal, task_id, after=0):
        """Events as protocol envelopes (stable ids, see coding_service.wire) for the contract router."""
        snapshot = self.events(principal, task_id, 0)
        mapped = to_protocol_events(task_id, snapshot["events"])
        return {"events": [e for e in mapped if int(e["event_id"]) > after], "state": snapshot["state"], "next_cursor": len(mapped)}

    def decide(self, principal, task_id, action_id, fingerprint, approve):
        return self._own(principal, task_id).decide(action_id, fingerprint, approve)

    def cancel(self, principal, task_id):
        task = self._own(principal, task_id)
        task.cancel()
        return task.state

    def join(self, principal, task_id, timeout=10):
        """Wait for the task's worker to stop (after cancel or completion). Returns True if stopped."""
        thread = self._own(principal, task_id).thread
        thread.join(timeout)
        return not thread.is_alive()
