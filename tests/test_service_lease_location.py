"""Places an OS lease cannot protect are refused at start (exit code 5), not documented away. The code 4 / 5 split lets a supervisor
tell 'retry later' from 'inspect the store' from 'fix the path'."""
import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_service.__main__ import main
from coding_service.lease import LeaseError, LeaseUnsupported, ServiceAlreadyRunning, ServiceLease, _is_remote, check_location


class Locations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_an_ordinary_local_database_is_leased_and_a_second_runtime_is_refused(self):
        db = self.root / "tasks.db"
        first = ServiceLease(db)
        self.addCleanup(first.close)
        with self.assertRaises(ServiceAlreadyRunning):
            ServiceLease(db)

    def test_a_hard_linked_database_is_refused_before_any_lock_is_taken(self):
        db = self.root / "tasks.db"
        sqlite3.connect(db).close()
        db.write_bytes(b"x")
        try:
            os.link(db, self.root / "alias.db")
        except OSError:
            self.skipTest("hard links are not available on this filesystem")
        with self.assertRaises(LeaseUnsupported):
            ServiceLease(db)
        self.assertFalse((self.root / "tasks.db.service.lock").exists(), "refusal must not leave a lock file behind")

    def test_a_network_location_is_refused(self):
        with patch("coding_service.lease._is_remote", return_value=True):
            with self.assertRaises(LeaseUnsupported) as caught:
                check_location(self.root / "tasks.db")
        self.assertIn("network", str(caught.exception))

    @unittest.skipUnless(os.name == "nt", "UNC paths are a Windows form")
    def test_unc_paths_are_recognised_as_remote(self):
        self.assertTrue(_is_remote("\\\\server\\share\\tasks.db"))
        self.assertFalse(_is_remote(self.root / "tasks.db"))

    def test_both_refusals_are_lease_errors_and_not_confusable_with_a_store_failure(self):
        self.assertTrue(issubclass(ServiceAlreadyRunning, LeaseError) and issubclass(LeaseUnsupported, LeaseError))
        for failure, fragment in ((ServiceAlreadyRunning("x"), "another service owns"), (LeaseUnsupported("on a network location"), "network location")):
            stderr = io.StringIO()
            with patch("coding_service.__main__.load_config"), patch("coding_service.__main__.build_manager", side_effect=failure), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(["--config", "fixture.json"]), 5)
            self.assertIn(fragment, stderr.getvalue())
        stderr = io.StringIO()
        with patch("coding_service.__main__.load_config"), patch("coding_service.__main__.build_manager", side_effect=PermissionError("locked")), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(["--config", "fixture.json"]), 4, "a store that cannot be read stays code 4")


if __name__ == "__main__":
    unittest.main()
