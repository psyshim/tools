"""Loopback-only read API."""
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from .workspace import Workspace

def handler_factory(workspace, token):
    if not token:
        raise ValueError("token required")
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                return self._send(401, {"error": "unauthorized"})
            route = urlparse(self.path).path
            if route not in {"/v1/workspace/list", "/v1/workspace/read"}:
                return self._send(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    return self._send(413, {"error": "invalid request size"})
                args = json.loads(self.rfile.read(length))
                if not isinstance(args, dict):
                    raise ValueError("expected JSON object")
                if route.endswith("/list"):
                    result = workspace.list_directory(args.get("path", "."), args.get("depth", 1))
                else:
                    result = workspace.read_file(args["path"], args.get("start_line", 1), args.get("end_line", 200))
                self._send(200, {"result": result})
            except (ValueError, KeyError, TypeError, UnicodeError, OSError) as exc:
                self._send(400, {"error": type(exc).__name__})
        def _send(self, status, data):
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args):
            pass
    return Handler

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_factory(Workspace(args.workspace), os.environ.get("CODING_AGENT_TOKEN", "")))
    server.serve_forever()

if __name__ == "__main__":
    main()
