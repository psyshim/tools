"""A noisy tool must not accumulate its entire output in the agent process."""
import sys
import tempfile
import tracemalloc
import unittest
from code_attach.tools import ToolBox


class BoundedOutputTests(unittest.TestCase):
    def test_64_mib_output_keeps_only_tail_and_bounded_parent_memory(self):
        command = [sys.executable, "-c", "import os; chunk=b'x'*1048576; [os.write(1,chunk) for _ in range(64)]; os.write(1,b'END_MARKER')"]
        with tempfile.TemporaryDirectory() as root:
            tools = ToolBox(root, {"noisy": command}, test_timeout=30)
            tracemalloc.start()
            try:
                result = tools.t_run_tests("noisy")
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertEqual(result["status"], "PASS", result)
        self.assertTrue(result["output"].endswith("END_MARKER"))
        self.assertLessEqual(len(result["output"]), 8000)
        self.assertLess(peak, 4 * 1024 * 1024, f"agent accumulated tool output: peak={peak}")
