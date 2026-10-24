import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from coding_service.backup import backup_store, restore_store, APPLICATION_ID
from coding_service.manage import main
from coding_service.manager import DurableManager
from coding_service.store import TaskStore
from coding_service.usage import normalize_usage


class StateBackupTests(unittest.TestCase):
    def test_hashing_a_large_snapshot_honors_the_same_deadline(self):
        from coding_service.backup import _receipt
        source = self.root / "large.bin"
        source.write_bytes(b"x" * 131072)
        with patch("coding_service.backup.time.monotonic", side_effect=[0, 2]), self.assertRaises(TimeoutError):
            _receipt(source, deadline=1)

    def test_cli_explains_checksum_refusal_without_publishing_a_destination(self):
        backup, _ = self.snapshot()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = main(["--db", str(self.root / "new.db"), "restore", "--input", str(backup), "--sha256", "0" * 64])
        self.assertEqual(result, 1)
        self.assertIn("SHA-256 mismatch", stderr.getvalue())
        self.assertFalse((self.root / "new.db").exists())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "service.db"
        self.store = TaskStore(self.db)
        self.addCleanup(self.store.close)
        self.done, _, _ = self.store.create_or_get("host:user", "ws", "done", "日本語の依頼")
        self.store.append(self.done, "notice", {"code": "model_usage", "model_call_id": "one", **normalize_usage({"input_tokens": 100, "output_tokens": 25})})
        self.store.append(self.done, "state", {"state": "COMPLETED"})
        self.accepted, _, _ = self.store.create_or_get("host:user", "ws", "accepted", "not started at snapshot time")
        self.running, _, _ = self.store.create_or_get("host:user", "ws", "running", "possibly billed after snapshot")
        self.store.append(self.running, "state", {"state": "RUNNING"})
        self.store.append(self.running, "notice", {"code": "model_call_started", "model_call_id": "pending", **normalize_usage(None)})

    def snapshot(self):
        path = self.root / "backup.db"
        receipt = backup_store(self.store, path)
        self.assertEqual(receipt["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertFalse(receipt["device_state_included"])
        return path, receipt["sha256"]

    def test_live_wal_backup_and_restore_preserve_usage_identity_and_hold_old_work(self):
        self.assertTrue(Path(str(self.db) + "-wal").exists())
        original = self.store.snapshot(self.done)
        backup, digest = self.snapshot()
        self.store.append(self.done, "notice", {"code": "model_usage", "model_call_id": "later", **normalize_usage({"input_tokens": 9, "output_tokens": 1})})
        restored_path = self.root / "restored.db"
        receipt = restore_store(backup, restored_path, digest)
        self.assertEqual(receipt["unfinished_tasks_held"], 2)
        self.assertEqual(receipt["sha256"], hashlib.sha256(restored_path.read_bytes()).hexdigest())
        self.assertFalse(Path(str(restored_path) + "-wal").exists())
        restored = TaskStore(restored_path)
        try:
            self.assertEqual(restored.snapshot(self.done), original)
            summary = restored.usage_summary()
            self.assertEqual((summary["calls"], summary["known_total_tokens"], summary["unfinished_calls"]), (2, 125, 1))
            for tid in (self.accepted, self.running):
                self.assertEqual(restored.get(tid)["state"], "NEEDS_RECONCILIATION")
                self.assertIn("restored_from_backup", [e["data"].get("code") for e in restored.snapshot(tid)["events"]])
            self.assertEqual(restored.incomplete(), [])
            manager = DurableManager(restored, {}, {}, Mock())
            self.assertEqual(manager.recover()["resumed"], [])
            manager.model.assert_not_called()
            same, state, created = restored.create_or_get("host:user", "ws", "accepted", "not started at snapshot time")
            self.assertEqual((same, state, created), (self.accepted, "NEEDS_RECONCILIATION", False))
            self.assertEqual(self.store.get(self.running)["state"], "RUNNING", "restoration cannot alter the live source")
        finally:
            restored.close()

    def test_hash_mismatch_and_malformed_database_publish_nothing(self):
        backup, digest = self.snapshot()
        target = self.root / "restored.db"
        before = set(self.root.iterdir())
        with self.assertRaises(ValueError):
            restore_store(backup, target, "0" * 64)
        self.assertEqual(set(self.root.iterdir()), before)
        damaged = self.root / "broken.db"
        damaged.write_bytes(b"not SQLite")
        with self.assertRaises((ValueError, sqlite3.Error)):
            restore_store(damaged, target, hashlib.sha256(damaged.read_bytes()).hexdigest())
        self.assertFalse(target.exists())

    def test_unrelated_valid_sqlite_database_is_not_accepted_as_a_backup(self):
        source = self.root / "unrelated.db"
        connection = sqlite3.connect(source)
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute("CREATE TABLE unrelated(x)")
        connection.close()
        with self.assertRaises(ValueError):
            restore_store(source, self.root / "restored.db", hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertFalse((self.root / "restored.db").exists())

    def test_existing_destinations_are_never_replaced(self):
        backup, digest = self.snapshot()
        target = self.root / "existing.db"
        target.write_bytes(b"human data")
        for action in (lambda: backup_store(self.store, target), lambda: restore_store(backup, target, digest)):
            with self.assertRaises(FileExistsError):
                action()
            self.assertEqual(target.read_bytes(), b"human data")

    def test_fsync_failures_and_deadlines_leave_no_published_or_temporary_files(self):
        backup, digest = self.snapshot()
        before = set(self.root.iterdir())
        for action in (lambda: backup_store(self.store, self.root / "new.db"),
                       lambda: restore_store(backup, self.root / "new.db", digest)):
            with patch("coding_service.backup.os.fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
                action()
            self.assertEqual(set(self.root.iterdir()), before)
        with patch("coding_service.backup.time.monotonic", side_effect=[0, 2]), self.assertRaises(TimeoutError):
            backup_store(self.store, self.root / "new.db", timeout=1)
        with patch("coding_service.backup.time.monotonic", side_effect=[0, 2]), self.assertRaises(TimeoutError):
            restore_store(backup, self.root / "new.db", digest, timeout=1)
        self.assertEqual(set(self.root.iterdir()), before)

    def test_future_schema_malformed_json_and_orphans_are_refused(self):
        backup, _ = self.snapshot()
        for name, mutate in (
            ("future", lambda c: c.execute("PRAGMA user_version=999")),
            ("columns", lambda c: c.execute("ALTER TABLE model_usage ADD COLUMN future TEXT")),
            ("json", lambda c: c.execute("UPDATE events SET data='{' WHERE type='notice'")),
            ("orphan", lambda c: c.execute("UPDATE model_usage SET task_id='missing'")),
        ):
            with self.subTest(name=name):
                source = self.root / (name + ".db")
                source.write_bytes(backup.read_bytes())
                connection = sqlite3.connect(source)
                mutate(connection)
                connection.commit()
                connection.close()
                with self.assertRaises((ValueError, sqlite3.Error)):
                    restore_store(source, self.root / "new.db", hashlib.sha256(source.read_bytes()).hexdigest())
                self.assertFalse((self.root / "new.db").exists())

    def test_cli_backup_restore_and_full_export_are_self_contained(self):
        backup, restored, exported = (self.root / name for name in ("cli-backup.db", "cli-restored.db", "full.jsonl"))
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            self.assertEqual(main(["--db", str(self.db), "backup", "--output", str(backup)]), 0)
        digest = json.loads(capture.getvalue())["sha256"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--db", str(restored), "restore", "--input", str(backup), "--sha256", digest]), 0)
            self.assertEqual(main(["--db", str(restored), "export", "--task-id", self.done, "--include-content", "--output", str(exported)]), 0)
        data = [json.loads(line) for line in exported.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(data[0]["data"]["instruction"], "日本語の依頼")
