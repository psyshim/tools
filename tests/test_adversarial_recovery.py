"""Adversarial regression tests for fault-service retry and type validation."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from coding_agent.fault_service import handler_factory, ScenarioStore

class AdversarialRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.store = ScenarioStore()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory("agent-token", "control-token", self.store))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def call(self, path, body, token="agent-token", key="stable"):
        headers={"Authorization": "Bearer " + token, "Idempotency-Key": key, "Content-Type": "application/json"}
        req=urllib.request.Request(self.base+path, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=2) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_response_lost_after_creation_reuses_task(self):
        self.assertEqual(self.call("/__test/scenario", {"scenario":"drop_after_create"}, token="control-token")[0], 200)
        task={"protocol":"1.0", "mode":"read", "workspace_id":"w", "instruction":"inspect"}
        with self.assertRaises((urllib.error.URLError, ConnectionError)):
            self.call("/v1/tasks", task)
        status, first = self.call("/v1/tasks", task)
        self.assertEqual(status, 202)
        self.assertEqual(first["task_id"], self.call("/v1/tasks", task)[1]["task_id"])
        self.assertEqual(len(self.store.tasks), 1)

    def test_response_loss_is_per_operation_not_global(self):
        self.assertEqual(self.call("/__test/scenario", {"scenario":"drop_after_create"}, token="control-token")[0], 200)
        for key in ("operation-a", "operation-b"):
            request={"protocol":"1.0", "mode":"read", "workspace_id":"w", "instruction":key}
            with self.assertRaises((urllib.error.URLError, ConnectionError)):
                self.call("/v1/tasks", request, key=key)
            status, saved = self.call("/v1/tasks", request, key=key)
            self.assertEqual(status, 202)
            self.assertEqual(self.call("/v1/tasks", request, key=key)[1]["task_id"], saved["task_id"])
        self.assertEqual(len(self.store.tasks), 2)

    def test_invalid_field_types_rejected(self):
        for field in ("workspace_id", "instruction"):
            for invalid in (["x"], {"x":1}, 42, ""):
                with self.subTest(field=field, invalid=invalid):
                    task={"protocol":"1.0", "mode":"read", "workspace_id":"w", "instruction":"inspect"}
                    task[field]=invalid
                    self.assertEqual(self.call("/v1/tasks", task)[0],400)
