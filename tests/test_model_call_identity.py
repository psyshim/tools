import unittest
from unittest.mock import Mock
from coding_service.loop import Task, MAX_CALLS_PER_TURN


class ModelCallIdentityTests(unittest.TestCase):
    def test_repeated_ids_across_turns_and_invalid_ids_become_unique_bounded_ids(self):
        seen = set()
        calls = []
        for step in range(2):
            calls.extend(Task._normalize_calls([
                {"id": "same", "name": "list_dir", "args": {}},
                {"id": "same", "name": "list_dir", "args": {}},
                {"id": "x" * 10000, "name": "\ud800", "args": {}},
            ], step, seen))
        self.assertEqual(len({c["id"] for c in calls}), 6)
        self.assertTrue(all(len(c["id"]) <= 128 for c in calls))
        self.assertEqual(calls[2]["name"], "")

    def test_excess_calls_are_not_normalized_at_all(self):
        class MustNotInspect(dict):
            def get(self, *args):
                raise AssertionError("excess calls must not be inspected")
        raw = [{"id": str(i), "name": "list_dir", "args": {}} for i in range(MAX_CALLS_PER_TURN)] + [MustNotInspect()]
        self.assertEqual(len(Task._normalize_calls(raw, 0)), MAX_CALLS_PER_TURN)

    def test_wrong_container_is_failed_not_false_completion(self):
        box = Mock()
        task = Task("user", box, Mock(return_value={"tool_calls": {"name": "apply_proposal"}}), "instruction")
        task.run()
        self.assertEqual(task.state, "FAILED")
        box.call.assert_not_called()
