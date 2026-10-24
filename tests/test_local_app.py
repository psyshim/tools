"""Exercise the runnable host through its real routes and real file/test effects."""
import tempfile
import time
from pathlib import Path
import sys
import threading
import unittest

from fastapi.testclient import TestClient
from examples.local_app import create_app, demo_callback


class LocalAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        (self.root / "test_calc.py").write_text("from calc import add\nassert add(1, 2) == 3\n", encoding="utf-8")
        self.logs = self.root / "logs"
        self.client = TestClient(create_app(self.root, demo_callback, "secret", {"unit": [sys.executable, "test_calc.py"]}, demo=True, log_dir=self.logs))
        self.headers = {"Authorization": "Bearer secret"}

    def wait(self, tid, predicate):
        for _ in range(300):
            data = self.client.get(f"/api/code/tasks/{tid}/events", headers=self.headers).json()
            if predicate(data):
                return data
            time.sleep(.01)
        self.fail("timed out: " + repr(data))

    def test_public_ui_never_embeds_token_and_api_requires_auth(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("app.js", response.text)
        self.assertNotIn("secret", response.text)
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        for route in ("/api/config", "/api/code/availability"):
            self.assertEqual(self.client.get(route).status_code, 401)
        self.assertEqual(self.client.get("/api/config", headers=self.headers).json()["demo"], True)
        self.assertEqual(self.client.get("/app.js").status_code, 200)
        self.assertEqual(self.client.get("/app.css").status_code, 200)

    def test_foreign_origin_and_host_rejected(self):
        for headers in ({"Origin": "https://example.com"}, {"Host": "attacker.example"}):
            self.assertEqual(self.client.get("/api/config", headers={**self.headers, **headers}).status_code, 403)
        self.assertEqual(self.client.get("/api/config", headers={**self.headers, "Origin": "http://testserver"}).status_code, 200)

    def test_edit_and_real_test_require_separate_approvals(self):
        data = self.client.post("/api/code/tasks", headers=self.headers, json={"workspace_id": "project", "instruction": "fix", "idempotency_key": "one"}).json()
        tid = data["task_id"]
        for count in (1, 2):
            data = self.wait(tid, lambda d: sum(e["type"] == "approval_required" for e in d["events"]) >= count)
            pending = [e["data"] for e in data["events"] if e["type"] == "approval_required"][-1]
            source = (self.root / "calc.py").read_text(encoding="utf-8")
            self.assertIn("a - b" if count == 1 else "a + b", source)
            response = self.client.post(f"/api/code/tasks/{tid}/decision", headers=self.headers, json={"action_id": pending["action_id"], "fingerprint": pending["fingerprint"], "approve": True})
            self.assertEqual(response.status_code, 200)
        data = self.wait(tid, lambda d: d["terminal"])
        self.assertEqual(data["state"], "COMPLETED")
        result = [e["data"]["result"] for e in data["events"] if e["type"] == "tool_result" and e["data"]["tool"] == "run_tests"][-1]
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["exit_code"], 0)
        import json
        records = [json.loads(line) for line in (self.logs / (tid + ".jsonl")).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[0]["data"]["instruction"], "fix")
        self.assertEqual(sum(r["type"] == "approval_decision" and r["data"]["approve"] for r in records), 2)
        self.assertEqual(records[-1]["data"]["state"], "COMPLETED")
        self.assertTrue(all(r["timestamp"] and r["task_id"] == tid for r in records))
        # Recreating the user's host neither truncates nor replaces existing logs.
        before = (self.logs / (tid + ".jsonl")).read_bytes()
        create_app(self.root, demo_callback, "new-secret", log_dir=self.logs)
        self.assertEqual((self.logs / (tid + ".jsonl")).read_bytes(), before)

    def test_rejected_edit_leaves_file_unchanged(self):
        data = self.client.post("/api/code/tasks", headers=self.headers, json={"workspace_id": "project", "instruction": "fix", "idempotency_key": "one"}).json()
        tid = data["task_id"]
        data = self.wait(tid, lambda d: d["state"] == "AWAITING_APPROVAL")
        pending = [e["data"] for e in data["events"] if e["type"] == "approval_required"][-1]
        self.client.post(f"/api/code/tasks/{tid}/decision", headers=self.headers, json={"action_id": pending["action_id"], "fingerprint": pending["fingerprint"], "approve": False})
        self.wait(tid, lambda d: d["terminal"])
        self.assertIn("a - b", (self.root / "calc.py").read_text(encoding="utf-8"))

    def test_cancel_during_model_response_is_not_reported_completed(self):
        from code_attach.engine import Engine
        entered, release = threading.Event(), threading.Event()
        def complete(messages, tools):
            entered.set()
            release.wait(3)
            return {"text": "done"}
        engine = Engine(complete)
        tid, _ = engine.start("local", self.root, "cancel", "fix")
        self.assertTrue(entered.wait(2))
        engine.cancel("local", tid)
        release.set()
        self.assertTrue(engine.join("local", tid))
        self.assertEqual(engine.events("local", tid)["state"], "CANCELLED")

    def test_log_failure_rejects_start_without_calling_model(self):
        from code_attach import make_router
        from fastapi import FastAPI
        from unittest.mock import Mock
        complete = Mock()
        def unavailable(task_id, event):
            raise OSError("disk full")
        app = FastAPI()
        app.include_router(make_router(complete, lambda r: "local", lambda p, w: self.root, event_sink=unavailable, allow_mutation=True, prototype=True))
        response = TestClient(app).post("/tasks", json={"workspace_id": "project", "instruction": "fix", "idempotency_key": "one"})
        self.assertEqual(response.status_code, 503)
        complete.assert_not_called()

    def test_log_failure_at_approval_does_not_apply_edit(self):
        from code_attach.engine import Engine
        from coding_agent.activity_log import ActivityLog
        log = ActivityLog(self.logs)
        def sink(task_id, event):
            if event["type"] == "approval_decision":
                raise OSError("disk full")
            log.record(task_id, event)
        engine = Engine(demo_callback, event_sink=sink)
        tid, _ = engine.start("local", self.root, "one", "fix")
        for _ in range(200):
            data = engine.events("local", tid)
            pending = [e["data"] for e in data["events"] if e["type"] == "approval_required"]
            if pending:
                break
            time.sleep(.01)
        self.assertTrue(pending)
        with self.assertRaises(OSError):
            engine.decide("local", tid, pending[0]["action_id"], pending[0]["fingerprint"], True)
        self.assertIn("a - b", (self.root / "calc.py").read_text(encoding="utf-8"))
        engine.cancel("local", tid)
        self.assertTrue(engine.join("local", tid))

    def test_logging_failure_cannot_prevent_cancellation(self):
        from code_attach.engine import Engine
        entered, release = threading.Event(), threading.Event()
        broken = False
        def sink(task_id, event):
            if broken:
                raise OSError("disk full")
        def complete(messages, tools):
            entered.set()
            release.wait(3)
            return {"text": "done"}
        engine = Engine(complete, event_sink=sink)
        tid, _ = engine.start("local", self.root, "stop", "fix")
        self.assertTrue(entered.wait(2))
        broken = True
        engine.cancel("local", tid)
        release.set()
        self.assertTrue(engine.join("local", tid))
        events = engine.events("local", tid)
        self.assertTrue(events["terminal"])
        self.assertEqual(events["state"], "FAILED")
        self.assertEqual(events["events"][-1]["data"]["detail"], "activity log unavailable")


if __name__ == "__main__":
    unittest.main()
