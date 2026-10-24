"""Regression tests for read restrictions and fault-state preservation."""
import tempfile
import unittest
from pathlib import Path
from coding_agent.workspace import Workspace
from coding_agent.fault_service import ScenarioStore

class BoundaryRegressionTests(unittest.TestCase):
    def test_sensitive_files_hidden_and_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("TOKEN=secret")
            (root / "credentials.json").write_text("{}")
            (root / "visible.py").write_text("x = 1")
            ws = Workspace(root)
            self.assertEqual(ws.list_directory(), ["visible.py"])
            for name in (".env", "credentials.json", ".git/config"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    ws.read_file(name)

    def test_scenario_change_does_not_destroy_previous_tasks(self):
        state = ScenarioStore()
        state.tasks["t1"] = {"task_id": "t1"}
        state.keys["key"] = ("digest", "t1")
        state.configure("unavailable")
        self.assertEqual(state.tasks["t1"]["task_id"], "t1")
        self.assertEqual(state.keys["key"][1], "t1")
        state.reset()
        self.assertFalse(state.tasks)
        self.assertFalse(state.keys)

if __name__ == "__main__":
    unittest.main()
