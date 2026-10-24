"""An exception after dispatch cannot establish that no local effect happened."""
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from coding_agent.journal import OperationJournal
from device_agent.core import DeviceService


class DeviceEffectUncertaintyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = object.__new__(DeviceService)
        self.service.journal = OperationJournal(self.root / "ops.db")
        self.addCleanup(self.service.journal.close)
        self.service._jlock = threading.Lock()
        self.box = Mock()
        self.entry = {"workspace": SimpleNamespace(id="ws"), "toolbox": self.box}

    def call(self):
        return self.service._effect(self.entry, "session", "apply_proposal", {"proposal_id": "p"}, "op")

    def test_write_then_exception_stays_unknown_and_retry_does_not_repeat(self):
        def effect(*_):
            (self.root / "changed.txt").write_text("changed", encoding="utf-8")
            raise OSError("failure after writing")
        self.box.call.side_effect = effect
        result = self.call()
        self.assertTrue(result.get("unknown_outcome"), result)
        self.assertEqual(self.service.operation("op")["state"], "UNKNOWN")
        self.assertEqual((self.root / "changed.txt").read_text(), "changed")
        self.assertTrue(self.call()["unknown_outcome"])
        self.assertEqual(self.box.call.call_count, 1)

    def test_explicit_partial_outcome_does_not_become_failed_or_succeeded(self):
        self.box.call.return_value = {"status": "NEEDS_RECONCILIATION", "inconsistent_paths": ["a.py"]}
        result = self.call()
        self.assertTrue(result.get("unknown_outcome"), result)
        self.assertEqual(result["result"]["inconsistent_paths"], ["a.py"])
        self.assertEqual(self.service.operation("op")["state"], "UNKNOWN")

    def test_receipt_storage_failure_is_reported_unknown(self):
        self.box.call.return_value = {"status": "APPLIED"}
        with patch.object(self.service.journal, "reconcile", side_effect=sqlite3.OperationalError("disk full")):
            result = self.call()
        self.assertTrue(result.get("unknown_outcome"), result)
        self.assertEqual(self.service.operation("op")["state"], "UNKNOWN")
        self.assertTrue(self.call()["unknown_outcome"])
        self.assertEqual(self.box.call.call_count, 1)
