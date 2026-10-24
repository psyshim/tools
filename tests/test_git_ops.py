"""Tests for coding_agent.git_ops — uses real temporary git repos."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from coding_agent.git_ops import GitOperations, _scrub, _validate_relpath


def _git(args, cwd):
    """Helper: run a git command in *cwd*."""
    env = {**os.environ, "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t"}
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True,
        text=True, env=env, shell=False, timeout=30,
    )


def _init_repo(tmp: Path):
    """Create a git repo with one committed file."""
    _git(["init"], tmp)
    _git(["-c", "core.hooksPath=/dev/null", "config", "user.name", "test"], tmp)
    _git(["-c", "core.hooksPath=/dev/null", "config", "user.email", "t@t"], tmp)
    (tmp / "hello.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "hello.txt"], tmp)
    _git(["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
          "commit", "-m", "initial"], tmp)


class TestInspect(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _init_repo(self.tmp)
        self.ops = GitOperations(self.tmp)

    def test_status_clean(self):
        r = self.ops.inspect("status")
        self.assertEqual(r["action"], "status")
        self.assertIsInstance(r["entries"], list)

    def test_status_dirty(self):
        (self.tmp / "hello.txt").write_text("changed\n")
        r = self.ops.inspect("status")
        self.assertTrue(any("hello" in e for e in r["entries"]))

    def test_diff(self):
        (self.tmp / "hello.txt").write_text("changed\n")
        r = self.ops.inspect("diff")
        self.assertIn("changed", r["diff"])

    def test_staged_diff(self):
        (self.tmp / "hello.txt").write_text("staged\n")
        _git(["add", "hello.txt"], self.tmp)
        r = self.ops.inspect("staged_diff")
        self.assertIn("staged", r["diff"])
        # Clean up the staged change for other tests
        _git(["reset", "HEAD", "hello.txt"], self.tmp)
        _git(["checkout", "--", "hello.txt"], self.tmp)

    def test_log(self):
        r = self.ops.inspect("log")
        self.assertEqual(r["action"], "log")
        self.assertTrue(len(r["entries"]) >= 1)
        self.assertIn("initial", r["entries"][0])

    def test_branches(self):
        r = self.ops.inspect("branches")
        self.assertIn(r["current"], r["branches"])

    def test_unsupported_action(self):
        with self.assertRaises(ValueError):
            self.ops.inspect("cherry-pick")

    def test_diff_with_path(self):
        (self.tmp / "hello.txt").write_text("changed\n")
        (self.tmp / "other.txt").write_text("new\n")
        r = self.ops.inspect("diff", path="hello.txt")
        self.assertIn("changed", r["diff"])


class TestPlanCommitAndApply(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _init_repo(self.tmp)
        self.ops = GitOperations(self.tmp)

    def test_plan_and_apply(self):
        (self.tmp / "hello.txt").write_text("updated\n")
        plan = self.ops.plan_commit(["hello.txt"], "update hello")
        self.assertEqual(plan["type"], "commit_plan")
        self.assertIn("hello.txt", plan["paths"])
        self.assertIn("head", plan)
        self.assertIn("branch", plan)

        result = self.ops.apply(plan)
        self.assertEqual(result["status"], "APPLIED")
        self.assertIn("commit", result)
        # Verify SHA changed
        self.assertNotEqual(result["commit"], plan["head"])

    def test_commit_sha_valid(self):
        (self.tmp / "hello.txt").write_text("v2\n")
        plan = self.ops.plan_commit(["hello.txt"], "v2")
        result = self.ops.apply(plan)
        self.assertEqual(result["status"], "APPLIED")
        # The commit SHA should be a 40-char hex string
        self.assertEqual(len(result["commit"]), 40)
        self.assertTrue(all(c in "0123456789abcdef" for c in result["commit"]))

    def test_selective_paths(self):
        """Only the selected path is committed; unrelated changes stay unstaged."""
        (self.tmp / "hello.txt").write_text("commit me\n")
        (self.tmp / "other.txt").write_text("leave me alone\n")
        plan = self.ops.plan_commit(["hello.txt"], "selective")
        result = self.ops.apply(plan)
        self.assertEqual(result["status"], "APPLIED")
        # other.txt should still be untracked
        st = _git(["status", "--porcelain"], self.tmp)
        self.assertIn("other.txt", st.stdout)

    def test_new_file_commit(self):
        (self.tmp / "brand_new.txt").write_text("fresh\n")
        plan = self.ops.plan_commit(["brand_new.txt"], "add new file")
        result = self.ops.apply(plan)
        self.assertEqual(result["status"], "APPLIED")

    def test_stale_head_conflict(self):
        """If HEAD moves between plan and apply, we get CONFLICT."""
        (self.tmp / "hello.txt").write_text("v2\n")
        plan = self.ops.plan_commit(["hello.txt"], "v2")

        # Simulate HEAD moving: make another commit
        (self.tmp / "hello.txt").write_text("hello\n")  # revert content
        (self.tmp / "sneaky.txt").write_text("surprise\n")
        _git(["add", "sneaky.txt"], self.tmp)
        _git(["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
              "commit", "-m", "sneak"], self.tmp)

        (self.tmp / "hello.txt").write_text("v2\n")  # re-apply change
        result = self.ops.apply(plan)
        self.assertEqual(result["status"], "CONFLICT")
        self.assertIn("HEAD changed", result["reason"])

    def test_stale_file_conflict(self):
        """If a target file changes between plan and apply, we get CONFLICT."""
        (self.tmp / "hello.txt").write_text("v2\n")
        plan = self.ops.plan_commit(["hello.txt"], "v2")

        # Someone else edits the same file
        (self.tmp / "hello.txt").write_text("v3 by someone else\n")

        result = self.ops.apply(plan)
        self.assertEqual(result["status"], "CONFLICT")
        self.assertIn("file changed", result["reason"])

    def test_existing_staged_rejected(self):
        """plan_commit refuses when there are already staged changes."""
        (self.tmp / "hello.txt").write_text("staged\n")
        _git(["add", "hello.txt"], self.tmp)
        with self.assertRaises(RuntimeError) as ctx:
            self.ops.plan_commit(["hello.txt"], "should fail")
        self.assertIn("staged", str(ctx.exception).lower())

    def test_staged_appears_before_apply(self):
        """apply returns CONFLICT if staged changes appear after plan."""
        (self.tmp / "hello.txt").write_text("v2\n")
        plan = self.ops.plan_commit(["hello.txt"], "v2")

        # Stage something else
        (self.tmp / "rogue.txt").write_text("rogue\n")
        _git(["add", "rogue.txt"], self.tmp)

        result = self.ops.apply(plan)
        self.assertEqual(result["status"], "CONFLICT")
        self.assertIn("staged", result["reason"].lower())


class TestPathSecurity(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _init_repo(self.tmp)
        self.ops = GitOperations(self.tmp)

    def test_absolute_path_rejected(self):
        with self.assertRaises(ValueError):
            self.ops.plan_commit(["/etc/passwd"], "steal")

    def test_traversal_rejected(self):
        with self.assertRaises(ValueError):
            self.ops.plan_commit(["../../../etc/passwd"], "escape")

    def test_dot_git_rejected(self):
        with self.assertRaises(ValueError):
            self.ops.plan_commit([".git/config"], "tamper")

    def test_dotenv_rejected(self):
        with self.assertRaises(ValueError):
            self.ops.plan_commit([".env"], "leak")

    def test_inspect_path_validation(self):
        with self.assertRaises(ValueError):
            self.ops.inspect("diff", path="/etc/shadow")


class TestArgumentInjection(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _init_repo(self.tmp)
        self.ops = GitOperations(self.tmp)

    def test_dash_arg_in_path(self):
        """Paths starting with - shouldn't be interpreted as flags (-- separator protects)."""
        # plan_commit validates the path; a flag-like name is just a filename
        # The -- in the git add command prevents flag interpretation.
        # This should fail validation or just work as a filename, not inject flags.
        (self.tmp / "-n").write_text("trick\n")
        # Should not crash or inject
        plan = self.ops.plan_commit(["-n"], "test dash file")
        self.assertIn("-n", plan["paths"])

    def test_unsupported_action_string(self):
        with self.assertRaises(ValueError):
            self.ops.inspect("push")


class TestScrubbing(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _init_repo(self.tmp)
        self.ops = GitOperations(self.tmp)

    def test_root_scrubbed_from_diff(self):
        (self.tmp / "hello.txt").write_text("has path " + str(self.tmp) + " inside\n")
        r = self.ops.inspect("diff")
        self.assertNotIn(str(self.tmp), r["diff"])

    def test_plan_diff_scrubbed(self):
        (self.tmp / "hello.txt").write_text("contains " + str(self.tmp) + "\n")
        plan = self.ops.plan_commit(["hello.txt"], "test scrub")
        self.assertNotIn(str(self.tmp), plan["diff"])

    def test_plan_no_absolute_root(self):
        (self.tmp / "hello.txt").write_text("v2\n")
        plan = self.ops.plan_commit(["hello.txt"], "msg")
        plan_json = str(plan)
        self.assertNotIn(str(self.tmp), plan_json)


class TestConstructorValidation(unittest.TestCase):
    def test_non_git_dir_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        with self.assertRaises(ValueError):
            GitOperations(tmp)

    def test_invalid_plan_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        _init_repo(tmp)
        ops = GitOperations(tmp)
        with self.assertRaises(ValueError):
            ops.apply({"type": "wrong"})
        with self.assertRaises(ValueError):
            ops.apply("not a dict")


class TestUnsupportedOps(unittest.TestCase):
    """Verify that dangerous operations are not exposed."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _init_repo(self.tmp)
        self.ops = GitOperations(self.tmp)

    def test_no_push_action(self):
        with self.assertRaises(ValueError):
            self.ops.inspect("push")

    def test_no_reset_action(self):
        with self.assertRaises(ValueError):
            self.ops.inspect("reset")

    def test_no_merge_action(self):
        with self.assertRaises(ValueError):
            self.ops.inspect("merge")

    def test_no_rebase_action(self):
        with self.assertRaises(ValueError):
            self.ops.inspect("rebase")

    def test_no_clean_action(self):
        with self.assertRaises(ValueError):
            self.ops.inspect("clean")

    def test_no_arbitrary_action(self):
        with self.assertRaises(ValueError):
            self.ops.inspect("!rm -rf /")


if __name__ == "__main__":
    unittest.main()
