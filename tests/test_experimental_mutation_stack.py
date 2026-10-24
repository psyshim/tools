"""Phase 6 gates, end to end across real processes: effects only with explicit device grants, approval-bound, journaled, honest
about uncertainty, and safe against a human's concurrent save. Everything here is the EXPERIMENTAL configuration; the default
installation (no grants) is covered by test_service_device_processes."""
import os
import sqlite3
import sys
import unittest

try:
    from stack_harness import Stack, drain
except ImportError:
    from tests.stack_harness import Stack, drain

from coding_service.device_client import DeviceClient

WINDOWS = os.name == "nt"
UNIT = [sys.executable, "-c", "import sys; sys.exit(0 if open('a.txt').read().strip() == 'alpha' else 1)"]

def experimental(test, **kw):
    kw.setdefault("test_commands", {"unit": UNIT})
    return Stack(test, device_caps=("read", "propose", "apply", "test"), experimental=True, allow_mutation=True, **kw)

def until(gw, user, handle, kind, seconds=30):
    seen = []
    for event in gw.observe_coding(user, handle, poll_interval=0.05, max_seconds=seconds):
        seen.append(event)
        if event.type == kind:
            return seen
    raise AssertionError(f"no {kind} event; got {[e.type for e in seen]}")

def decide(gw, user, handle, card, approve=True):
    return gw.submit_decision(user, handle, card.data["action_id"], card.data["fingerprint"], approve)

def journal(stack, sql="SELECT id, state, result FROM operations"):
    conn = sqlite3.connect(f"file:{(stack.device_state / 'journal.db').as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()

class _ClassOwner:
    def __init__(self, cls):
        self.addCleanup = cls.addClassCleanup

class ApprovedMutation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = experimental(_ClassOwner(cls)).start()

    def setUp(self):
        self.stack.project.joinpath("a.txt").write_bytes(b"alpha\n")
        self.gw = self.stack.gateway()
        self.n = getattr(ApprovedMutation, "counter", 0) + 1
        ApprovedMutation.counter = self.n

    def start(self, text):
        ApprovedMutation.serial = getattr(ApprovedMutation, "serial", 0) + 1
        return self.gw.start_coding("alice", "shop", text, conversation_id="c", message_id=f"msg-{ApprovedMutation.serial}")

    def text(self):
        return self.stack.project.joinpath("a.txt").read_bytes()

    def test_capabilities_are_advertised_honestly(self):
        got = self.gw.availability()
        self.assertEqual((got.mutation, "apply" in got.modes, "test" in got.modes), (True, True, True))

    def test_nothing_changes_before_the_decision_and_approval_applies_once_with_a_receipt(self):
        handle = self.start("approve this")
        events = until(self.gw, "alice", handle, "approval_required")
        card = events[-1]
        self.assertIn("-alpha", card.data["summary"]["diff"])
        self.assertEqual(self.text(), b"alpha\n", "nothing is written before the decision")
        self.assertTrue(decide(self.gw, "alice", handle, card).accepted)
        final = drain(self.gw, "alice", handle)
        receipts = [e for e in final if e.type == "mutation_applied"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].data["receipt"]["status"], "APPLIED")
        self.assertEqual([e.data["state"] for e in final if e.type == "state"][-1], "COMPLETED")
        self.assertEqual(self.text(), b"BETA\n")
        rows = [r for r in journal(self.stack) if r[1] == "SUCCEEDED"]
        self.assertTrue(rows, "the device journaled the effect")

    def test_rejection_changes_nothing_and_records_no_effect(self):
        before = len(journal(self.stack))
        handle = self.start("approve this")
        card = until(self.gw, "alice", handle, "approval_required")[-1]
        self.assertTrue(decide(self.gw, "alice", handle, card, approve=False).accepted)
        final = drain(self.gw, "alice", handle)
        self.assertNotIn("mutation_applied", [e.type for e in final])
        self.assertEqual(self.text(), b"alpha\n")
        self.assertEqual(len(journal(self.stack)), before, "a rejected action never reached the journal")

    def test_wrong_fingerprint_and_a_second_decision_are_refused(self):
        handle = self.start("approve this")
        card = until(self.gw, "alice", handle, "approval_required")[-1]
        self.assertFalse(self.gw.submit_decision("alice", handle, card.data["action_id"], "wrong", True).accepted)
        self.assertTrue(decide(self.gw, "alice", handle, card, approve=False).accepted)
        self.assertFalse(decide(self.gw, "alice", handle, card, approve=True).accepted)
        drain(self.gw, "alice", handle)
        self.assertEqual(self.text(), b"alpha\n")

    def test_a_human_save_between_proposal_and_decision_is_never_overwritten(self):
        handle = self.start("approve this")
        card = until(self.gw, "alice", handle, "approval_required")[-1]
        self.stack.project.joinpath("a.txt").write_bytes(b"human work\n")
        decide(self.gw, "alice", handle, card)
        final = drain(self.gw, "alice", handle)
        self.assertNotIn("mutation_applied", [e.type for e in final])
        notices = [e.data for e in final if e.type == "notice" and e.data.get("code") == "mutation_not_applied"]
        self.assertEqual([n["status"] for n in notices], ["CONFLICT"])
        self.assertEqual(self.text(), b"human work\n")

    @unittest.skipUnless(WINDOWS, "the write guard is a Windows mechanism")
    def test_a_file_held_open_for_writing_by_another_program_blocks_the_apply_safely(self):
        handle = self.start("approve this")
        card = until(self.gw, "alice", handle, "approval_required")[-1]
        with open(self.stack.project / "a.txt", "r+b") as other_program:
            other_program.read(1)
            decide(self.gw, "alice", handle, card)
            final = drain(self.gw, "alice", handle)
        self.assertNotIn("mutation_applied", [e.type for e in final])
        self.assertEqual(self.text(), b"alpha\n")

    def test_tests_never_report_a_false_pass(self):
        first = self.start("runtests now")
        card = until(self.gw, "alice", first, "approval_required")[-1]
        self.assertEqual(card.data["summary"]["kind"], "run_tests")
        decide(self.gw, "alice", first, card)
        result = next(e for e in drain(self.gw, "alice", first) if e.type == "test_result")
        self.assertEqual(result.data["status"], "PASS")
        edit = self.start("approve this")
        decide(self.gw, "alice", edit, until(self.gw, "alice", edit, "approval_required")[-1])
        drain(self.gw, "alice", edit)
        second = self.start("runtests again")
        decide(self.gw, "alice", second, until(self.gw, "alice", second, "approval_required")[-1])
        result = next(e for e in drain(self.gw, "alice", second) if e.type == "test_result")
        self.assertEqual(result.data["status"], "FAIL", "the edit broke the check and the report says so")
        self.assertEqual(result.data["exit_code"], 1)

    def test_cancel_while_waiting_for_a_decision(self):
        handle = self.start("approve this")
        until(self.gw, "alice", handle, "approval_required")
        self.assertEqual(self.gw.cancel_coding("alice", handle).state in ("CANCELLED", "AWAITING_APPROVAL", "RUNNING"), True)
        final = drain(self.gw, "alice", handle)
        self.assertEqual([e.data["state"] for e in final if e.type == "state"][-1], "CANCELLED")
        self.assertEqual(self.text(), b"alpha\n")

class BothSidesMustAgree(unittest.TestCase):
    def test_a_device_grant_alone_cannot_enable_effects_when_the_service_keeps_them_off(self):
        stack = Stack(self, device_caps=("read", "propose", "apply", "test"), experimental=True, allow_mutation=False, test_commands={"unit": UNIT}).start()
        gw = stack.gateway()
        got = gw.availability()
        self.assertEqual((got.mutation, "apply" in got.modes), (False, False))
        handle = gw.start_coding("alice", "shop", "approve this", conversation_id="c", message_id="b1")
        final = drain(gw, "alice", handle)
        self.assertNotIn("approval_required", [e.type for e in final])
        self.assertNotIn("mutation_applied", [e.type for e in final])
        self.assertEqual(stack.project.joinpath("a.txt").read_bytes().replace(b"\r\n", b"\n"), b"alpha\n")
        self.assertEqual(journal(stack), [], "nothing reached the device journal")

class RestartsAroundApproval(unittest.TestCase):
    def test_service_killed_while_a_decision_is_pending_never_applies_it_afterwards(self):
        stack = experimental(self).start()
        original = stack.project.joinpath("a.txt").read_bytes()
        gw = stack.gateway()
        handle = gw.start_coding("alice", "shop", "approve this", conversation_id="c", message_id="r1")
        card = until(gw, "alice", handle, "approval_required")[-1]
        stack.stop("service", kill=True)
        stack.start_service()
        gw = stack.gateway()
        outcome = gw.submit_decision("alice", handle, card.data["action_id"], card.data["fingerprint"], True)
        self.assertFalse(outcome.accepted, "the worker that asked is gone; the stale card cannot be answered")
        final = drain(gw, "alice", handle)
        self.assertEqual([e.data["state"] for e in final if e.type == "state"][-1], "FAILED")
        self.assertEqual(stack.project.joinpath("a.txt").read_bytes(), original)
        self.assertEqual([r for r in journal(stack) if r[1] != "PREPARED"], [])

class CrashPoints(unittest.TestCase):
    """The device dies at each point of an approved effect. The service must say NEEDS_RECONCILIATION (never success, never a
    clean failure), the file must be exactly what really happened, and the device journal must tell the truth afterwards."""
    def crash_at(self, point):
        stack = experimental(self, env_extra={"DEVICE_AGENT_ALLOW_FAILPOINTS": "1", "DEVICE_AGENT_FAILPOINT": point}).start()
        gw = stack.gateway()
        handle = gw.start_coding("alice", "shop", "approve this", conversation_id="c", message_id="x1")
        card = until(gw, "alice", handle, "approval_required")[-1]
        decide(gw, "alice", handle, card)
        final = drain(gw, "alice", handle, seconds=60)
        self.assertEqual(stack.procs["device"].wait(timeout=20), 137, "the device died at the failpoint")
        types = [e.type for e in final]
        self.assertNotIn("mutation_applied", types, "no receipt for something nobody can vouch for")
        self.assertEqual([e.data["state"] for e in final if e.type == "state"][-1], "NEEDS_RECONCILIATION")
        notice = next(e.data for e in final if e.type == "notice" and e.data.get("code") == "mutation_not_applied")
        self.assertEqual(notice["status"], "UNKNOWN_OUTCOME")
        stack.env_extra.pop("DEVICE_AGENT_FAILPOINT")
        stack.start_device()                           # the operator restarts the device and looks at the truth
        client = DeviceClient(stack.device_url, stack.device_token)
        return stack, client.operation(notice["op_id"]), stack.project.joinpath("a.txt").read_bytes()

    def test_crash_after_claim_before_the_effect(self):
        stack, op, content = self.crash_at("after_claim")
        self.assertEqual(content.replace(b"\r\n", b"\n"), b"alpha\n", "nothing happened")
        self.assertEqual(op["state"], "UNKNOWN")

    def test_crash_after_the_effect_before_it_was_recorded(self):
        stack, op, content = self.crash_at("after_effect")
        self.assertEqual(content.replace(b"\r\n", b"\n"), b"BETA\n", "it did happen")
        self.assertEqual(op["state"], "UNKNOWN", "...and the journal honestly does not know: a human must reconcile, nothing retries")

    def test_crash_after_recording_before_the_response(self):
        stack, op, content = self.crash_at("after_record")
        self.assertEqual(content.replace(b"\r\n", b"\n"), b"BETA\n")
        self.assertEqual(op["state"], "SUCCEEDED")
        self.assertEqual(op["result"]["result"]["status"], "APPLIED", "the recorded receipt is available for reconciliation")

if __name__ == "__main__":
    unittest.main()
