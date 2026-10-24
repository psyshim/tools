"""Malformed model JSON must produce a retryable error, never default execution."""
import json
import unittest
from unittest.mock import Mock

from coding_service.providers import from_openai, from_anthropic
from coding_service.providers import from_openai_response
from coding_service.loop import Task


class JsonCorruptionTests(unittest.TestCase):
    def convert(self, value):
        return from_openai({"tool_calls": [{"id": "x", "function": {
            "name": "list_dir", "arguments": value}}]})["tool_calls"][0]

    def test_hostile_json_is_rejected_without_dispatch_or_approval(self):
        values = [None, {}, 42, "", "{", '{"path":"a",}',
                  '```json\n{}\n```', '{} trailing', '[]', 'null',
                  '{"path":"a","path":"b"}', '{"x":{"a":1,"a":2}}',
                  '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}',
                  '{"x":"\\ud800"}', '[' * 2000 + ']' * 2000,
                  '{"x":"' + 'a' * 262144 + '"}']
        for value in values:
            with self.subTest(value=str(value)[:60]):
                call = self.convert(value)
                box = Mock()
                box.scrub.side_effect = lambda value: value
                task = Task("user", box, None, "test")
                self.assertEqual(task._execute(call)["status"], "ERROR")
                box.call.assert_not_called()
                box.is_effect.assert_not_called()

    def test_valid_unicode_and_empty_object_survive(self):
        for args in ({}, {"path": "日本語.json", "text": 'quote"\n\\'}):
            self.assertEqual(self.convert(json.dumps(args))["args"], args)

    def test_shortened_history_remains_valid_json_and_full_log_is_untouched(self):
        from coding_service.loop import KEEP_FULL_TOOL_RESULTS
        task = Task("user", Mock(), None, "test")
        content = json.dumps({"text": '日本語"\\\n' * 1000}, ensure_ascii=False)
        for i in range(KEEP_FULL_TOOL_RESULTS + 1):
            task.messages.append({"role": "tool", "tool_call_id": str(i), "name": "read_file", "content": content})
        view = [m for m in task._model_view() if m["role"] == "tool"]
        self.assertTrue(json.loads(view[0]["content"])["truncated"])
        self.assertLess(len(view[0]["content"]), 1000)
        self.assertEqual(json.loads(view[-1]["content"]), json.loads(content))
        self.assertEqual(task.messages[2]["content"], content)

    def test_anthropic_invalid_objects_are_rejected(self):
        for value in (None, [], "{}", {"x": float("nan")}):
            call = from_anthropic([{"type": "tool_use", "input": value}])["tool_calls"][0]
            self.assertIn("_invalid_tool_arguments", call["args"])

    def test_incomplete_or_ambiguous_response_is_never_completion(self):
        payloads = [b'{"choices":', b'{"choices":[],"choices":[{}]}',
                    json.dumps({"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}).encode(),
                    json.dumps({"choices": [{"message": {"content": None}, "finish_reason": "stop"}]}).encode(),
                    b'{"choices":null}', b'{"choices":[null]}']
        for raw in payloads:
            with self.subTest(raw=raw[:60]), self.assertRaises(ValueError):
                from_openai_response(raw)

    def test_compatible_response_survives_and_network_read_is_bounded(self):
        from coding_service.models import openai_compatible
        from unittest.mock import patch
        raw = json.dumps({"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}).encode()
        response = Mock()
        response.read.return_value = raw
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(openai_compatible("http://localhost", "model", "key")([], [])["text"], "done")
        response.read.assert_called_once_with(4 * 1024 * 1024 + 1)

    def test_model_can_correct_invalid_arguments_next_turn(self):
        box = Mock()
        box.schemas.return_value = []
        box.scrub.side_effect = lambda value: value
        box.is_effect.return_value = False
        box.call.return_value = {"entries": []}
        replies = iter([
            {"tool_calls": [self.convert('{"path":')]},
            {"tool_calls": [self.convert('{"path":"."}')]},
            {"text": "done"},
        ])
        task = Task("user", box, lambda *_: next(replies), "test")
        task.run()
        self.assertEqual(task.state, "COMPLETED")
        self.assertEqual(box.call.call_count, 1)
        results = [json.loads(m["content"]) for m in task.messages if m["role"] == "tool"]
        self.assertEqual(results[0]["status"], "ERROR")
        self.assertEqual(results[1], {"entries": []})
