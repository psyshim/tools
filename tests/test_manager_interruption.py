"""Deterministic races and unknown effects must survive terminal/restart handling."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from coding_service.loop import Task
from coding_service.manager import DurableManager
from coding_service.store import TaskStore


class ManagerInterruptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.store.close)
        self.manager = DurableManager(self.store, {"device": Mock()},
            {"host": {"workspaces": {"ws": {"device": "device", "workspace": "ws"}}}}, Mock())

    def test_cancel_while_device_session_is_opening_does_not_run_model(self):
        entered, release = threading.Event(), threading.Event()
        box = Mock()
        box.schemas.return_value = []
        def opening(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release device")
            return box
        self.manager.model.return_value = {"text": "done"}
        with patch("coding_service.manager.RemoteToolbox", side_effect=opening):
            tid, _ = self.manager.start("host:user", "ws", "key", "instruction")
            try:
                self.assertTrue(entered.wait(5))
                self.manager.cancel("host:user", tid)
            finally:
                release.set()
                self.manager._threads[tid].join(5)
        self.assertFalse(self.manager._threads[tid].is_alive())
        self.manager.model.assert_not_called()
        self.assertEqual(self.store.get(tid)["state"], "CANCELLED")

    def test_unknown_effect_stops_remaining_calls_and_model_turns(self):
        box = Mock()
        box.schemas.return_value = []
        box.is_effect.return_value = False  # approval is tested elsewhere
        box.call.return_value = {"status": "UNKNOWN_OUTCOME", "op_id": "operation"}
        model = Mock(return_value={"tool_calls": [
            {"id": "one", "name": "apply_proposal", "args": {}},
            {"id": "two", "name": "apply_proposal", "args": {}},
        ]})
        task = Task("user", box, model, "instruction")
        task.run()
        self.assertEqual(task.state, "NEEDS_RECONCILIATION")
        self.assertEqual(box.call.call_count, 1)
        self.assertEqual(model.call_count, 1)

    def test_unknown_effect_followed_by_crash_is_not_downgraded_to_failure(self):
        tid, _, _ = self.store.create_or_get("host:user", "ws", "key", "instruction")
        self.store.append(tid, "notice", {"code": "worker_started"})
        self.store.append(tid, "approval_decision", {"action_id": "one", "approve": True})
        self.store.append(tid, "tool_activity", {"tool": "apply_proposal", "status": "UNKNOWN_OUTCOME"})
        result = self.manager.recover()
        self.assertIn(tid, result["needs_reconciliation"])
        self.assertEqual(self.store.get(tid)["state"], "NEEDS_RECONCILIATION")

    def test_unknown_sink_cannot_be_hidden_by_failure_or_cancel(self):
        for terminal in ("COMPLETED", "CANCELLED", "FAILED"):
            with self.subTest(terminal=terminal):
                tid, _, _ = self.store.create_or_get("host:user", "ws", terminal, "instruction")
                sink = self.manager._sink(tid)
                sink(tid, {"type": "tool_result", "data": {"tool": "apply_proposal", "result": {"status": "UNKNOWN_OUTCOME"}}})
                sink(tid, {"type": "state", "data": {"state": terminal}})
                self.assertEqual(self.store.get(tid)["state"], "NEEDS_RECONCILIATION")

    def test_restart_does_not_start_a_previously_cancelled_accepted_task(self):
        tid, _, _ = self.store.create_or_get("host:user", "ws", "key", "instruction")
        self.store.append(tid, "notice", {"code": "cancel_requested"})
        with patch.object(self.manager, "_spawn") as spawn:
            result = self.manager.recover()
        spawn.assert_not_called()
        self.assertIn(tid, result["interrupted"])
        self.assertEqual(self.store.get(tid)["state"], "CANCELLED")
