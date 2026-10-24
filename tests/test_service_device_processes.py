"""Phase 4 gate: the Coding Service and the Device Agent are separate OS processes started from separate minimal source trees;
the host side has no filesystem execution dependency; malformed grants and workspace mismatch are rejected by the device."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

try:
    from stack_harness import Stack, drain, ROOT      # python -m unittest discover -s tests
except ImportError:
    from tests.stack_harness import Stack, drain, ROOT      # python -m unittest tests.test_...

from host_client import AgentUnavailable, ProtocolRefused

class _ClassOwner:
    """Lets a Stack register its cleanup with the class instead of one test."""
    def __init__(self, cls):
        self.addCleanup = cls.addClassCleanup

class SeparateProcessStack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = Stack(_ClassOwner(cls)).start()    # these scenarios only read and propose, so one pair of processes serves them all

    def test_trees_physically_separate_the_capabilities(self):
        stack = self.shared
        for forbidden in ("coding_agent", "code_attach", "device_agent"):
            self.assertFalse((stack.service_tree / forbidden).exists(), f"the service tree must not contain {forbidden}")
        self.assertFalse((stack.device_tree / "coding_service").exists(), "the device tree must not contain the service")
        self.assertTrue((stack.device_tree / "protocol").exists(), "the device shares only the stdlib-only wire helpers")

    def test_propose_flow_across_three_isolated_parties(self):
        stack = self.shared
        self.assertNotEqual(stack.procs["device"].pid, stack.procs["service"].pid)
        gw = stack.gateway()
        got = gw.availability()
        self.assertEqual((got.ok, got.modes, got.mutation), (True, ("read", "propose"), False))
        handle = gw.start_coding("alice", "shop", "propose a change", conversation_id="c", message_id="m1")
        events = drain(gw, "alice", handle)
        types = [e.type for e in events]
        self.assertIn("proposal_completed", types)
        self.assertNotIn("mutation_applied", types)
        self.assertEqual(events[-1].data["state"], "COMPLETED")
        proposal = next(e for e in events if e.type == "proposal_completed")
        self.assertIn("a.txt", json.dumps(proposal.data))
        self.assertEqual((stack.project / "a.txt").read_text(encoding="utf-8"), "alpha\n", "proposing changes nothing on disk")

    def test_the_device_refuses_what_the_wire_cannot_ask_for_and_nothing_private_leaks(self):
        stack = self.shared
        gw = stack.gateway()
        handle = gw.start_coding("alice", "shop", "traverse everywhere", conversation_id="c", message_id="m2")
        events = drain(gw, "alice", handle)
        text = json.dumps([e.data for e in events])
        self.assertNotIn("STACK-SECRET", text)
        self.assertNotIn(str(stack.base), text)
        self.assertNotIn(str(Path.home()), text)
        self.assertEqual([e.data["status"] for e in events if e.type == "tool_activity"], ["ERROR"] * 4)

    def test_default_installation_cannot_mutate_whatever_the_model_asks(self):
        stack = self.shared
        gw = stack.gateway()
        events = drain(gw, "alice", gw.start_coding("alice", "shop", "approve this", conversation_id="c", message_id="m3"))
        types = [e.type for e in events]
        self.assertNotIn("approval_required", types)
        self.assertNotIn("mutation_applied", types)
        self.assertEqual((stack.project / "a.txt").read_text(encoding="utf-8"), "alpha\n")

    def test_hosts_are_isolated_from_each_other(self):
        stack = self.shared
        a, b = stack.gateway("chat-a"), stack.gateway("chat-b")
        handle = a.start_coding("alice", "shop", "propose a change", conversation_id="c", message_id="m4")
        drain(a, "alice", handle)
        with self.assertRaises(ProtocolRefused) as caught:
            drain(b, "alice", handle)           # same user name, different host: a different principal
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        with self.assertRaises(ProtocolRefused) as caught:
            b.start_coding("alice", "shop", "propose x", conversation_id="c", message_id="m5")
        self.assertEqual(caught.exception.code, "NOT_FOUND", "host B was never granted this workspace")
        with self.assertRaises(ProtocolRefused):
            a.start_coding("alice", "unknown-workspace", "propose x", conversation_id="c", message_id="m6")

    def test_unknown_host_credential_is_unavailable_for_the_caller(self):
        stack = self.shared
        from host_client import AgentGateway
        bad = AgentGateway(stack.service_url, "not-a-credential", retries=1, timeout=10)
        self.assertEqual(bad.availability().reason, "credential_rejected")

class DeviceBoundary(unittest.TestCase):
    def test_malformed_grants_stop_the_device_before_it_listens(self):
        stack = Stack(self)
        stack.caps = ["read", "propose", "apply"]   # effects without experimental_mutation
        path = stack.base / "grants.json"
        path.write_text(json.dumps(stack.grants()), encoding="utf-8")
        result = subprocess.run([sys.executable, "-m", "device_agent", "--config", str(path), "--port", "0"], cwd=str(stack.device_tree),
                                env={**__import__("os").environ, "PYTHONPATH": str(stack.device_tree)}, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn("experimental_mutation", result.stderr)
        self.assertIn("refuses to start", result.stderr)

    def test_workspace_mismatch_is_rejected_by_the_device_and_surfaces_as_a_failed_task(self):
        stack = Stack(self, workspace_target="not-on-this-device").start()
        gw = stack.gateway()
        events = drain(gw, "alice", gw.start_coding("alice", "shop", "propose a change", conversation_id="c", message_id="m7"))
        self.assertEqual((events[-1].type, events[-1].data["state"], events[-1].data.get("detail")), ("state", "FAILED", "workspace_refused"))
        self.assertEqual((stack.project / "a.txt").read_text(encoding="utf-8"), "alpha\n")

    def test_wrong_device_token_never_reaches_the_workspace(self):
        stack = Stack(self)
        stack.start_device()
        stack.device_token = "a-different-token"        # the service will present this, not the one the device knows
        stack.start_service()
        gw = stack.gateway()
        events = drain(gw, "alice", gw.start_coding("alice", "shop", "propose a change", conversation_id="c", message_id="m8"))
        self.assertEqual(events[-1].data["state"], "FAILED")
        self.assertEqual(events[-1].data.get("detail"), "device_unavailable")

    def test_a_dead_device_fails_tasks_cleanly_and_the_service_keeps_answering(self):
        stack = Stack(self).start()
        stack.stop("device", kill=True)
        gw = stack.gateway()
        self.assertTrue(gw.availability().ok, "the service itself is up")
        events = drain(gw, "alice", gw.start_coding("alice", "shop", "propose a change", conversation_id="c", message_id="m9"))
        self.assertEqual((events[-1].data["state"], events[-1].data.get("detail")), ("FAILED", "device_unavailable"))

    def test_the_service_being_down_is_a_typed_unavailable_for_the_host(self):
        stack = Stack(self).start()
        gw = stack.gateway()
        stack.stop("service", kill=True)
        self.assertFalse(gw.availability().ok)
        with self.assertRaises(AgentUnavailable):
            gw.start_coding("alice", "shop", "x", conversation_id="c", message_id="m10")

    def test_second_device_on_the_same_workspace_refuses_to_start(self):
        stack = Stack(self)
        stack.start_device()
        second = Stack(self)
        second.project = stack.project
        second.locks = stack.locks                     # the shared per-user lock location is the same
        path = second.base / "grants2.json"
        path.write_text(json.dumps(second.grants()), encoding="utf-8")
        result = subprocess.run([sys.executable, "-m", "device_agent", "--config", str(path), "--port", "0"], cwd=str(second.device_tree),
                                env={**__import__("os").environ, "PYTHONPATH": str(second.device_tree)}, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 3, result.stderr[-500:])
        self.assertIn("already owns", result.stderr)

class Provenance(unittest.TestCase):
    def test_service_package_never_imports_tool_file_or_git_code(self):
        import ast
        forbidden = {"coding_agent", "code_attach", "device_agent", "subprocess", "shutil", "ctypes", "multiprocessing"}
        for path in (ROOT / "coding_service").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
                for name in names:
                    with self.subTest(file=path.name, imports=name):
                        self.assertNotIn(name.split(".")[0], forbidden)

    def test_device_package_never_imports_service_or_host_code(self):
        import ast
        forbidden = {"coding_service", "host_client", "testkit"}
        for path in list((ROOT / "device_agent").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
                for name in names:
                    with self.subTest(file=path.name, imports=name):
                        self.assertNotIn(name.split(".")[0], forbidden)

if __name__ == "__main__":
    unittest.main()
