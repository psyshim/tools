import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from coding_service.errors import LogUnavailable
from coding_service.manager import DurableManager
from coding_service.protocol_router import make_protocol_router_for
from coding_service.store import TaskStore
from coding_service.usage import normalize_usage


class CorruptStateRecordsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.store.close)
        self.manager = DurableManager(self.store, {"dev": Mock()},
            {"host": {"workspaces": {"ws": {"device": "dev", "workspace": "ws"}}}}, Mock())

    def task(self, key):
        return self.store.create_or_get("host:user", "ws", key, "instruction")[0]

    def test_bad_json_or_missing_notice_fields_never_resumes_work_or_returns_500(self):
        app = FastAPI()
        app.include_router(make_protocol_router_for(self.manager, lambda _: "host:user", lambda *_: "ws"))
        with TestClient(app) as client:
            for i, raw in enumerate(("{", "[]", '{"code":"a","code":"b"}', '{"code":NaN}', '{}')):
                tid = self.task(str(i))
                self.store._db.execute("UPDATE events SET data=? WHERE task_id=?", (raw, tid))
                with self.subTest(raw=raw):
                    with self.assertRaises(LogUnavailable), patch.object(self.manager, "_spawn") as spawn:
                        self.manager.recover()
                    spawn.assert_not_called()
                    response = client.get(f"/tasks/{tid}/events")
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json()["error"]["code"], "LOG_UNAVAILABLE")
                # Remove this hostile row so the next subcase proves its own failure.
                self.store._db.execute("UPDATE events SET data=? WHERE task_id=?", ('{"code":"task_accepted"}', tid))

    def test_all_histories_are_validated_before_any_recovered_worker_starts(self):
        first, damaged = self.task("first"), self.task("damaged")
        self.store._db.execute("UPDATE events SET data='{' WHERE task_id=?", (damaged,))
        with patch.object(self.manager, "_spawn") as spawn, self.assertRaises(LogUnavailable):
            self.manager.recover()
        spawn.assert_not_called()
        self.assertEqual(self.store.get(first)["state"], "ACCEPTED")

    def test_missing_worker_marker_in_running_history_is_not_proof_of_never_started(self):
        tid = self.task("running")
        self.store.append(tid, "state", {"state": "RUNNING"})
        with patch.object(self.manager, "_spawn") as spawn:
            result = self.manager.recover()
        spawn.assert_not_called()
        self.assertIn(tid, result["needs_reconciliation"])
        self.assertEqual(self.store.get(tid)["state"], "NEEDS_RECONCILIATION")

    def test_malformed_usage_record_is_not_reported_as_zero_or_as_known_total(self):
        tid = self.task("usage")
        good = {"code": "model_usage", "model_call_id": "call", **normalize_usage({"input_tokens": 1, "output_tokens": 2})}
        self.store.append(tid, "notice", good)
        for bad in ("{", "[]", "{}", json.dumps({**good, "total_tokens": 999})):
            self.store._db.execute("UPDATE model_usage SET data=?", (bad,))
            with self.subTest(bad=bad), self.assertRaises(LogUnavailable):
                self.store.usage_summary()

    def test_startup_refuses_unreadable_history_with_a_clear_exit_and_closes_store(self):
        from coding_service.__main__ import main
        manager = Mock()
        manager.recover.side_effect = LogUnavailable("invalid JSON")
        stderr = io.StringIO()
        with patch("coding_service.__main__.load_config", return_value=Mock()), patch("coding_service.__main__.build_manager", return_value=manager), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(["--config", "fixture.json"]), 4)
        self.assertIn("task history cannot be read", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        manager.shutdown.assert_called_once()
        manager.store.close.assert_called_once()
