"""Focused real-HTTP contract tests for the detached host mock."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from host_mock.server import handler_factory as mock_handler
from coding_agent.service import handler_factory as local_handler
from coding_agent.workspace import Workspace

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        (Path(self.temp.name) / "demo.txt").write_text("first\nsecond\n", encoding="utf-8")
        self.agent = ThreadingHTTPServer(("127.0.0.1", 0), local_handler(Workspace(self.temp.name), "agent-secret"))
        self.start_server(self.agent)
        self.host = ThreadingHTTPServer(("127.0.0.1", 0), mock_handler(
            f"http://127.0.0.1:{self.agent.server_port}", "agent-secret", "host-secret"))
        self.start_server(self.host)

    def start_server(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        if not hasattr(self, "threads"):
            self.threads = []
        self.threads.append(thread)

    def tearDown(self):
        for server in (self.host, self.agent):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)
        self.temp.cleanup()

    def call(self, route, token="host-secret", data=None):
        body = json.dumps(data if data is not None else {"path": "demo.txt"}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.host.server_port}{route}",
            data=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_read_through_wrapper(self):
        status, payload = self.call("/mock/read", data={"path": "demo.txt", "start_line": 2, "end_line": 2})
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["lines"], ["second"])
        self.assertEqual(len(payload["result"]["sha256"]), 64)

    def test_auth_and_rejected_path(self):
        self.assertEqual(self.call("/mock/list", token="bad")[0], 401)
        self.assertEqual(self.call("/mock/read", data={"path": "../secret"})[0], 502)

    def test_agent_unavailable(self):
        self.agent.shutdown()
        self.agent.server_close()
        self.assertEqual(self.call("/mock/read")[0], 503)

    def test_mock_not_exposing_task_api(self):
        self.assertEqual(self.call("/v1/tasks")[0], 404)

if __name__ == "__main__":
    unittest.main()
