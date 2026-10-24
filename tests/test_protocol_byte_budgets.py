"""JSON protocol limits are UTF-8 byte limits, not character counts."""
import unittest
import json
from unittest.mock import Mock

from coding_service.loop import Task
from coding_service.wire import to_protocol_events
from protocol import validate_event


class ProtocolByteBudgetTests(unittest.TestCase):
    def test_large_japanese_message_is_a_valid_bounded_protocol_event(self):
        event = {"type": "assistant", "data": {"text": "日本語" * 30000}}
        converted = to_protocol_events("task", [event])
        self.assertEqual(validate_event(converted[0]), converted[0])
        self.assertLessEqual(len(json.dumps(converted[0]["data"], ensure_ascii=False).encode("utf-8")), 65536)
        self.assertIn("truncated", converted[0]["data"]["text"])
        self.assertEqual(event["data"]["text"], "日本語" * 30000)

    def test_oversize_approval_is_refused_without_truncation_or_effect(self):
        box = Mock()
        box.is_effect.return_value = True
        box.scrub.side_effect = lambda s: s
        box.describe_effect.return_value = ({"kind": "edit", "path": "設定.json", "diff": "変更" * 15000}, "fingerprint")
        task = Task("user", box, None, "instruction", approval_timeout=0.01)
        result = task._execute({"id": "call", "name": "apply_proposal", "args": {}})
        self.assertEqual(result["status"], "ERROR")
        self.assertFalse(any(e["type"] == "approval_required" for e in task.events))
        box.call.assert_not_called()

    def test_large_derived_diagnostics_cannot_break_test_result_envelope(self):
        event = {"type": "tool_result", "data": {"tool": "run_tests", "result": {"status": "FAIL", "exit_code": 1,
                 "output": "失敗" * 8000, "parsed": {"diagnostics": [{"message": "詳細" * 1000}] * 40}}}}
        for converted in to_protocol_events("task", [event]):
            validate_event(converted)

    def test_escaping_and_lone_surrogates_are_bounded_without_invalid_json(self):
        event = {"type": "assistant", "data": {"text": '\\"\n' * 30000 + '\ud800'}}
        for converted in to_protocol_events("task", [event]):
            validate_event(converted)

    def test_validator_rejects_multibyte_oversize_and_nonfinite_data(self):
        from protocol import ProtocolError
        for data in ({"text": "あ" * 30000}, {"text": "ok", "number": float("nan")}):
            with self.assertRaises(ProtocolError):
                validate_event({"protocol": "1.0", "task_id": "task", "event_id": "1", "type": "assistant_message", "data": data})
