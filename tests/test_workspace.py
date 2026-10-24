import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from coding_agent.workspace import Workspace
from coding_agent.service import handler_factory

class AgentTests(unittest.TestCase):
    def test_reads_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "demo.txt").write_text("a\nb\nc\n", encoding="utf-8")
            ws = Workspace(root)
            self.assertEqual(ws.list_directory(), ["demo.txt"])
            self.assertEqual(ws.read_file("demo.txt", 2, 3)["lines"], ["b", "c"])
            for path in ("../nonexistent", "/etc/passwd"):
                with self.assertRaises(ValueError):
                    ws.read_file(path)
            with self.assertRaises(ValueError):
                ws.read_file("demo.txt", 1, 202)

    def test_symlink_outside(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as external:
            outside = Path(external) / "secret"
            outside.write_text("secret")
            (Path(directory) / "link").symlink_to(outside)
            ws = Workspace(directory)
            self.assertEqual(ws.list_directory(), [])
            with self.assertRaises(ValueError):
                ws.read_file("link")

    def test_http_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "a.txt").write_text("hello")
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(Workspace(directory), "test-token"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                uri = f"http://127.0.0.1:{server.server_port}/v1/workspace/read"
                def request(with_auth):
                    headers = {"Content-Type": "application/json"}
                    if with_auth:
                        headers["Authorization"] = "Bearer test-token"
                    return urllib.request.Request(uri, data=b'{"path":"a.txt"}', headers=headers)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request(False), timeout=2)
                self.assertEqual(caught.exception.code, 401)
                with urllib.request.urlopen(request(True), timeout=2) as response:
                    self.assertEqual(json.load(response)["result"]["lines"], ["hello"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

if __name__ == "__main__":
    unittest.main()
