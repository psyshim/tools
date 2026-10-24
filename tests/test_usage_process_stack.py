"""Accounting belongs to the detachable service, survives restart and needs no host implementation."""
import json
import os
import subprocess
import sys
import unittest

try:
    from stack_harness import Stack, drain
except ImportError:
    from tests.stack_harness import Stack, drain


class UsageProcessStackTests(unittest.TestCase):
    def test_usage_notice_ledger_restart_retry_and_admin_cli_across_processes(self):
        stack = Stack(self)
        (stack.base / "model.py").write_text(
            "def complete(messages, tools):\n"
            "    return {'text': 'done', 'usage': {'provider': 'fixture', 'model': 'test-model', 'input_tokens': 100, 'output_tokens': 25, 'total_tokens': 125}}\n",
            encoding="utf-8")
        stack.start()
        gw = stack.gateway()
        handle = gw.start_coding("alice", "shop", "account this call", conversation_id="c", message_id="usage-one")
        events = drain(gw, "alice", handle)
        usage = [e.data for e in events if e.type == "notice" and e.data.get("code") == "model_usage"]
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["total_tokens"], 125)

        def summary():
            result = subprocess.run([sys.executable, "-m", "coding_service.manage", "--db", str(stack.db), "usage"],
                                    cwd=stack.service_tree, env={**os.environ, "PYTHONPATH": str(stack.service_tree)},
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        self.assertEqual(summary()["known_total_tokens"], 125)
        stack.stop("service", kill=True)
        stack.start_service()
        gw = stack.gateway()
        self.assertEqual(gw.start_coding("alice", "shop", "account this call", conversation_id="c", message_id="usage-one"), handle)
        drain(gw, "alice", handle)
        after = summary()
        self.assertEqual((after["calls"], after["known_total_tokens"], after["unavailable_calls"]), (1, 125, 0))
