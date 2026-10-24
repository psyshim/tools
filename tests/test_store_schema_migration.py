from pathlib import Path
import sqlite3
import tempfile
import unittest

from coding_service.errors import LogUnavailable
from coding_service.store import TaskStore, SCHEMA_VERSION


class StoreSchemaMigrationTests(unittest.TestCase):
    def test_known_unversioned_database_is_upgraded_without_losing_events_or_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = TaskStore(path)
            tid, _, _ = store.create_or_get("host:user", "ws", "key", "instruction")
            before = store.snapshot(tid)
            store._db.execute("DROP TABLE model_usage")  # the supported pre-accounting schema
            store._db.execute("PRAGMA user_version=0")
            store.close()
            upgraded = TaskStore(path)
            try:
                self.assertEqual(upgraded._db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(upgraded.snapshot(tid), before)
                self.assertEqual(upgraded.usage_summary()["calls"], 0)
            finally:
                upgraded.close()

    def test_future_version_is_refused_without_overwriting_its_marker_or_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "future.db"
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA user_version=999")
            connection.close()
            before = path.read_bytes()
            with self.assertRaises(LogUnavailable):
                TaskStore(path)
            self.assertEqual(path.read_bytes(), before)

    def test_known_version_with_unrecognized_columns_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = TaskStore(path)
            store._db.execute("ALTER TABLE tasks ADD COLUMN future TEXT")
            store.close()
            with self.assertRaises(LogUnavailable):
                TaskStore(path)

    def test_unrelated_database_is_refused_without_adding_component_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unrelated.db"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE human_data(value TEXT)")
            connection.commit()
            connection.close()
            before = path.read_bytes()
            with self.assertRaises(LogUnavailable):
                TaskStore(path)
            self.assertEqual(path.read_bytes(), before)
