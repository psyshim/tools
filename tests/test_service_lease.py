"""Service ownership is OS-backed, without locking out local administration."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from coding_service.app import build_manager
from coding_service.lease import ServiceAlreadyRunning, ServiceLease
from coding_service.manager import DurableManager
from coding_service.store import TaskStore
from coding_service.backup import backup_store


class ServiceLeaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "state.db"

    def test_another_process_is_refused_and_hard_exit_releases_ownership(self):
        code = "from coding_service.lease import ServiceLease; import sys; lease=ServiceLease(sys.argv[1]); print('held',flush=True); sys.stdin.read()"
        proc = subprocess.Popen([sys.executable, "-c", code, str(self.db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        try:
            self.assertEqual(proc.stdout.readline().strip(), "held")
            with self.assertRaises(ServiceAlreadyRunning):
                ServiceLease(self.db)
            proc.kill()
            proc.wait(timeout=10)
            lease = ServiceLease(self.db)
            lease.close()
            self.assertTrue(lease.path.exists(), "never unlink lock files and split ownership between inodes")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=10)

    def test_usage_export_backup_and_prune_remain_available_while_owned(self):
        lease = ServiceLease(self.db)
        self.addCleanup(lease.close)
        store = TaskStore(self.db)
        self.addCleanup(store.close)
        self.assertEqual(store.usage_summary()["calls"], 0)
        from coding_service.manage import export_snapshot
        export_snapshot(store, self.db.parent / "events.jsonl")
        backup_store(store, self.db.parent / "backup.db")
        store.prune_logs(before=0)
        other = ServiceLease(self.db.parent / "other.db")
        other.close()

    def test_refusal_happens_before_store_model_or_recovery(self):
        lease = ServiceLease(self.db)
        self.addCleanup(lease.close)
        with patch("coding_service.app.TaskStore") as store, patch("coding_service.app.load_model") as model:
            with self.assertRaises(ServiceAlreadyRunning):
                build_manager(SimpleNamespace(db_path=self.db), {})
        store.assert_not_called()
        model.assert_not_called()
        self.assertFalse(self.db.exists())

    def test_failed_model_construction_releases_store_and_lease(self):
        config = SimpleNamespace(db_path=self.db, devices={}, hosts={}, model="bad", allow_mutation=False, max_tasks=1)
        with patch("coding_service.app.load_model", side_effect=ValueError("bad model")), self.assertRaises(ValueError):
            build_manager(config, {})
        lease = ServiceLease(self.db)
        lease.close()

    def test_shutdown_retains_lease_until_blocked_worker_really_finishes(self):
        lease = ServiceLease(self.db)
        self.addCleanup(lease.close)
        manager = DurableManager(Mock(), {}, {}, Mock())
        manager._service_lease = lease
        release = threading.Event()
        worker = threading.Thread(target=release.wait)
        worker.start()
        manager._threads["task"] = worker
        try:
            manager.shutdown(timeout=0)
            with self.assertRaises(ServiceAlreadyRunning):
                ServiceLease(self.db)
        finally:
            release.set()
            worker.join(5)
        manager.shutdown(timeout=0)
        next_owner = ServiceLease(self.db)
        next_owner.close()

    def test_cli_reports_ownership_refusal_without_traceback(self):
        from coding_service.__main__ import main
        stderr = io.StringIO()
        with patch("coding_service.__main__.load_config"), patch("coding_service.__main__.build_manager", side_effect=ServiceAlreadyRunning()), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(["--config", "fixture.json"]), 4)
        self.assertIn("another service owns", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_second_real_service_cannot_recover_a_live_task(self):
        from stack_harness import Stack
        from test_service_recovery import db_rows, wait_for
        stack = Stack(self, env_extra={"MODEL_RELEASE_FLAG": str(self.db.parent / "release")}).start()
        gateway = stack.gateway()
        gateway.start_coding("alice", "shop", "wait for ownership test", conversation_id="lease", message_id="one")
        wait_for(lambda: db_rows(stack, "SELECT call_id FROM model_usage"))
        env = {**os.environ, "PYTHONPATH": str(stack.service_tree), "SVC_DEVICE_TOKEN": stack.device_token}
        ready = stack.base / "second.ready"
        second = subprocess.run([sys.executable, "-m", "coding_service", "--config", str(stack.base / "service.json"),
                                 "--port", "0", "--ready-file", str(ready)], cwd=stack.service_tree,
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 4, second.stderr)
        self.assertIn("another service owns", second.stderr)
        self.assertFalse(ready.exists())
        self.assertEqual(db_rows(stack, "SELECT state FROM tasks"), [("RUNNING",)])
        self.assertEqual(len(db_rows(stack, "SELECT call_id FROM model_usage")), 1)
        stack.stop("service", kill=True)
        stack.start_service()
        self.assertEqual(db_rows(stack, "SELECT state FROM tasks"), [("FAILED",)])
