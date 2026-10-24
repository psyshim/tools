"""HTTP face of the device: token-authenticated, loopback-only by default, typed bodies, no path or command ever accepted."""
import hashlib
import hmac

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .core import DeviceError, DeviceService
from protocol.json_boundary import loads_object, read_limited_body

MAX_BODY = 1024 * 1024
LOOPBACK = {"127.0.0.1", "::1", "localhost"}

def create_app(config, service=None, trust_test_client=False):
    service = service or DeviceService(config)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service

    def guard(request):
        host = request.client.host if request.client else ""
        if not config.allow_remote_clients and host not in LOOPBACK and not (trust_test_client and host == "testclient"):
            raise HTTPException(403, "this device accepts local connections only")
        supplied = request.headers.get("authorization", "")
        token = supplied[7:] if supplied.startswith("Bearer ") else ""
        if not hmac.compare_digest(hashlib.sha256(token.encode("utf-8")).hexdigest(), config.token_sha256):
            raise HTTPException(401, "unauthorized")

    async def read_body(request):
        try:
            raw = await read_limited_body(request, MAX_BODY)
            data = loads_object(raw, max_bytes=MAX_BODY)
        except OverflowError:
            raise DeviceError(413, "payload_too_large", "request body too large") from None
        except (ValueError, RecursionError):
            raise DeviceError(400, "bad_request", "body is not valid JSON") from None
        if not isinstance(data, dict):
            raise DeviceError(400, "bad_request", "body must be a JSON object")
        return data

    async def respond(request, produce, with_body=False):
        """Authenticate first, parse second, run the blocking work off the event loop, translate refusals to typed errors."""
        guard(request)
        try:
            data = await read_body(request) if with_body else None
            return JSONResponse(await run_in_threadpool(produce, data))
        except DeviceError as exc:
            return JSONResponse({"error": {"type": "DeviceError", "code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.get("/v1/info")
    async def info(request: Request):
        return await respond(request, lambda _: service.info())

    @app.post("/v1/sessions")
    async def open_session(request: Request):
        return await respond(request, lambda d: service.open_session(d.get("workspace_id"), d.get("session_id")), with_body=True)

    @app.get("/v1/sessions/{session_id}/schemas")
    async def schemas(session_id: str, request: Request):
        return await respond(request, lambda _: service.schemas(session_id))

    @app.post("/v1/sessions/{session_id}/call")
    async def call(session_id: str, request: Request):
        return await respond(request, lambda d: service.call(session_id, d.get("tool"), d.get("args", {}), d.get("op_id")), with_body=True)

    @app.post("/v1/sessions/{session_id}/describe")
    async def describe(session_id: str, request: Request):
        return await respond(request, lambda d: service.describe_effect(session_id, d.get("tool"), d.get("args", {})), with_body=True)

    @app.post("/v1/sessions/{session_id}/cancel")
    async def cancel(session_id: str, request: Request):
        return await respond(request, lambda _: service.cancel(session_id))

    @app.delete("/v1/sessions/{session_id}")
    async def close(session_id: str, request: Request):
        return await respond(request, lambda _: service.close_session(session_id))

    @app.get("/v1/operations/{op_id}")
    async def operation(op_id: str, request: Request):
        return await respond(request, lambda _: service.operation(op_id))

    return app
