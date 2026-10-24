"""The contract surface: protocol-shaped HTTP routes over the engine. Host-facing; no tool, file or process code here.

Everything this router returns validates against `protocol` (versioned envelopes, stable error codes, event cursor).
Mount it at its own prefix, e.g. `/code/v1`. The older `make_router` surface remains for the prototype demo only.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from protocol import PROTOCOL_VERSION, TERMINAL_STATES, ProtocolError, check_version, parse_cursor, validate_decision, validate_task_request
from .errors import BadWorkspace, Busy, KeyConflict, LogUnavailable, NotFound
from protocol.json_boundary import loads_object, read_limited_body

MAX_BODY_BYTES = 65536

def make_protocol_router_for(engine, authorize, resolve_workspace):
    """HTTP translation over any task manager that offers start / events / decide / cancel / modes / mutation_enabled
    (the in-process prototype Engine, or the durable coding-service manager). protocol_snapshot(principal, task_id, after) returns
    {events: [protocol envelopes with id > after], state, next_cursor: the id of the last event the task has}. The router cannot tell them apart, which is what
    lets one conformance suite hold both to the same contract.

    authorize(request) -> principal (raise HTTPException on failure); resolve_workspace(principal, workspace_id) -> the
    reference the manager needs (a local directory for the prototype, the workspace id itself for the service) or None."""
    router = APIRouter()

    def fail(error):
        return JSONResponse(error.envelope(), status_code=error.status, headers={"X-Protocol-Version": PROTOCOL_VERSION})

    def ok(body, status=200):
        return JSONResponse({"protocol": PROTOCOL_VERSION, **body}, status_code=status, headers={"X-Protocol-Version": PROTOCOL_VERSION})

    def authenticate(request):
        """Host authentication first (before any parsing). Its HTTPException becomes a protocol error."""
        try:
            principal = authorize(request)
        except HTTPException as exc:
            code = "UNAUTHENTICATED" if exc.status_code == 401 else "FORBIDDEN" if exc.status_code == 403 else "UNAVAILABLE"
            raise ProtocolError(code, "not permitted") from None
        check_version(request.headers.get("x-protocol-version"))
        return principal

    async def body_of(request):
        try:
            raw = await read_limited_body(request, MAX_BODY_BYTES)
            return loads_object(raw, max_bytes=MAX_BODY_BYTES)
        except OverflowError:
            raise ProtocolError("PAYLOAD_TOO_LARGE", "request body too large") from None
        except (ValueError, RecursionError):
            raise ProtocolError("BAD_REQUEST", "body is not valid JSON") from None

    def owned(principal, task_id, call):
        try:
            return call()
        except NotFound:
            raise ProtocolError("NOT_FOUND", "task not found") from None
        except LogUnavailable:
            raise ProtocolError("LOG_UNAVAILABLE", "activity log unavailable") from None

    @router.get("/availability")
    async def availability(request: Request):
        try:
            authenticate(request)
        except ProtocolError as exc:
            return fail(exc)
        return ok({"available": True, "modes": engine.modes, "mutation": engine.mutation_enabled})

    def start(principal, req):
        root = resolve_workspace(principal, req["workspace_id"])
        if root is None:
            raise ProtocolError("NOT_FOUND", "workspace not found")
        try:
            task_id, state = engine.start(principal, root, req["idempotency_key"], req["instruction"])
        except KeyConflict:
            raise ProtocolError("IDEMPOTENCY_CONFLICT", "idempotency key reused with different content", "idempotency_key") from None
        except Busy:
            raise ProtocolError("RATE_LIMITED", "too many tasks") from None
        except BadWorkspace:
            raise ProtocolError("NOT_FOUND", "workspace not found") from None
        except LogUnavailable:
            raise ProtocolError("LOG_UNAVAILABLE", "activity log unavailable") from None
        return {"task_id": task_id, "state": state}

    @router.post("/tasks")
    async def create(request: Request):
        try:
            principal = authenticate(request)
            req = validate_task_request(await body_of(request))
            return ok(await run_in_threadpool(start, principal, req), 202)
        except ProtocolError as exc:
            return fail(exc)

    @router.get("/tasks/{task_id}/events")
    async def events(task_id: str, request: Request):
        try:
            principal = authenticate(request)
            after = parse_cursor(request.query_params.get("after", "0"))
            snap = owned(principal, task_id, lambda: engine.protocol_snapshot(principal, task_id, after))
            return ok({"events": snap["events"], "state": snap["state"], "terminal": snap["state"] in TERMINAL_STATES, "next_cursor": snap["next_cursor"]})
        except ProtocolError as exc:
            return fail(exc)

    @router.post("/tasks/{task_id}/decision")
    async def decision(task_id: str, request: Request):
        try:
            principal = authenticate(request)
            req = validate_decision(await body_of(request))
            done = await run_in_threadpool(lambda: owned(principal, task_id, lambda: engine.decide(principal, task_id, req["action_id"], req["fingerprint"], req["approve"])))
            if not done:
                raise ProtocolError("NO_PENDING_ACTION", "no matching pending action")
            return ok({"accepted": True})
        except ProtocolError as exc:
            return fail(exc)

    @router.post("/tasks/{task_id}/cancel")
    async def cancel(task_id: str, request: Request):
        try:
            principal = authenticate(request)
            return ok({"state": await run_in_threadpool(lambda: owned(principal, task_id, lambda: engine.cancel(principal, task_id)))})
        except ProtocolError as exc:
            return fail(exc)

    router.engine = engine   # for tests and for hosts that need to join workers on shutdown
    return router
