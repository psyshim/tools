"""Phase 5 gate: task identity, idempotency, ordered events and terminal outcomes live in the service's durable store.
Real processes are killed (no clean shutdown) and restarted; cursors, ids and ownership must mean the same thing afterwards."""
import json
import sqlite3
import subprocess
import sys
import time
import unittest

try:
    from stack_harness import Stack, drain
except ImportError:
    from tests.stack_harness import Stack, drain

from host_client import AgentUnavailable, ProtocolRefused

def db_rows(stack, sql, args=()):
    conn = sqlite3.connect(f"file:{stack.db.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()

def poll_all(gw, user, handle, after=0):
    result = gw.poll_coding(user, handle, after)
    return result

def wait_for(predicate, seconds=30):
    end = time.time() + seconds
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")

def events_of(stack, task_id):
    return [(seq, kind, json.loads(data)) for seq, kind, data in db_rows(stack, "SELECT seq, type, data FROM events WHERE task_id=? ORDER BY seq", (task_id,))]

class RestartKeepsMeaning(unittest.TestCase):
    def test_cursor_identity_idempotency_and_ownership_survive_a_hard_kill(self):
        stack = Stack(self).start()
        gw = stack.gateway()
        handle = gw.start_coding("alice", "shop", "propose a change", conversation_id="c", message_id="m1")
        before = drain(gw, "alice", handle)
        self.assertEqual(before[-1].data["state"], "COMPLETED")
        stack.stop("service", kill=True)
        stack.start_service()
        gw = stack.gateway()
        after = drain(gw, "alice", handle)
        self.assertEqual([(e.event_id, e.type, e.data) for e in after], [(e.event_id, e.type, e.data) for e in before], "replay after a restart is identical")
        tail = gw.poll_coding("alice", handle, before[1].event_id)
        self.assertEqual([e.event_id for e in tail.events], [e.event_id for e in before[2:]])
        self.assertEqual(gw.start_coding("alice", "shop", "propose a change", conversation_id="c", message_id="m1"), handle, "same message, same task, across restarts")
        with self.assertRaises(ProtocolRefused) as caught:
            gw.start_coding("alice", "shop", "a DIFFERENT instruction", conversation_id="c", message_id="m1")
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")
        for other_user, other_host in (("bob", "chat-a"), ("alice", "chat-b")):
            with self.assertRaises(ProtocolRefused) as caught:
                drain(stack.gateway(other_host), other_user, handle)
            self.assertEqual(caught.exception.code, "NOT_FOUND")

    def test_a_task_killed_while_running_is_closed_explicitly_with_no_gap_or_rewrite(self):
        stack = Stack(self)
        flag = stack.base / "release.flag"
        stack.env_extra["MODEL_RELEASE_FLAG"] = str(flag)
        stack.start()
        gw = stack.gateway()
        handle = gw.start_coding("alice", "shop", "wait for me", conversation_id="c", message_id="m2")
        seen = wait_for(lambda: [e for e in gw.poll_coding("alice", handle, 0).events if e.type == "state"])
        prefix = gw.poll_coding("alice", handle, 0).events
        self.assertEqual(seen[0].data["state"], "RUNNING")
        stack.stop("service", kill=True)
        stack.start_service()
        gw = stack.gateway()
        events = drain(gw, "alice", handle)
        self.assertEqual([(e.event_id, e.type, e.data) for e in events[:len(prefix)]], [(e.event_id, e.type, e.data) for e in prefix], "nothing already written was changed")
        self.assertEqual([e.event_id for e in events], list(range(1, len(events) + 1)), "ids stay gap-free")
        self.assertEqual(events[-2].data["code"], "service_restarted")
        self.assertEqual(events[-1].data["state"], "FAILED")
        self.assertIn("interrupted", events[-1].data["detail"])
        count = len(events)
        flag.write_text("go", encoding="utf-8")
        self.assertEqual(gw.start_coding("alice", "shop", "wait for me", conversation_id="c", message_id="m2"), handle)
        time.sleep(0.5)
        self.assertEqual(len(drain(gw, "alice", handle)), count, "the same key never re-runs a closed task")

    def test_crash_right_after_acceptance_neither_loses_nor_duplicates_the_task(self):
        stack = Stack(self, env_extra={"CODING_SERVICE_ALLOW_FAILPOINTS": "1", "CODING_SERVICE_FAILPOINT": "after_accept"})
        stack.start()
        gw = stack.gateway()
        with self.assertRaises(AgentUnavailable):
            gw.start_coding("alice", "shop", "propose a change", conversation_id="c", message_id="m3")     # the service dies after committing, before answering
        self.assertEqual(stack.procs["service"].wait(timeout=20), 137)
        self.assertEqual(len(db_rows(stack, "SELECT id FROM tasks")), 1, "the accepted task is durable even though no answer was ever sent")
        stack.env_extra.pop("CODING_SERVICE_FAILPOINT")
        stack.start_service()
        gw = stack.gateway()
        handle = gw.start_coding("alice", "shop", "propose a change", conversation_id="c", message_id="m3")      # the client simply retries
        events = drain(gw, "alice", handle)
        self.assertEqual(events[-1].data["state"], "COMPLETED")
        self.assertIn("proposal_completed", [e.type for e in events])
        self.assertEqual(len(db_rows(stack, "SELECT id FROM tasks")), 1, "still exactly one task")
        codes = [e.data.get("code") for e in events if e.type == "notice"]
        self.assertEqual(codes[:2], ["task_accepted", "worker_started"])

    def test_every_invariant_holds_after_kills_at_arbitrary_moments(self):
        stack = Stack(self).start()
        gw = stack.gateway()
        handles = []
        for i in range(5):
            handles.append(gw.start_coding("alice", "shop", f"propose change {i}", conversation_id="c", message_id=f"k{i}"))
        time.sleep(0.15)
        stack.stop("service", kill=True)
        stack.start_service()
        gw = stack.gateway()
        for handle in handles:
            drain(gw, "alice", handle)       # every task reaches a terminal state, whatever moment the kill landed on
        for task_id, state in db_rows(stack, "SELECT id, state FROM tasks"):
            events = events_of(stack, task_id)
            self.assertEqual([seq for seq, _, _ in events], list(range(1, len(events) + 1)), "contiguous sequence, no duplicates")
            states = [d["state"] for _, kind, d in events if kind == "state"]
            self.assertIn(state, ("COMPLETED", "FAILED", "NEEDS_RECONCILIATION", "CANCELLED"))
            self.assertEqual(states[-1], state, "the task row and its last state event agree")
            self.assertEqual(sum(1 for _, kind, d in events if kind == "state" and d["state"] in ("COMPLETED", "FAILED", "CANCELLED", "NEEDS_RECONCILIATION")), 1, "exactly one terminal event")

    def test_repeated_restarts_are_harmless(self):
        stack = Stack(self).start()
        handle = stack.gateway().start_coding("alice", "shop", "propose a change", conversation_id="c", message_id="m4")
        first = drain(stack.gateway(), "alice", handle)
        for _ in range(3):
            stack.stop("service", kill=True)
            stack.start_service()
        self.assertEqual([e.event_id for e in drain(stack.gateway(), "alice", handle)], [e.event_id for e in first])

class StoreProblems(unittest.TestCase):
    def test_a_corrupt_store_stops_the_service_with_a_clear_message_instead_of_serving_nonsense(self):
        stack = Stack(self)
        stack.db.write_bytes(b"this is not a sqlite database" * 100)
        stack.start_device()
        import os
        path = stack.base / "service.json"
        path.write_text(json.dumps(stack.service_config()), encoding="utf-8")
        result = subprocess.run([sys.executable, "-m", "coding_service", "--config", str(path), "--port", "0"], cwd=str(stack.service_tree),
                                env={**os.environ, "PYTHONPATH": str(stack.service_tree), "SVC_DEVICE_TOKEN": stack.device_token}, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 4, result.stderr[-600:])
        self.assertIn("task store", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_a_bad_configuration_is_refused_as_a_whole(self):
        stack = Stack(self)
        import os
        bad = stack.service_config() if hasattr(stack, "device_url") else {"db_path": "relative", "devices": {}, "hosts": {}, "model": "nope", "surprise": 1}
        path = stack.base / "bad.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        result = subprocess.run([sys.executable, "-m", "coding_service", "--config", str(path), "--port", "0"], cwd=str(stack.service_tree),
                                env={**os.environ, "PYTHONPATH": str(stack.service_tree)}, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        for expected in ("db_path must be an absolute path", "unknown key 'surprise'", "devices must be a non-empty object", "model must be"):
            self.assertIn(expected, result.stderr)

if __name__ == "__main__":
    unittest.main()
