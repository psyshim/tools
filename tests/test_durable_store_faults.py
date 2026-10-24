"""Injected SQLite commit failures must not poison subsequent durable requests."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from coding_service.errors import LogUnavailable
from coding_service.store import TaskStore


class FailNextCommit:
    def __init__(self, connection):
        self.connection = connection
        self.fail = True

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args):
        if sql == "COMMIT" and self.fail:
            self.fail = False
            # SQLITE_BUSY can leave the transaction active, unlike some IO errors.
            raise sqlite3.OperationalError("injected commit failure")
        return self.connection.execute(sql, *args)


class StoreFaultTests(unittest.TestCase):
    def test_failed_usage_commit_rolls_back_both_ledger_and_event(self):
        from coding_service.usage import normalize_usage
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.db")
            try:
                tid, _, _ = store.create_or_get("user", "ws", "key", "instruction")
                before = store.snapshot(tid)
                data = {"code": "model_usage", "model_call_id": "call", **normalize_usage({"input_tokens": 10, "output_tokens": 5})}
                store._db = FailNextCommit(store._db)
                with self.assertRaises(LogUnavailable):
                    store.append(tid, "notice", data)
                self.assertEqual(store.usage_summary()["calls"], 0)
                self.assertEqual(store.snapshot(tid), before)
                store.append(tid, "notice", data)
                self.assertEqual(store.usage_summary()["known_total_tokens"], 15)
            finally:
                store.close()

    def test_failed_create_commit_rolls_back_and_next_request_works(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.db")
            try:
                store._db = FailNextCommit(store._db)
                with self.assertRaises(LogUnavailable):
                    store.create_or_get("user", "ws", "key", "instruction")
                self.assertIsNone(store.lookup("user", "key"))
                task_id, state, created = store.create_or_get("user", "ws", "key", "instruction")
                self.assertTrue(created)
                self.assertEqual(state, "ACCEPTED")
                self.assertEqual(len(store.snapshot(task_id)["events"]), 1)
            finally:
                store.close()

    def test_failed_event_commit_leaves_neither_event_nor_state_change(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.db")
            try:
                task_id, _, _ = store.create_or_get("user", "ws", "key", "instruction")
                before = store.snapshot(task_id)
                store._db = FailNextCommit(store._db)
                with self.assertRaises(LogUnavailable):
                    store.append(task_id, "state", {"state": "COMPLETED"})
                self.assertEqual(store.snapshot(task_id), before)
                store.append(task_id, "state", {"state": "FAILED"})
                self.assertEqual(store.snapshot(task_id)["state"], "FAILED")
            finally:
                store.close()
