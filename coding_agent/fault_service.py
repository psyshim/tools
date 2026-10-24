"""Deterministic loopback-only mock task service; no file, shell or Git access."""
import hashlib
import hmac
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

class ScenarioStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.tasks = {}
        self.keys = {}
        self.calls = 0
        self.configure("normal")

    def configure(self, scenario):
        if scenario not in {"normal", "unavailable", "drop_after_create", "event_replay", "cursor_expired", "fail_task"}:
            raise ValueError("unknown scenario")
        with self.lock:
            self.scenario = scenario

    def reset(self):
        with self.lock:
            self.tasks.clear()
            self.keys.clear()
            self.calls = 0

def handler_factory(token, control_token, store=None, actor_tokens=None):
    if not token or not control_token or token == control_token:
        raise ValueError("distinct tokens required")
    state = store or ScenarioStore()
    actors = actor_tokens if actor_tokens is not None else {token: "single-test-actor"}
    if not isinstance(actors, dict) or not actors or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in actors.items()):
        raise ValueError("invalid actor token mapping")
    if control_token in actors:
        raise ValueError("control token must be isolated")
    class Handler(BaseHTTPRequestHandler):
        def send(self, code, obj):
            data = json.dumps(obj, sort_keys=True).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def auth(self, key):
            return hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + key)

        def body(self):
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16384:
                raise ValueError("invalid length")
            value = json.loads(self.rfile.read(size))
            if not isinstance(value, dict):
                raise ValueError("object expected")
            return value

        def do_POST(self):
            if self.path == "/__test/scenario":
                if not self.auth(control_token):
                    return self.send(401, {"error":"unauthorized"})
                try:
                    request = self.body()
                    scenario = request["scenario"]
                    self.body_reset_requested = request.get("reset", False) is True
                    state.configure(scenario)
                    if self.body_reset_requested:
                        state.reset()
                except (KeyError, TypeError, ValueError):
                    return self.send(400, {"error":"invalid_scenario"})
                return self.send(200, {"scenario":scenario})
            actor = next((name for secret, name in actors.items() if self.auth(secret)), None)
            if actor is None:
                return self.send(401, {"error":"unauthorized"})
            if self.path != "/v1/tasks":
                return self.send(404, {"error":"not_found"})
            if state.scenario == "unavailable":
                return self.send(503, {"error":"unavailable"})
            key = self.headers.get("Idempotency-Key", "")
            if not 1 <= len(key) <= 128:
                return self.send(400, {"error":"idempotency_key_required"})
            try:
                obj = self.body()
                if obj.get("protocol") != "1.0" or obj.get("mode") != "read" or not isinstance(obj.get("workspace_id"), str) or not obj["workspace_id"].strip() or not isinstance(obj.get("instruction"), str) or not obj["instruction"].strip():
                    raise ValueError("unsupported request")
                digest = hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            except (TypeError, ValueError):
                return self.send(400, {"error":"invalid_request"})
            with state.lock:
                state.calls += 1
                scoped_key = (actor, key)
                if scoped_key in state.keys:
                    old_digest, task_id = state.keys[scoped_key]
                    if old_digest != digest:
                        return self.send(409, {"error":"idempotency_conflict"})
                else:
                    task_id = "mock-" + str(uuid.uuid4())
                    state.keys[scoped_key] = (digest, task_id)
                    outcome = "FAILED" if state.scenario == "fail_task" else "COMPLETED"
                    state.tasks[task_id] = {"task_id":task_id, "owner":actor, "workspace_id":obj["workspace_id"], "state":outcome, "event_cursor":"2", "events":[
                        {"id":"1", "task_id":task_id, "type":"task.started", "payload":{}},
                        {"id":"2", "task_id":task_id, "type":"task.failed" if outcome == "FAILED" else "task.completed", "payload":{"status":outcome}}
                    ]}
                scenario = state.scenario
            if scenario == "drop_after_create" and state.calls == 1:
                self.close_connection = True
                return
            return self.send(202, {"task_id":task_id, "state":"QUEUED", "event_cursor":"0"})

        def do_GET(self):
            if not self.auth(token):
                return self.send(401, {"error":"unauthorized"})
            if self.path == "/v1/capabilities":
                return self.send(200, {"protocol":"1.0", "capabilities":["tasks","events"], "mock":True})
            parsed = urlsplit(self.path)
            parts = parsed.path.strip("/").split("/")
            if len(parts) not in (3,4) or parts[:2] != ["v1","tasks"]:
                return self.send(404, {"error":"not_found"})
            with state.lock:
                task = state.tasks.get(parts[2])
                scenario = state.scenario
            if task is None or task["owner"] != actor:
                return self.send(404, {"error":"not_found"})
            if len(parts) == 3:
                return self.send(200, {k:task[k] for k in ("task_id","state","event_cursor")})
            if parts[3] != "events":
                return self.send(404, {"error":"not_found"})
            cursor = parse_qs(parsed.query).get("after", ["0"])[0]
            if not cursor.isdecimal() or len(cursor) > 10:
                return self.send(400, {"error":"invalid_cursor"})
            if scenario == "cursor_expired":
                return self.send(410, {"error":"EVENT_CURSOR_EXPIRED", "snapshot":{"task_id":task["task_id"], "state":task["state"]}})
            events = [e for e in task["events"] if int(e["id"]) > int(cursor)]
            if scenario == "event_replay" and events:
                events = [events[0]] + events
            return self.send(200, {"events":events, "cursor":task["event_cursor"]})
        def log_message(self, *args):
            pass
    return Handler

def main():
    import argparse
    import os
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1",args.port),handler_factory(os.environ.get("MOCK_AGENT_TOKEN"),os.environ.get("MOCK_CONTROL_TOKEN"))).serve_forever()

if __name__ == "__main__":
    main()
