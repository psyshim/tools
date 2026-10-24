"""HTTP application: the protocol router over the durable manager, with host authentication.

Authentication: `Authorization: Bearer <host credential>` identifies the HOST (matched by hash against the configuration).
The host vouches for its end user in `X-Principal` (percent-encoded); the service namespaces it as `<host_id>:<user>`, so two hosts
can never collide or reach each other's tasks. Only /availability may be called without a user.
"""
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException

from .manager import DurableManager
from .models import load_model
from .protocol_router import make_protocol_router_for
from .store import TaskStore
from .device_client import DeviceClient

def build_manager(config, env, sleep=None):
    store = TaskStore(config.db_path)
    devices = {did: DeviceClient(spec["url"], spec["token"]) for did, spec in config.devices.items()}
    return DurableManager(store, devices, config.hosts, load_model(config.model, env), allow_mutation=config.allow_mutation, max_tasks=config.max_tasks)

def create_app(config, manager):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager

    def authorize(request):
        supplied = request.headers.get("authorization", "")
        host_id = config.host_for_credential(supplied[7:]) if supplied.startswith("Bearer ") else None
        if host_id is None:
            raise HTTPException(401, "unauthorized")
        user = unquote(request.headers.get("x-principal", ""))[:256]
        if not user or any(ord(c) < 32 for c in user):
            if request.url.path.rstrip("/").endswith("/availability"):
                return f"{host_id}:"
            raise HTTPException(401, "a user identity is required")
        return f"{host_id}:{user}"

    def resolve_workspace(principal, workspace_id):
        host = config.hosts.get(principal.split(":", 1)[0], {})
        return workspace_id if workspace_id in host.get("workspaces", {}) else None

    app.include_router(make_protocol_router_for(manager, authorize, resolve_workspace))
    return app
