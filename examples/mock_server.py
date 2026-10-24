"""Run the executable mock coding service over HTTP, so a host can be developed with no agent, no model and no files.

    python -m examples.mock_server --port 8799 --credential dev-credential --allow alice:w1 --allow bob:w2

Point host_client.AgentGateway("http://127.0.0.1:8799", "dev-credential") at it. The mock follows the same contract as the
real service (protocol/, tests/test_protocol_conformance.py) and never changes anything on disk.
Instructions starting with "propose" yield a proposal, "approve" yields an approval card, anything else a plain answer.
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from protocol import error_envelope
from testkit.mock import MockCodingService

MAX_BODY = 65536

def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        def _serve(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, error_envelope("PAYLOAD_TOO_LARGE", "request body too large"))
            raw = self.rfile.read(length) if length else b""
            body = None
            if raw:
                try:
                    body = json.loads(raw)
                except ValueError:
                    return self._send(400, error_envelope("BAD_REQUEST", "body is not valid JSON"))
            status, payload = service.handle(method, self.path, dict(self.headers), body)
            self._send(status, payload)

        def _send(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

        def log_message(self, *args):
            pass
    return Handler

def serve(service, host="127.0.0.1", port=0):
    """Return a started-but-not-yet-serving server; call .serve_forever() (usually in a thread) and .shutdown() to stop."""
    return ThreadingHTTPServer((host, port), make_handler(service))

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--credential", default="dev-credential")
    parser.add_argument("--allow", action="append", default=[], metavar="USER:WORKSPACE", help="workspace a user may use (repeatable)")
    args = parser.parse_args()
    grants = {}
    for item in args.allow or ["alice:w1"]:
        user, _, workspace = item.partition(":")
        grants.setdefault("host:" + user, set()).add(workspace)
    server = serve(MockCodingService({}, grants, host_credentials={args.credential}), port=args.port)
    print(f"mock coding service on http://127.0.0.1:{args.port} (credential {args.credential!r}, users {sorted(g[5:] for g in grants)})", flush=True)
    server.serve_forever()

if __name__ == "__main__":
    main()
