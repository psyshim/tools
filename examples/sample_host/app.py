"""A stand-in for an existing chat product: its own login, its own workspace ACL, ordinary chat, plus the thin coding adapter.

Run:  python -m examples.sample_host.app --service http://127.0.0.1:8799 --credential dev-credential
(with `python -m examples.mock_server` running, or the real coding service).
"""
import argparse

from fastapi import FastAPI, HTTPException, Request

from host_client import AgentGateway
from .adapter import CodingAdapter

USERS = {"token-alice": "alice", "token-bob": "bob"}
ACL = {"alice": {"w1"}, "bob": {"w2"}}

def create_app(gateway):
    app = FastAPI()
    adapter = CodingAdapter(gateway, lambda user, workspace: workspace in ACL.get(user, ()))

    def identity(request: Request):
        user = USERS.get(request.headers.get("authorization", "")[7:])
        if user is None:
            raise HTTPException(401, "login required")
        return user

    @app.post("/chat")                      # the host's ordinary chat: must never depend on the coding service
    async def chat(request: Request):
        user = identity(request)
        body = await request.json()
        return {"reply": f"echo for {user}: {str(body.get('message', ''))[:200]}"}

    @app.get("/coding/availability")
    async def availability(request: Request):
        identity(request)
        return adapter.availability()

    @app.post("/coding/start")
    async def start(request: Request):
        user, body = identity(request), await request.json()
        return adapter.start(user, str(body.get("workspace_id", "")), str(body.get("text", "")), str(body.get("conversation_id", "")), str(body.get("message_id", "")))

    @app.get("/coding/{handle}/events")
    async def events(handle: str, request: Request, after: str = "0"):
        try:
            return adapter.poll(identity(request), handle, after)
        except ValueError:
            return {"ok": False, "error": "bad_request"}

    @app.post("/coding/{handle}/decision")
    async def decision(handle: str, request: Request):
        user, body = identity(request), await request.json()
        return adapter.decide(user, handle, str(body.get("action_id", "")), str(body.get("fingerprint", "")), bool(body.get("approve")))

    @app.post("/coding/{handle}/cancel")
    async def cancel(handle: str, request: Request):
        return adapter.cancel(identity(request), handle)

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
