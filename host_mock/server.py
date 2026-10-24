"""Standalone, low-resource mock host backend.

Development-only: loopback, explicit shared tokens, no chat DB or model.
"""
import argparse
import hmac
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

def handler_factory(agent_url, agent_token, host_token):
    if not agent_token or not host_token:
        raise ValueError("both tokens are required")
    parsed = urlsplit(agent_url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.username or parsed.password:
        raise ValueError("mock accepts a loopback agent URL only")
    agent_base = agent_url.rstrip("/")
    routes = {"/mock/list": "/v1/workspace/list", "/mock/read": "/v1/workspace/read"}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + host_token):
                return self._send(401, {"error": "unauthorized"})
            route = self.path
            if route not in routes:
                return self._send(404, {"error": "not_found"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > 8192:
                    return self._send(413, {"error": "invalid_size"})
                raw = self.rfile.read(size)
                obj = json.loads(raw)
                if not isinstance(obj, dict):
                    raise ValueError("JSON object required")
                request = urllib.request.Request(
                    agent_base + routes[route],
                    data=json.dumps(obj).encode("utf-8"),
                    headers={"Authorization": "Bearer " + agent_token, "Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(request, timeout=3) as response:
                        result = json.load(response)
                        return self._send(response.status, result)
                except urllib.error.HTTPError as exc:
                    return self._send(502, {"error": "agent_rejected", "status": exc.code})
                except (urllib.error.URLError, TimeoutError, OSError):
                    return self._send(503, {"error": "agent_unavailable"})
            except (ValueError, TypeError, UnicodeError):
                return self._send(400, {"error": "invalid_request"})

        def _send(self, status, obj):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass
    return Handler

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-url", default="http://127.0.0.1:8765")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    handler = handler_factory(args.agent_url, os.environ.get("CODING_AGENT_TOKEN"), os.environ.get("MOCK_HOST_TOKEN"))
    ThreadingHTTPServer(("127.0.0.1", args.port), handler).serve_forever()

if __name__ == "__main__":
    main()
