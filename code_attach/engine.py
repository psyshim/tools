"""Agent-side facade. Owns task registry, idempotency and ownership. Knows nothing about HTTP or the host's auth."""
import threading
from .session import Task, TERMINAL
from .tools import ToolBox

class NotFound(Exception):
    pass

class KeyConflict(Exception):
    pass

class Busy(Exception):
    pass

class Engine:
    def __init__(self, complete, test_commands=None, max_tasks=64, test_timeout=180):
        self._complete = complete
        self._tests = dict(test_commands or {})
        self._max = max_tasks
        self._timeout = test_timeout
        self._tasks, self._keys, self._content = {}, {}, {}
        self._lock = threading.Lock()

    @property
    def modes(self):
        return ["read", "propose", "apply"] + (["test"] if self._tests else [])

    def start(self, principal, root, idempotency_key, instruction):
        """root is a directory the host already authorized for principal. Returns (task_id, state)."""
        key, content = (principal, idempotency_key), (str(root), instruction)
        with self._lock:
            if key in self._keys:
                task = self._tasks[self._keys[key]]
                if self._content[task.id] != content:
                    raise KeyConflict()
                return task.id, task.state
            if len(self._tasks) >= self._max:
                raise Busy()
            task = Task(principal, ToolBox(root, self._tests, self._timeout), self._complete, instruction)
            self._tasks[task.id], self._keys[key], self._content[task.id] = task, task.id, content
        task.start()
        return task.id, task.state

    def _own(self, principal, task_id):
        task = self._tasks.get(task_id)
        if task is None or task.principal != principal:
            raise NotFound()
        return task

    def events(self, principal, task_id, after=0):
        events, state = self._own(principal, task_id).events_after(max(after, 0))
        return {"events": events, "state": state, "terminal": state in TERMINAL}

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
