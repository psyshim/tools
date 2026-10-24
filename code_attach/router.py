"""HTTP translation only. Host supplies identity, workspace ACL and the model callback; no tool or execution code here."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from .engine import BadWorkspace, Busy, Engine, KeyConflict, LogUnavailable, NotFound
from protocol.json_boundary import loads_object, read_limited_body

MAX_BODY_BYTES = 65536

class NewTask(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=128)
    instruction: str = Field(min_length=1, max_length=16384)
    idempotency_key: str = Field(min_length=1, max_length=128)

class Decision(BaseModel):
    action_id: str = Field(max_length=256)
    fingerprint: str = Field(max_length=256)
    approve: bool

async def read_model(request, model):
    """Parse and validate a JSON body ourselves: bounded size, and error responses never echo the input back
    (FastAPI's default 422 echoes it and crashes on lone surrogates, turning bad input into a 500)."""
    try:
        raw = await read_limited_body(request, MAX_BODY_BYTES)
        data = loads_object(raw, max_bytes=MAX_BODY_BYTES)
    except OverflowError:
        raise HTTPException(413, "request body too large") from None
    except (ValueError, RecursionError):
        raise HTTPException(400, "body is not valid JSON")
    try:
        return model.model_validate(data, strict=True)
    except ValidationError as exc:
        raise HTTPException(422, [{"field": ".".join(str(p) for p in e["loc"]), "problem": e["type"]} for e in exc.errors()])

def make_router(complete, authorize, resolve_workspace, test_commands=None, max_tasks=64, test_timeout=180, event_sink=None, test_env=None, recycle_dir=None, allow_mutation=False, *, prototype=False):
    """complete(messages, tools) -> {"text", "tool_calls"}; authorize(request) -> principal (raise HTTPException on failure);
    resolve_workspace(principal, workspace_id) -> local directory path or None; test_commands: {name: argv list}.
    allow_mutation=False (the default) is read/propose mode: the assistant returns patch proposals and can neither apply
    them nor run commands. Enabling it is an explicit, local-experimental decision (see docs/UPSTREAM_MIGRATION_PLAYBOOK.md)."""
    if not prototype:
        raise RuntimeError("this runs the tools inside the host process and is a local experiment only; production hosts use host_client "
                           "against the coding service (docs/HOST_KIT.md). Pass prototype=True to acknowledge that.")
    engine = Engine(complete, test_commands, max_tasks, test_timeout, event_sink, test_env, recycle_dir=recycle_dir, allow_mutation=allow_mutation)
    router = APIRouter()

    def current_principal(request: Request):
        return authorize(request)  # a dependency: runs before body parsing, so unauthenticated callers always get the host's 401

    def guarded(fn, *args):
        try:
            return fn(*args)
        except NotFound:
            raise HTTPException(404, "not found")
        except LogUnavailable:
            raise HTTPException(503, "activity log unavailable")

    @router.get("/availability")
    def availability(principal=Depends(current_principal)):
        return {"available": True, "modes": engine.modes, "mutation": engine.mutation_enabled}

    def start(principal, body):
        root = resolve_workspace(principal, body.workspace_id)
        if root is None:
            raise HTTPException(404, "workspace not found")
        try:
            task_id, state = engine.start(principal, root, body.idempotency_key, body.instruction)
        except KeyConflict:
            raise HTTPException(409, "idempotency key reused with different content")
        except Busy:
            raise HTTPException(429, "too many tasks")
        except BadWorkspace:
            raise HTTPException(404, "workspace not found")
        except LogUnavailable:
            raise HTTPException(503, "activity log unavailable")
        return {"task_id": task_id, "state": state}

    @router.post("/tasks", status_code=202)
    async def create(request: Request, principal=Depends(current_principal)):
        body = await read_model(request, NewTask)
        return await run_in_threadpool(start, principal, body)

    @router.get("/tasks/{task_id}/events")
    def events(task_id: str, after: int = 0, principal=Depends(current_principal)):
        return guarded(engine.events, principal, task_id, after)

    @router.post("/tasks/{task_id}/decision")
    async def decision(task_id: str, request: Request, principal=Depends(current_principal)):
        body = await read_model(request, Decision)
        if not await run_in_threadpool(guarded, engine.decide, principal, task_id, body.action_id, body.fingerprint, body.approve):
            raise HTTPException(409, "no matching pending action")
        return {"accepted": True}

    @router.post("/tasks/{task_id}/cancel")
    def cancel(task_id: str, principal=Depends(current_principal)):
        return {"state": guarded(engine.cancel, principal, task_id)}

    return router
