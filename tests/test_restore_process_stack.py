"""A service restored from a live backup must not replay model calls or work."""
import sqlite3
import unittest

try:
    from stack_harness import Stack, drain
except ImportError:
    from tests.stack_harness import Stack, drain

from coding_service.backup import backup_store, restore_store
from coding_service.store import TaskStore


class RestoreProcessStackTests(unittest.TestCase):
    def test_restore_holds_a_live_model_attempt_and_preserves_idempotency(self):
        stack = Stack(self)
        original_bytes = stack.project.joinpath("a.txt").read_bytes()
        flag = stack.base / "model-release.flag"
        stack.env_extra["MODEL_RELEASE_FLAG"] = str(flag)
        stack.start()
        gateway = stack.gateway()
        handle = gateway.start_coding("alice", "shop", "wait during model call", conversation_id="restore", message_id="one")
        observed = []
        for event in gateway.observe_coding("alice", handle, poll_interval=0.05, max_seconds=30):
            observed.append(event)
            if event.type == "notice" and event.data.get("code") == "model_call_started":
                break
        self.assertTrue(any(e.data.get("code") == "model_call_started" for e in observed))
        backup = stack.base / "snapshot.db"
        live = TaskStore(stack.db)
        try:
            receipt = backup_store(live, backup)
        finally:
            live.close()
        stack.stop("service", kill=True)
        restored = stack.base / "restored.db"
        restored_receipt = restore_store(backup, restored, receipt["sha256"])
        self.assertEqual(restored_receipt["unfinished_tasks_held"], 1)
        stack.db = restored
        stack.start_service()
        gateway = stack.gateway()
        events = drain(gateway, "alice", handle)
        self.assertEqual(events[-1].data["state"], "NEEDS_RECONCILIATION")
        self.assertTrue(any(e.data.get("code") == "restored_from_backup" and e.data.get("manual_review_required") for e in events))
        self.assertEqual(gateway.start_coding("alice", "shop", "wait during model call", conversation_id="restore", message_id="one"), handle)
        flag.write_text("release", encoding="utf-8")
        # The restored task stays terminal even though a model callback could now finish.
        self.assertEqual(drain(gateway, "alice", handle)[-1].data["state"], "NEEDS_RECONCILIATION")
        connection = sqlite3.connect(restored)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
        finally:
            connection.close()
        self.assertEqual(stack.project.joinpath("a.txt").read_bytes(), original_bytes)
