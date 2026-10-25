"""Cross-process single-executor regression."""
import multiprocessing
import os
import tempfile
import unittest
from unittest import mock
from coding_agent.execution_owner import SingleExecutor, OwnerBusy

def contender(root, channel):
    try:
        with SingleExecutor(root):
            channel.put("acquired")
    except OwnerBusy:
        channel.put("busy")

class SingleExecutorTests(unittest.TestCase):
    def test_second_process_is_rejected_then_can_restart(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {"CODING_AGENT_STATE_DIR": state}):
            ctx = multiprocessing.get_context("spawn")
            with SingleExecutor(root):
                queue = ctx.Queue()
                process = ctx.Process(target=contender, args=(root, queue))
                process.start()
                process.join(60)          # a hang guard: a spawned interpreter needs tens of seconds to start on a heavily loaded Windows machine
                if process.exitcode is None:
                    process.terminate()
                    process.join(10)
                    self.fail("the contender process did not finish: it neither acquired nor reported busy")
                self.assertEqual(process.exitcode, 0)
                self.assertEqual(queue.get(timeout=10), "busy")
            with SingleExecutor(root):
                pass

    def test_lock_file_is_never_created_inside_workspace(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {"CODING_AGENT_STATE_DIR": state}):
            with SingleExecutor(root):
                self.assertEqual(os.listdir(root), [])
                self.assertTrue(os.listdir(state))

if __name__ == "__main__":
    unittest.main()
