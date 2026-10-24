"""HTTP translation only. Host supplies identity, workspace ACL and the model callback; no tool or execution code here."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from .engine import Busy, Engine, KeyConflict, NotFound

class NewTask(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=128)
    instruction: str = Field(min_length=1, max_length=16384)
    idempotency_key: str = Field(min_length=1, max_length=128)

class Decision(BaseModel):
    action_id: str
    fingerprint: str
    approve: bool

def make_router(complete, authorize, resolve_workspace, test_commands=None, max_tasks=64, test_timeout=180, event_sink=None):
    """complete(messages, tools) -> {"text", "tool_calls"}; authorize(request) -> principal (raise HTTPException on failure);
    resolve_workspace(principal, workspace_id) -> local directory path or None; test_commands: {name: argv list}."""
    engine = Engine(complete, test_commands, max_tasks, test_timeout, event_sink)
    router = APIRouter()

    def guarded(fn, *args):
        try:
            return fn(*args)
        except NotFound:
            raise HTTPException(404, "not found")
        except OSError:
            raise HTTPException(503, "activity log unavailable")

    @router.get("/availability")
    def availability(request: Request):
        authorize(request)
        return {"available": True, "modes": engine.modes}

    @router.post("/tasks", status_code=202)
    def create(body: NewTask, request: Request):
        principal = authorize(request)
        root = resolve_workspace(principal, body.workspace_id)
        if root is None:
            raise HTTPException(404, "workspace not found")
        try:
            task_id, state = engine.start(principal, root, body.idempotency_key, body.instruction)
        except KeyConflict:
            raise HTTPException(409, "idempotency key reused with different content")
        except Busy:
            raise HTTPException(429, "too many tasks")
        except OSError:
            raise HTTPException(503, "activity log unavailable")
        return {"task_id": task_id, "state": state}

    @router.get("/tasks/{task_id}/events")
    def events(task_id: str, request: Request, after: int = 0):
        return guarded(engine.events, authorize(request), task_id, after)

    @router.post("/tasks/{task_id}/decision")
    def decision(task_id: str, body: Decision, request: Request):
        if not guarded(engine.decide, authorize(request), task_id, body.action_id, body.fingerprint, body.approve):
            raise HTTPException(409, "no matching pending action")
        return {"accepted": True}

    @router.post("/tasks/{task_id}/cancel")
    def cancel(task_id: str, request: Request):
        return {"state": guarded(engine.cancel, authorize(request), task_id)}

    return router
