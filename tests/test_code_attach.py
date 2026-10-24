"""End-to-end tests of the attachment through FastAPI with a scripted fake model (no network, no LLM)."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from code_attach import make_router
from code_attach.providers import from_anthropic, from_openai, to_anthropic, to_openai

BUGGY = "def add(a, b):\n    return a - b\n"
TEST = "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\nif __name__ == '__main__':\n    unittest.main()\n"

def call(i, tool, **args):
    return {"id": f"c{i}", "name": tool, "args": args}

def scripted(steps):
    it = iter(steps)
    def complete(messages, tools):
        return next(it)
    return complete

class Env:
    def __init__(self, steps, tests=True):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "calc.py").write_text(BUGGY, encoding="utf-8")
        (self.root / "test_calc.py").write_text(TEST, encoding="utf-8")
        app = FastAPI()
        def authorize(request):
            user = request.headers.get("x-user")
            if not user:
                raise HTTPException(401)
            return user
        cmds = {"unit": [sys.executable, "test_calc.py"]} if tests else None
        app.include_router(make_router(scripted(steps), authorize, lambda p, w: str(self.root) if (p, w) == ("alice", "ws1") else None, cmds), prefix="/code")
        self.client = TestClient(app)

    def start(self, user="alice", key="k1", text="fix add"):
        return self.client.post("/code/tasks", json={"workspace_id": "ws1", "instruction": text, "idempotency_key": key}, headers={"x-user": user})

    def wait(self, tid, pred, user="alice"):
        for _ in range(200):
            r = self.client.get(f"/code/tasks/{tid}/events", headers={"x-user": user}).json()
            if pred(r):
                return r
            time.sleep(0.02)
        raise AssertionError("timeout waiting: %r" % r)

    def approve(self, tid, ev, approve=True, fingerprint=None):
        d = ev["data"]
        return self.client.post(f"/code/tasks/{tid}/decision", headers={"x-user": "alice"},
                                json={"action_id": d["action_id"], "fingerprint": fingerprint or d["fingerprint"], "approve": approve})

    def pending(self, tid, n):
        r = self.wait(tid, lambda r: sum(e["type"] == "approval_required" for e in r["events"]) >= n)
        return [e for e in r["events"] if e["type"] == "approval_required"][n - 1]

    def close(self):
        self.tmp.cleanup()

class CodeAttachTests(unittest.TestCase):
    def test_fix_and_test_with_approvals(self):
        # the proposal id is only known at runtime, so the fake model reads it from the tool result
        def complete(messages, tools):
            last = messages[-1]
            n = sum(m["role"] == "assistant" for m in messages)
            if n == 0:
                return {"text": "", "tool_calls": [call(1, "propose_edit", path="calc.py", old="a - b", new="a + b")]}
            if n == 1:
                import json
                return {"text": "", "tool_calls": [call(2, "apply_proposal", proposal_id=json.loads(last["content"])["proposal_id"])]}
            if n == 2:
                return {"text": "", "tool_calls": [call(3, "run_tests", name="unit")]}
            return {"text": "done", "tool_calls": []}
        env = Env([])
        app = FastAPI()
        app.include_router(make_router(complete, lambda r: "alice", lambda p, w: str(env.root), {"unit": [sys.executable, "test_calc.py"]}))
        client = TestClient(app)
        tid = client.post("/tasks", json={"workspace_id": "ws1", "instruction": "fix", "idempotency_key": "a"}).json()["task_id"]
        def pend(n):
            for _ in range(300):
                evs = client.get(f"/tasks/{tid}/events").json()["events"]
                p = [e for e in evs if e["type"] == "approval_required"]
                if len(p) >= n:
                    return p[n - 1]["data"]
                time.sleep(0.02)
            raise AssertionError("no approval")
        d = pend(1)
        self.assertIn("a + b", d["summary"]["diff"])
        self.assertEqual((env.root / "calc.py").read_text(encoding="utf-8"), BUGGY, "no write before approval")
        self.assertEqual(client.post(f"/tasks/{tid}/decision", json={"action_id": d["action_id"], "fingerprint": d["fingerprint"], "approve": True}).status_code, 200)
        d = pend(2)
        self.assertEqual((env.root / "calc.py").read_text(encoding="utf-8"), BUGGY.replace("a - b", "a + b"))
        client.post(f"/tasks/{tid}/decision", json={"action_id": d["action_id"], "fingerprint": d["fingerprint"], "approve": True})
        for _ in range(300):
            r = client.get(f"/tasks/{tid}/events").json()
            if r["terminal"]:
                break
            time.sleep(0.02)
        self.assertEqual(r["state"], "COMPLETED")
        tests = [e for e in r["events"] if e["type"] == "tool_result" and e["data"]["tool"] == "run_tests"][0]
        self.assertEqual(tests["data"]["result"]["status"], "PASS")
        env.close()

    def test_rejected_apply_does_not_write(self):
        env = Env([])
        import json
        def complete(messages, tools):
            n = sum(m["role"] == "assistant" for m in messages)
            if n == 0:
                return {"text": "", "tool_calls": [call(1, "propose_edit", path="calc.py", old="a - b", new="a + b")]}
            if n == 1:
                return {"text": "", "tool_calls": [call(2, "apply_proposal", proposal_id=json.loads(messages[-1]["content"])["proposal_id"])]}
            return {"text": "stopped", "tool_calls": []}
        app = FastAPI()
        app.include_router(make_router(complete, lambda r: "alice", lambda p, w: str(env.root)))
        c = TestClient(app)
        tid = c.post("/tasks", json={"workspace_id": "w", "instruction": "x", "idempotency_key": "a"}).json()["task_id"]
        for _ in range(300):
            p = [e for e in c.get(f"/tasks/{tid}/events").json()["events"] if e["type"] == "approval_required"]
            if p:
                break
            time.sleep(0.02)
        d = p[0]["data"]
        self.assertEqual(c.post(f"/tasks/{tid}/decision", json={"action_id": d["action_id"], "fingerprint": "0" * 64, "approve": True}).status_code, 409)
        c.post(f"/tasks/{tid}/decision", json={"action_id": d["action_id"], "fingerprint": d["fingerprint"], "approve": False})
        for _ in range(300):
            r = c.get(f"/tasks/{tid}/events").json()
            if r["terminal"]:
                break
            time.sleep(0.02)
        self.assertEqual(r["state"], "COMPLETED")
        self.assertEqual((env.root / "calc.py").read_text(encoding="utf-8"), BUGGY)
        env.close()

    def test_access_control_and_idempotency(self):
        env = Env([{"text": "hi", "tool_calls": []}] * 4)
        self.assertEqual(env.client.post("/code/tasks", json={"workspace_id": "ws1", "instruction": "x", "idempotency_key": "k"}).status_code, 401)
        self.assertEqual(env.start(user="bob").status_code, 404)  # no access to ws1
        tid = env.start().json()["task_id"]
        self.assertEqual(env.start().json()["task_id"], tid)  # same key, same content
        self.assertEqual(env.start(text="other").status_code, 409)  # same key, different content
        self.assertEqual(env.client.get(f"/code/tasks/{tid}/events", headers={"x-user": "bob"}).status_code, 404)
        env.close()

    def test_path_traversal_and_secret_files_fail_closed(self):
        env = Env([
            {"text": "", "tool_calls": [call(1, "read_file", path="../outside.txt"), call(2, "read_file", path=".env")]},
            {"text": "", "tool_calls": []},
        ])
        (env.root / ".env").write_text("SECRET=1", encoding="utf-8")
        tid = env.start().json()["task_id"]
        r = env.wait(tid, lambda r: r["terminal"])
        results = [e["data"]["result"] for e in r["events"] if e["type"] == "tool_result"]
        self.assertTrue(all(x["status"] == "ERROR" for x in results), results)
        self.assertNotIn("SECRET", str(r))
        env.close()

    def test_model_failure_is_failed_not_completed(self):
        env = Env([])  # scripted model raises StopIteration immediately
        tid = env.start().json()["task_id"]
        r = env.wait(tid, lambda r: r["terminal"])
        self.assertEqual(r["state"], "FAILED")
        env.close()

    def test_run_tests_unavailable_without_config(self):
        env = Env([{"text": "", "tool_calls": [call(1, "run_tests", name="unit")]}, {"text": "", "tool_calls": []}], tests=False)
        tid = env.start().json()["task_id"]
        r = env.wait(tid, lambda r: r["terminal"])
        res = [e for e in r["events"] if e["type"] == "tool_result"][0]["data"]
        self.assertEqual(res["result"]["status"], "ERROR")
        self.assertFalse([e for e in r["events"] if e["type"] == "approval_required"])
        env.close()

    def test_provider_converters_roundtrip(self):
        msgs = [{"role": "system", "text": "s"}, {"role": "user", "text": "u"},
                {"role": "assistant", "text": "t", "tool_calls": [call(1, "read_file", path="a")]},
                {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "{}"}]
        o = to_openai(msgs, [])
        self.assertEqual(o["messages"][2]["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(from_openai({"content": "x", "tool_calls": [o["messages"][2]["tool_calls"][0]]})["tool_calls"][0]["args"], {"path": "a"})
        a = to_anthropic(msgs, [])
        self.assertEqual(a["system"], "s")
        self.assertEqual(a["messages"][-1]["content"][0]["type"], "tool_result")
        self.assertEqual(from_anthropic(a["messages"][1]["content"])["tool_calls"][0]["name"], "read_file")

if __name__ == "__main__":
    unittest.main()
