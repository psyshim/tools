"""Runnable local host for code_attach: python -m examples.local_app --workspace DIR.

The host owns model access, authentication, and workspace registration. The reusable
code_attach package remains independent of this example and its network client.
"""
import argparse
import hmac
import json
import os
from pathlib import Path
import secrets
import sys
import urllib.request

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from code_attach import make_router
from code_attach.providers import from_openai, to_openai
from code_attach.activity import ActivityLog, default_log_dir, display_path


def model_callback(model, api_key, base_url):
    def complete(messages, tools):
        payload = {"model": model, **to_openai(messages, tools)}
        request = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions",
            json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            return from_openai(json.load(response)["choices"][0]["message"])
    return complete


def demo_callback(messages, tools):
    """Explicit scripted demo; each task derives its state from its own history."""
    results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
    step = len(results)
    if results and results[-1].get("status") in {"NOT_APPROVED", "ERROR", "CONFLICT"}:
        return {"text": "デモを終了しました: " + results[-1]["status"]}
    if step == 0:
        name, args = "search_text", {"query": "def add", "glob": "calc.py"}
    elif step == 1:
        matches = results[-1].get("matches", [])
        if not matches:
            return {"text": "デモを終了しました: add が見つかりません"}
        name, args = "read_file", {"path": "calc.py", "start_line": matches[0]["line"]}
    elif step == 2:
        name, args = "propose_edit", {"path": "calc.py", "old": "return a - b", "new": "return a + b"}
    elif step == 3:
        name, args = "apply_proposal", {"proposal_id": results[-1]["proposal_id"]}
    elif step == 4:
        name, args = "run_tests", {"name": "unit"}
    else:
        return {"text": "デモのテスト結果: " + results[-1].get("status", "UNKNOWN")}
    return {"text": "", "tool_calls": [{"id": f"demo-{step}", "name": name, "args": args}]}


def create_app(workspace, complete, token, test_commands=None, demo=False, log_dir=None):
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir() or not token:
        raise ValueError("a workspace directory and nonempty token are required")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    activity = ActivityLog(log_dir if log_dir is not None else default_log_dir(root))
    app.state.log_dir = activity.directory

    def authorize(request):
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
            raise HTTPException(401, "unauthorized")
        return "local"

    @app.middleware("http")
    async def local_boundary(request, call_next):
        # An arbitrary website must not be able to issue authenticated local requests.
        host = request.headers.get("host", "").split(":")[0]
        if host not in {"127.0.0.1", "localhost", "testserver"}:
            return HTMLResponse("Invalid host", status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != f"http://{request.headers['host']}":
            return HTMLResponse("Invalid origin", status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (Path(__file__).parent / "local_ui" / "index.html").read_text(encoding="utf-8")

    @app.get("/app.js")
    def script():
        from fastapi.responses import Response
        return Response((Path(__file__).parent / "local_ui" / "app.js").read_text(encoding="utf-8"), media_type="text/javascript")

    @app.get("/app.css")
    def style():
        from fastapi.responses import Response
        return Response((Path(__file__).parent / "local_ui" / "app.css").read_text(encoding="utf-8"), media_type="text/css")

    @app.get("/api/config")
    def config(request: Request):
        authorize(request)
        return {"workspace": root.name, "workspace_id": "project", "demo": demo,
                "log_dir": display_path(activity.directory)}

    app.include_router(make_router(complete, authorize,
                                  lambda principal, wid: str(root) if wid == "project" else None,
                                  test_commands, event_sink=activity.record), prefix="/api/code")
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="project directory (new directory for --demo)")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--demo", action="store_true", help="scripted edit/test demonstration, no model call")
    parser.add_argument("--demo-lines", type=int, default=0, help="add up to 175000 filler lines to the generated demo script")
    parser.add_argument("--log-dir", help="persistent task log directory (default: per-user, outside the project)")
    parser.add_argument("--test-command", nargs=argparse.REMAINDER, help="fixed argv; put this option last")
    args = parser.parse_args()
    if not 0 <= args.demo_lines <= 175000 or (args.demo_lines and not args.demo):
        parser.error("--demo-lines requires --demo and a value from 0 to 175000")
    root = Path(args.workspace).resolve()
    commands = {"unit": args.test_command} if args.test_command else None
    if args.demo:
        # Never seed or overwrite an existing project, even when it is empty.
        root.mkdir(parents=True, exist_ok=False)
        prefix = "".join(f"# legacy section {n:06d}\n" for n in range(args.demo_lines))
        (root / "calc.py").write_text(prefix + "def add(a, b):\n    return a - b\n", encoding="utf-8", newline="")
        (root / "test_calc.py").write_text(
            "import unittest\nfrom calc import add\nclass AddTest(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n"
            "if __name__ == '__main__':\n    unittest.main()\n", encoding="utf-8")
        complete = demo_callback
        commands = {"unit": [sys.executable, "test_calc.py"]}
    else:
        if not root.is_dir():
            parser.error("--workspace must be an existing directory")
        model, key = os.getenv("MODEL"), os.getenv("OPENAI_API_KEY")
        if not model or not key:
            parser.error("set MODEL and OPENAI_API_KEY, or use --demo with a new directory")
        complete = model_callback(model, key, os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    token = secrets.token_urlsafe(32)
    app = create_app(root, complete, token, commands, args.demo, args.log_dir)
    print(f"Task logs: {display_path(app.state.log_dir)}", flush=True)
    print(f"Open http://127.0.0.1:{args.port}/#{token}", flush=True)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
