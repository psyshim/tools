"""FastAPI router: the only thing the host wires. Host supplies identity, workspace ACL and the model callback."""
import threading
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from .session import Task, TERMINAL
from .tools import ToolBox

class NewTask(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=128)
    instruction: str = Field(min_length=1, max_length=16384)
    idempotency_key: str = Field(min_length=1, max_length=128)

class Decision(BaseModel):
    action_id: str
    fingerprint: str
    approve: bool

def make_router(complete, authorize, resolve_workspace, test_commands=None, max_tasks=64):
    """complete(messages, tools) -> {"text", "tool_calls"}; authorize(request) -> principal (raise HTTPException on failure);
    resolve_workspace(principal, workspace_id) -> local directory path or None; test_commands: {name: argv list}."""
    router = APIRouter()
    tasks, by_key, request_of, lock = {}, {}, {}, threading.Lock()

    def owned(request, task_id):
        principal = authorize(request)
        task = tasks.get(task_id)
        if task is None or task.principal != principal:
            raise HTTPException(404, "not found")
        return task

    @router.get("/availability")
    def availability(request: Request):
        authorize(request)
        return {"available": True, "modes": ["read", "propose", "apply"] + (["test"] if test_commands else [])}

    @router.post("/tasks", status_code=202)
    def create(body: NewTask, request: Request):
        principal = authorize(request)
        key = (principal, body.idempotency_key)
        content = (body.workspace_id, body.instruction)
        with lock:
            if key in by_key:
                task = tasks[by_key[key]]
                if request_of[task.id] != content:
                    raise HTTPException(409, "idempotency key reused with different content")
                return {"task_id": task.id, "state": task.state}
            root = resolve_workspace(principal, body.workspace_id)
            if root is None:
                raise HTTPException(404, "workspace not found")
            if len(tasks) >= max_tasks:
                raise HTTPException(429, "too many tasks")
            task = Task(principal, ToolBox(root, test_commands), complete, body.instruction)
            tasks[task.id], by_key[key], request_of[task.id] = task, task.id, content
        task.start()
        return {"task_id": task.id, "state": task.state}

    @router.get("/tasks/{task_id}/events")
    def events(task_id: str, request: Request, after: int = 0):
        task = owned(request, task_id)
        evs, state = task.events_after(max(after, 0))
        return {"events": evs, "state": state, "terminal": state in TERMINAL}

    @router.post("/tasks/{task_id}/decision")
    def decision(task_id: str, body: Decision, request: Request):
        task = owned(request, task_id)
        if not task.decide(body.action_id, body.fingerprint, body.approve):
            raise HTTPException(409, "no matching pending action")
        return {"accepted": True}

    @router.post("/tasks/{task_id}/cancel")
    def cancel(task_id: str, request: Request):
        task = owned(request, task_id)
        task.cancel()
        return {"state": task.state}

    return router
