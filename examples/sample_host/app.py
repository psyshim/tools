"""A stand-in for an existing chat product: its own login, its own workspace ACL, ordinary chat, plus the coding wrapper.

Everything below the 'HOST-SPECIFIC' markers is what a real host writes; the coding routes are mounted from `ROUTES` and
handled by `CodingEndpoints.dispatch`.

Run:  python -m examples.sample_host.app --service http://127.0.0.1:8799 --credential dev-credential
(with `python -m examples.mock_server` running, or the real coding service).
"""
import argparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from host_client import ROUTES, AgentGateway, CodingEndpoints, HostEnvironment

# --- HOST-SPECIFIC: the host's users and ACL (a real host reads its own session and database) ---
USERS = {"token-alice": "alice", "token-bob": "bob"}
ACL = {"alice": {"w1"}, "bob": {"w2"}}

class SampleHost(HostEnvironment):
    def can_access_workspace(self, user, workspace_id):
        return workspace_id in ACL.get(user, ())

def identity(request: Request):
    user = USERS.get(request.headers.get("authorization", "")[7:])
    if user is None:
        raise HTTPException(401, "login required")
    return user
# --- end HOST-SPECIFIC ---

def create_app(gateway, environment=None):
    app = FastAPI()
    endpoints = CodingEndpoints(gateway, environment or SampleHost())

    @app.post("/chat")                      # the host's ordinary chat: must never depend on the coding service
    async def chat(request: Request):
        user = identity(request)
        body = await request.json()
        return {"reply": f"echo for {user}: {str(body.get('message', ''))[:200]}"}

    for route in ROUTES:                    # the whole coding surface: one generic handler per route
        def make(route):
            async def handler(request: Request):
                user = identity(request)
                try:
                    body = await request.json() if route.method == "POST" else None
                except ValueError:
                    body = None
                path = route.path.format(**request.path_params)
                reply = endpoints.dispatch(route.method, path, user=user, query=dict(request.query_params), body=body)
                return JSONResponse(reply.body, status_code=reply.status)
            return handler
        app.add_api_route(route.path, make(route), methods=[route.method])
    return app

def main():
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", default="http://127.0.0.1:8799")
    parser.add_argument("--credential", default="dev-credential")
    parser.add_argument("--port", type=int, default=8800)
    args = parser.parse_args()
    uvicorn.run(create_app(AgentGateway(args.service, args.credential)), host="127.0.0.1", port=args.port)

if __name__ == "__main__":
    main()
