import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from coding_service.manage import main
from coding_service.store import TaskStore
from coding_service.usage import normalize_usage


class ComponentManagementTests(unittest.TestCase):
    def test_export_fsync_failure_does_not_publish_a_partial_file(self):
        target = self.root / "logs.jsonl"
        before = set(self.root.iterdir())
        with patch("coding_service.manage.os.fsync", side_effect=OSError("disk full")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "export", "--output", str(target)]), 1)
        self.assertFalse(target.exists())
        self.assertEqual(set(self.root.iterdir()), before)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "service.db"
        self.store = TaskStore(self.db)
        self.addCleanup(self.store.close)
        self.tid, _, _ = self.store.create_or_get("host:user", "ws", "key", "PRIVATE-INSTRUCTION")
        self.store.append(self.tid, "assistant_message", {"text": "PRIVATE-OUTPUT"})
        self.store.append(self.tid, "notice", {"code": "model_usage", "model_call_id": "one", **normalize_usage({"input_tokens": 20, "output_tokens": 4})})
        self.store.append(self.tid, "state", {"state": "COMPLETED"})

    def test_pruning_is_dry_run_by_default_and_preserves_ledger_and_idempotency(self):
        running, _, _ = self.store.create_or_get("host:user", "ws", "active", "keep this instruction")
        before, usage = self.store.snapshot(self.tid), self.store.usage_summary()
        preview = self.store.prune_logs(time.time() + 1)
        self.assertFalse(preview["executed"])
        self.assertEqual(preview["eligible_tasks"], 1)
        self.assertEqual(self.store.snapshot(self.tid), before)
        self.store.prune_logs(time.time() + 1, execute=True)
        self.assertEqual(self.store.usage_summary(), usage)
        self.assertEqual(self.store.get(running)["instruction"], "keep this instruction")
        self.assertEqual(self.store.get(self.tid)["instruction"], "")
        events = self.store.snapshot(self.tid)["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], before["events"][-1])
        tid, state, created = self.store.create_or_get("host:user", "ws", "key", "PRIVATE-INSTRUCTION")
        self.assertEqual((tid, state, created), (self.tid, "COMPLETED", False))

    def test_export_metadata_default_and_explicit_content(self):
        rows = list(self.store.export_events())
        self.assertNotIn("PRIVATE-OUTPUT", json.dumps(rows))
        full = list(self.store.export_events(task_id=self.tid, include_content=True))
        self.assertIn("PRIVATE-OUTPUT", json.dumps(full))
        self.assertIn("PRIVATE-INSTRUCTION", json.dumps(full))
        self.assertEqual(full[0]["type"], "task_metadata")
        self.assertEqual(full[0]["data"]["workspace"], "ws")
        self.assertEqual(list(self.store.export_events(task_id="not-found")), [])

    def test_export_snapshot_remains_consistent_while_new_events_arrive(self):
        for i in range(600):
            self.store.append(self.tid, "notice", {"code": "fixture", "i": i})
        expected = len(list(self.store.export_events()))
        records = self.store.export_events()
        first = next(records)
        self.store.append(self.tid, "notice", {"code": "later"})
        self.assertEqual(len([first, *records]), expected)

    def test_cli_usage_export_and_no_clobber(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--db", str(self.db), "usage", "--task-id", self.tid]), 0)
        self.assertEqual(json.loads(output.getvalue())["known_total_tokens"], 24)
        target = self.root / "logs.jsonl"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "export", "--output", str(target)]), 0)
        self.assertNotIn("PRIVATE-OUTPUT", target.read_text(encoding="utf-8"))
        original = target.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "export", "--output", str(target)]), 1)
        self.assertEqual(target.read_bytes(), original)
