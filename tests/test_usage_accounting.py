"""Actual provider token counts, unknown usage and durable exactly-once totals."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from coding_service.loop import Task
from coding_service.manager import DurableManager
from coding_service.providers import from_openai_response
from coding_service.store import TaskStore
from coding_service.usage import normalize_usage


class UsageTests(unittest.TestCase):
    def test_crash_during_model_call_keeps_unknown_attempt_and_final_receipt_updates_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = TaskStore(path)
            tid, _, _ = store.create_or_get("host:user", "ws", "key", "instruction")
            started = {"code": "model_call_started", "model_call_id": "call", **normalize_usage(None)}
            store.append(tid, "notice", started)
            store.close()  # no response was received before the service disappeared
            store = TaskStore(path)
            try:
                unknown = store.usage_summary()
                self.assertEqual((unknown["calls"], unknown["unavailable_calls"], unknown["unfinished_calls"]), (1, 1, 1))
                finished = {"code": "model_usage", "model_call_id": "call", **normalize_usage({"input_tokens": 10, "output_tokens": 5})}
                store.append(tid, "notice", finished)
                store.append(tid, "notice", finished)
                store.append(tid, "notice", started)  # delayed start replay cannot overwrite final usage
                total = store.usage_summary()
                self.assertEqual((total["calls"], total["known_total_tokens"], total["unfinished_calls"]), (1, 15, 0))
            finally:
                store.close()

    def test_unrecordable_attempt_does_not_call_model(self):
        callback = Mock()
        def sink(_, event):
            if event["type"] == "model_call_started":
                raise OSError("disk full")
        task = Task("user", Mock(), callback, "instruction", event_sink=sink)
        task.run()
        callback.assert_not_called()
        self.assertEqual(task.state, "FAILED")

    def test_provider_cache_and_reasoning_are_subsets_not_added_to_total(self):
        raw = {"model": "model-a", "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
                         "prompt_tokens_details": {"cached_tokens": 80}, "completion_tokens_details": {"reasoning_tokens": 20}}}
        usage = from_openai_response(json.dumps(raw))["usage"]
        self.assertTrue(usage["available"])
        self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]), (100, 30, 130))
        self.assertEqual((usage["cached_input_tokens"], usage["reasoning_output_tokens"]), (80, 20))

    def test_missing_invalid_and_zero_usage_are_distinct(self):
        self.assertFalse(normalize_usage(None)["available"])
        self.assertIsNone(normalize_usage(None)["total_tokens"])
        self.assertTrue(normalize_usage({"input_tokens": 0, "output_tokens": 0})["available"])
        for value in (-1, True, "123", float("inf"), 10**20):
            self.assertFalse(normalize_usage({"input_tokens": value, "output_tokens": 1})["available"])
        self.assertFalse(normalize_usage({"input_tokens": 10, "output_tokens": 2, "total_tokens": 999})["available"])
        invalid = normalize_usage({"input_tokens": 10, "output_tokens": 2, "total_tokens": 999})
        self.assertIsNone(invalid["total_tokens"])
        self.assertEqual(normalize_usage(invalid), invalid)

    def test_billable_usage_is_not_lost_when_valid_envelope_is_truncated(self):
        from coding_service.providers import ProviderResponseError
        raw = {"model": "model-a", "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}],
               "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}}
        with self.assertRaises(ProviderResponseError) as error:
            from_openai_response(json.dumps(raw))
        events = []
        task = Task("user", Mock(), Mock(side_effect=error.exception), "instruction", event_sink=lambda _, event: events.append(event))
        task.run()
        self.assertEqual(task.state, "FAILED")
        usage = next(e["data"] for e in events if e["type"] == "model_usage")
        self.assertEqual(usage["total_tokens"], 130)
        self.assertTrue(usage["available"])

    def test_usage_is_durable_scoped_and_replay_does_not_double_count(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = TaskStore(path)
            tid, _, _ = store.create_or_get("host:alice", "ws", "key", "instruction")
            other, _, _ = store.create_or_get("host:bob", "ws", "key", "instruction")
            data = {"code": "model_usage", "model_call_id": "call-one", **normalize_usage({"input_tokens": 10, "output_tokens": 5}, provider="provider", model="model")}
            first = store.append(tid, "notice", data)
            self.assertEqual(store.append(tid, "notice", data), first)
            self.assertEqual(len(store.snapshot(tid)["events"]), 2)
            with self.assertRaises(ValueError):
                store.append(tid, "notice", {**data, "total_tokens": 999})
            store.append(other, "notice", {"code": "model_usage", "model_call_id": "missing", **normalize_usage(None)})
            store.close()
            store = TaskStore(path)
            try:
                all_usage = store.usage_summary()
                self.assertEqual((all_usage["calls"], all_usage["unavailable_calls"], all_usage["known_total_tokens"]), (2, 1, 15))
                self.assertEqual(store.usage_summary(principal="host:alice")["known_total_tokens"], 15)
                self.assertEqual(store.usage_summary(task_id=other)["known_total_tokens"], 0)
                self.assertEqual(store.usage_summary(task_id=other)["unavailable_calls"], 1)
                self.assertEqual(store.usage_summary(workspace="not-found")["calls"], 0)
                self.assertEqual(store.usage_summary(until=0)["calls"], 0)
                self.assertEqual(store.usage_summary(since=10**12)["calls"], 0)
                self.assertIsNone(all_usage["currency_cost"])
            finally:
                store.close()

    def test_real_loop_records_each_attempt_including_model_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "state.db")
            try:
                manager = DurableManager(store, {}, {}, None)
                for i, callback in enumerate((Mock(return_value={"text": "done", "usage": {"input_tokens": 10, "output_tokens": 5}}),
                                              Mock(side_effect=TimeoutError("model timed out")))):
                    tid, _, _ = store.create_or_get("host:user", "ws", str(i), "instruction")
                    task = Task("host:user", Mock(), callback, "instruction", event_sink=manager._sink(tid))
                    task.id = tid
                    task.run()
                summary = store.usage_summary()
                self.assertEqual((summary["calls"], summary["unavailable_calls"], summary["known_total_tokens"]), (2, 1, 15))
            finally:
                store.close()
