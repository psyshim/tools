"""Mock enforces identity from separate authenticated tokens, never request JSON."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from coding_agent.fault_service import handler_factory

class IdentityBoundaryTests(unittest.TestCase):
    def test_cross_actor_task_hidden_and_keys_scoped(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(
            "first", "control", actor_tokens={"first":"alice", "second":"bob"},
            workspace_grants={"alice":["test-ws"], "bob":["other-ws"]}))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base=f"http://127.0.0.1:{server.server_port}"
        def invoke(route, actor, body=None):
            headers={"Authorization":"Bearer "+actor,"Idempotency-Key":"same-key"}
            req=urllib.request.Request(base+route, headers=headers, data=json.dumps(body).encode() if body else None)
            try:
                with urllib.request.urlopen(req, timeout=2) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)
        try:
            request={"protocol":"1.0", "mode":"read", "workspace_id":"test-ws", "instruction":"inspect"}
            alice=invoke("/v1/tasks", "first", request)[1]["task_id"]
            self.assertEqual(invoke("/v1/tasks", "second", request)[0], 403)
            bob=invoke("/v1/tasks", "second", dict(request, workspace_id="other-ws"))[1]["task_id"]
            self.assertNotEqual(alice, bob)
            self.assertEqual(invoke("/v1/tasks/"+alice, "second")[0], 404)
            self.assertEqual(invoke("/v1/tasks/"+alice+"/events?after=0", "second")[0],404)
            self.assertEqual(invoke("/v1/tasks/"+alice, "first")[0],200)
            self.assertEqual(invoke("/v1/tasks/"+bob, "first")[0],404)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
