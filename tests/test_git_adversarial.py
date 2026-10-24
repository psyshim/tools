"""Adversarial local repositories. No operation ever targets the source checkout."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from coding_agent.git_ops import GitOperations, _run, _run_commit


class GitAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        _run(["git", "init"], self.root)
        (self.root / "a.txt").write_bytes(b"old\n")
        _run(["git", "add", "--", "a.txt"], self.root)
        _run_commit(["git", "commit", "-m", "initial"], self.root)
        self.ops = GitOperations(self.root)

    def proposal(self):
        (self.root / "a.txt").write_bytes(b"new\n")
        return self.ops.plan_commit(["a.txt"], "selected fixture change")

    def test_other_writers_index_lock_is_not_removed(self):
        p = self.proposal()
        lock = self.root / ".git/index.lock"
        lock.write_bytes(b"other writer")
        self.assertEqual(self.ops.apply(p)["status"], "CONFLICT")
        self.assertEqual(lock.read_bytes(), b"other writer")
        lock.unlink()

    def test_branch_switch_with_same_head_is_detected(self):
        p = self.proposal()
        _run(["git", "branch", "other"], self.root)
        _run(["git", "symbolic-ref", "HEAD", "refs/heads/other"], self.root)
        result = self.ops.apply(p)
        self.assertEqual(result["status"], "CONFLICT")
        self.assertIn("branch", result["reason"])

    def test_deleted_file_reappears_before_approval(self):
        (self.root / "a.txt").unlink()
        p = self.ops.plan_commit(["a.txt"], "remove file")
        (self.root / "a.txt").write_bytes(b"human restored")
        self.assertEqual(self.ops.apply(p)["status"], "CONFLICT")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"human restored")

    def test_broad_directory_commit_is_rejected(self):
        (self.root / "folder").mkdir()
        with self.assertRaises(ValueError):
            self.ops.plan_commit(["folder"], "too broad")

    def test_in_progress_merge_is_refused(self):
        (self.root / ".git/MERGE_HEAD").write_text("0" * 40)
        with self.assertRaises(ValueError):
            self.proposal()

    def test_git_directory_environment_cannot_redirect_repository(self):
        p = self.proposal()
        with patch.dict(os.environ, {"GIT_DIR": str(self.root / "nonexistent"), "GIT_WORK_TREE": str(self.root / "wrong")}):
            result = self.ops.apply(p)
        self.assertEqual(result["status"], "APPLIED", result)
        self.assertFalse(_run(["git", "diff", "--cached", "--name-only"], self.root).strip())

    def test_hostile_filters_are_not_executed_by_read_or_commit(self):
        (self.root / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
        # If accidentally executed, this makes Git fail immediately. A marker
        # command is not needed to prove the filter cannot affect the result.
        _run(["git", "config", "filter.evil.clean", "exit 99"], self.root)
        _run(["git", "config", "filter.evil.process", "exit 99"], self.root)
        _run(["git", "config", "filter.evil.required", "true"], self.root)
        p = self.proposal()
        self.assertIn("new", self.ops.inspect("diff")["diff"])
        self.assertEqual(self.ops.apply(p)["status"], "APPLIED")

    def test_unknown_publish_outcome_is_never_reported_as_success(self):
        p = self.proposal()
        real_run = _run
        def uncertain(argv, cwd, *args, **kwargs):
            if "update-ref" in argv:
                raise RuntimeError("connection lost after publish attempt")
            return real_run(argv, cwd, *args, **kwargs)
        with patch("coding_agent.git_ops._run", side_effect=uncertain):
            result = self.ops.apply(p)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertTrue((self.root / ".git/index.lock").exists())
        (self.root / ".git/index.lock").unlink()

    def test_autocrlf_commit_is_clean_without_rewriting_worktree(self):
        _run(["git", "config", "core.autocrlf", "true"], self.root)
        raw = b"new\r\nWindows line\r\n"
        (self.root / "a.txt").write_bytes(raw)
        p = self.ops.plan_commit(["a.txt"], "Windows newline fixture")
        self.assertEqual(self.ops.apply(p)["status"], "APPLIED")
        self.assertEqual((self.root / "a.txt").read_bytes(), raw)
        self.assertFalse(self.ops.inspect("status")["entries"])

    def test_changed_attributes_cannot_change_approved_commit_content(self):
        (self.root / "a.txt").write_bytes(b"new\r\n")
        _run(["git", "config", "core.autocrlf", "false"], self.root)
        p = self.ops.plan_commit(["a.txt"], "attribute snapshot")
        (self.root / ".gitattributes").write_text("a.txt text eol=lf\n", encoding="utf-8")
        result = self.ops.apply(p)
        self.assertEqual(result["status"], "CONFLICT")
        self.assertFalse(_run(["git", "diff", "--cached", "--name-only"], self.root).strip())
