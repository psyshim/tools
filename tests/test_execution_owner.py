"""Cross-process single-executor regression."""
import multiprocessing
import tempfile
import unittest
from coding_agent.execution_owner import SingleExecutor, OwnerBusy

def contender(root, channel):
    try:
        with SingleExecutor(root):
            channel.put("acquired")
    except OwnerBusy:
        channel.put("busy")

class SingleExecutorTests(unittest.TestCase):
    def test_second_process_is_rejected_then_can_restart(self):
        with tempfile.TemporaryDirectory() as root:
            ctx = multiprocessing.get_context("spawn")
            with SingleExecutor(root):
                queue = ctx.Queue()
                process = ctx.Process(target=contender, args=(root, queue))
                process.start()
                process.join(5)
                self.assertEqual(process.exitcode, 0)
                self.assertEqual(queue.get(timeout=1), "busy")
            with SingleExecutor(root):
                pass

if __name__ == "__main__":
    unittest.main()
