"""Phase 4 (device side): grants are the only source of authority; malformed grants are refused as a whole; the wire carries
typed intents, never paths or commands; effects are journaled and run at most once."""
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from coding_agent.execution_owner import OwnerBusy
from device_agent.config import ConfigError, parse_config
from device_agent.core import DeviceService
from device_agent.server import create_app

TOKEN = "device-token-123"
AUTH = {"authorization": "Bearer " + TOKEN}
SECRET = "DEVICE-SECRET-VALUE"

class Device:
    """A device with two workspaces plus an outside secret, in a temp tree."""
    def __init__(self, experimental=False, caps=("read", "propose"), test_commands=None):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.base, self.state = base, base / "state"
        self.root = base / "shop"
        self.root.mkdir()
        (self.root / "a.txt").write_text("alpha\n", encoding="utf-8")
        (self.root / ".env").write_text("KEY=" + SECRET, encoding="utf-8")
        (base / "outside.txt").write_text(SECRET, encoding="utf-8")
        self.other = base / "docs"
        self.other.mkdir()
        (self.other / "note.md").write_text("# note\n", encoding="utf-8")
        ws = {"root": str(self.root), "capabilities": list(caps)}
        if test_commands:
            ws["test_commands"] = test_commands
        self.raw = {"device_id": "dev1", "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(), "state_dir": str(self.state), "experimental_mutation": experimental,
                    "workspaces": {"shop": ws, "docs": {"root": str(self.other), "capabilities": ["read"]}}}
        self.raw["lock_dir"] = str(base / "locks")
        self.config = parse_config(self.raw)
        self.service = DeviceService(self.config)
        self.client = TestClient(create_app(self.config, self.service, trust_test_client=True))

    def close(self):
        self.service.close()
        self.tmp.cleanup()

    def open(self, workspace="shop", session="s1"):
        return self.client.post("/v1/sessions", json={"workspace_id": workspace, "session_id": session}, headers=AUTH)

    def call(self, tool, args=None, op_id=None, session="s1"):
        body = {"tool": tool, "args": args or {}}
        if op_id:
            body["op_id"] = op_id
        return self.client.post(f"/v1/sessions/{session}/call", json=body, headers=AUTH)

class Base(unittest.TestCase):
    def device(self, **kw):
        d = Device(**kw)
        self.addCleanup(d.close)
        return d

class GrantsAreRefusedAsAWhole(unittest.TestCase):
    def good(self, base):
        (base / "p").mkdir(exist_ok=True)
        return {"device_id": "d", "token_sha256": "a" * 64, "state_dir": str(base / "state"), "workspaces": {"w": {"root": str(base / "p"), "capabilities": ["read", "propose"]}}}

    def problems(self, mutate):
        with tempfile.TemporaryDirectory() as tmp:
            raw = self.good(Path(tmp).resolve())
            mutate(raw, Path(tmp).resolve())
            with self.assertRaises(ConfigError) as caught:
                parse_config(raw)
            return " | ".join(caught.exception.problems)

    def test_a_valid_file_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = parse_config(self.good(Path(tmp).resolve()))
            self.assertEqual(config.workspaces["w"].capabilities, frozenset({"read", "propose"}))

    def test_every_kind_of_malformed_or_unsafe_grant_is_refused_with_a_reason(self):
        cases = {
            "not an object": (lambda raw, b: None, None),
            "unknown top-level key": (lambda raw, b: raw.update(shell=True), "unknown key 'shell'"),
            "token not a hash": (lambda raw, b: raw.update(token_sha256="plaintext"), "token_sha256"),
            "device id": (lambda raw, b: raw.update(device_id="bad id!"), "device_id"),
            "relative state dir": (lambda raw, b: raw.update(state_dir="state"), "state_dir must be an absolute path"),
            "relative root": (lambda raw, b: raw["workspaces"]["w"].update(root="p"), "root must be an absolute path"),
            "missing root": (lambda raw, b: raw["workspaces"]["w"].update(root=str(b / "nope")), "root does not exist"),
            "drive root": (lambda raw, b: raw["workspaces"]["w"].update(root=Path(b.anchor).as_posix()), "filesystem root"),
            "no read": (lambda raw, b: raw["workspaces"]["w"].update(capabilities=["propose"]), "'read' capability"),
            "unknown capability": (lambda raw, b: raw["workspaces"]["w"].update(capabilities=["read", "shell"]), "unknown capability 'shell'"),
            "effects without experimental flag": (lambda raw, b: raw["workspaces"]["w"].update(capabilities=["read", "apply"]), "experimental_mutation"),
            "test without commands": (lambda raw, b: (raw.update(experimental_mutation=True), raw["workspaces"]["w"].update(capabilities=["read", "test"])), "at least one test_commands"),
            "commands without test": (lambda raw, b: raw["workspaces"]["w"].update(test_commands={"u": ["python"]}), "without the 'test' capability"),
            "bad commands": (lambda raw, b: (raw.update(experimental_mutation=True), raw["workspaces"]["w"].update(capabilities=["read", "test"], test_commands={"u": []})), "test_commands"),
            "bad workspace id": (lambda raw, b: raw["workspaces"].update({"../x": raw["workspaces"]["w"]}), "id must match"),
            "unknown workspace key": (lambda raw, b: raw["workspaces"]["w"].update(shell="rm"), "unknown key 'shell'"),
            "state inside workspace": (lambda raw, b: raw.update(state_dir=str(b / "p" / "state")), "private state must live outside"),
            "workspace inside state": (lambda raw, b: raw.update(state_dir=str(b)), "private state must live outside"),
            "nested workspaces": (lambda raw, b: (( b / "p" / "inner").mkdir(), raw["workspaces"].update(w2={"root": str(b / "p" / "inner"), "capabilities": ["read"]})), "overlaps"),
            "same root twice": (lambda raw, b: raw["workspaces"].update(w2=dict(raw["workspaces"]["w"])), "overlaps"),
        }
        for name, (mutate, expect) in cases.items():
            with self.subTest(case=name):
                if expect is None:
                    for raw in ([], "x", None, 5):
                        with self.assertRaises(ConfigError):
                            parse_config(raw)
                    continue
                self.assertIn(expect, self.problems(mutate))

    def test_all_problems_are_listed_in_one_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = self.good(Path(tmp).resolve())
            raw.update(token_sha256="x", device_id="!")
            raw["workspaces"]["w"].update(capabilities=["shell"])
            with self.assertRaises(ConfigError) as caught:
                parse_config(raw)
            self.assertGreaterEqual(len(caught.exception.problems), 4)

class WireIsTypedAndAuthenticated(Base):
    def test_token_and_locality(self):
        d = self.device()
        for headers in ({}, {"authorization": "Bearer wrong"}, {"authorization": "Basic abc"}, {"authorization": TOKEN}):
            self.assertEqual(d.client.get("/v1/info", headers=headers).status_code, 401, headers)
        self.assertEqual(d.client.get("/v1/info", headers=AUTH).status_code, 200)
        remote = TestClient(create_app(d.config, d.service))   # a client that is not loopback
        self.assertEqual(remote.get("/v1/info", headers=AUTH).status_code, 403)

    def test_info_never_reveals_roots_or_state(self):
        d = self.device()
        text = d.client.get("/v1/info", headers=AUTH).text
        for secret in (str(d.root), str(d.base), d.root.as_posix(), str(d.state)):
            self.assertNotIn(secret, text)
        self.assertEqual(json.loads(text)["workspaces"]["shop"]["capabilities"], ["propose", "read"])

    def test_sessions_bind_ids_to_workspaces(self):
        d = self.device()
        first = d.open()
        self.assertEqual(first.status_code, 200)
        self.assertEqual((first.json()["mutation"], first.json()["effect_tools"]), (False, []))
        self.assertEqual(d.open().json(), first.json(), "opening the same session again is idempotent")
        self.assertEqual(d.open(workspace="docs").status_code, 409)
        self.assertEqual(d.open(workspace="nope", session="s2").status_code, 404)
        self.assertEqual(d.open(workspace="C:/Windows", session="s3").status_code, 404, "a path is not a workspace id")
        for bad in ("", "../x", "a b", "x" * 100):
            self.assertEqual(d.client.post("/v1/sessions", json={"workspace_id": "shop", "session_id": bad}, headers=AUTH).status_code, 400)

    def test_misuse_is_refused_not_crashed(self):
        d = self.device()
        d.open()
        for raw in (b"{not json", b"[]", b'"x"', b""):
            r = d.client.post("/v1/sessions/s1/call", content=raw, headers={**AUTH, "content-type": "application/json"})
            self.assertIn(r.status_code, (200, 400), raw)
        self.assertEqual(d.client.post("/v1/sessions/s1/call", content=b"x" * (1024 * 1024 + 10), headers=AUTH).status_code, 413)
        self.assertEqual(d.client.post("/v1/sessions/s1/call", json={"tool": 5, "args": {}}, headers=AUTH).status_code, 400)
        self.assertEqual(d.client.post("/v1/sessions/s1/call", json={"tool": "read_file", "args": []}, headers=AUTH).status_code, 400)
        self.assertEqual(d.client.post("/v1/sessions/nope/call", json={"tool": "read_file", "args": {}}, headers=AUTH).status_code, 404)
        self.assertEqual(d.client.get("/v1/sessions/%2e%2e/schemas", headers=AUTH).status_code, 404)

    def test_the_wire_cannot_name_paths_roots_or_commands(self):
        d = self.device()
        d.open()
        attempts = [("read_file", {"path": "../outside.txt"}), ("read_file", {"path": str(d.base / "outside.txt")}), ("read_file", {"path": ".env"}),
                    ("run_shell", {"cmd": "whoami"}), ("search_text", {"query": "KEY", "path": ".."}), ("propose_new_file", {"path": "../evil.txt", "content": "x"})]
        for tool, args in attempts:
            r = d.call(tool, args)
            self.assertEqual(r.status_code, 200)
            self.assertIn("error", r.json(), (tool, args))
            self.assertNotIn(SECRET, r.text)
        self.assertFalse((d.base / "evil.txt").exists())
        self.assertEqual(d.call("read_file", {"path": "a.txt"}).json()["result"]["lines"], ["alpha"])

class GrantsIntersectWithRequests(Base):
    def test_read_propose_workspace_offers_and_accepts_no_effects(self):
        d = self.device()
        d.open()
        names = {s["name"] for s in d.client.get("/v1/sessions/s1/schemas", headers=AUTH).json()["schemas"]}
        self.assertTrue({"read_file", "propose_edit", "find_symbol"} <= names)
        self.assertFalse(names & {"apply_proposal", "run_tests", "propose_undo", "propose_git_commit"})
        proposal = d.call("propose_edit", {"path": "a.txt", "old": "alpha", "new": "BETA"}).json()["result"]["proposal_id"]
        r = d.call("apply_proposal", {"proposal_id": proposal}, op_id="op-1")
        self.assertIn("error", r.json())
        self.assertEqual((d.root / "a.txt").read_text(encoding="utf-8"), "alpha\n")
        self.assertIn("error", d.call("run_tests", {"name": "x"}, op_id="op-2").json())
        self.assertIn("error", d.client.post("/v1/sessions/s1/describe", json={"tool": "apply_proposal", "args": {"proposal_id": proposal}}, headers=AUTH).json())

    def test_a_read_only_workspace_cannot_even_propose_writes_it_does_not_have(self):
        d = self.device()
        d.open(workspace="docs", session="docs1")
        names = {s["name"] for s in d.client.get("/v1/sessions/docs1/schemas", headers=AUTH).json()["schemas"]}
        self.assertNotIn("apply_proposal", names)
        r = d.call("read_file", {"path": "note.md"}, session="docs1")
        self.assertEqual(r.json()["result"]["lines"], ["# note"])
        self.assertIn("error", d.call("read_file", {"path": "../shop/a.txt"}, session="docs1").json(), "sessions cannot cross workspaces")

    def test_apply_and_test_are_separate_grants(self):
        d = self.device(experimental=True, caps=("read", "propose", "test"), test_commands={"unit": ["python", "-c", "print('ok')"]})
        d.open()
        info = d.open().json()
        self.assertEqual(info["effect_tools"], ["run_tests"])
        proposal = d.call("propose_edit", {"path": "a.txt", "old": "alpha", "new": "BETA"}).json()["result"]["proposal_id"]
        self.assertIn("error", d.call("apply_proposal", {"proposal_id": proposal}, op_id="op-a").json())
        ran = d.call("run_tests", {"name": "unit"}, op_id="op-t").json()
        self.assertEqual(ran["result"]["status"], "PASS")
        self.assertEqual((d.root / "a.txt").read_text(encoding="utf-8"), "alpha\n")

class EffectsAreJournaledAndRunAtMostOnce(Base):
    def edit_device(self):
        d = self.device(experimental=True, caps=("read", "propose", "apply"))
        d.open()
        return d

    def test_effect_needs_an_op_id_and_applies_exactly_once(self):
        d = self.edit_device()
        pid = d.call("propose_edit", {"path": "a.txt", "old": "alpha", "new": "BETA"}).json()["result"]["proposal_id"]
        self.assertEqual(d.call("apply_proposal", {"proposal_id": pid}).status_code, 400)
        first = d.call("apply_proposal", {"proposal_id": pid}, op_id="op-1").json()
        self.assertEqual(first["result"]["status"], "APPLIED")
        again = d.call("apply_proposal", {"proposal_id": pid}, op_id="op-1").json()
        self.assertTrue(again["replayed"])
        self.assertEqual(again["result"], first["result"], "a repeated op_id returns the recorded outcome and does not run again")
        self.assertEqual((d.root / "a.txt").read_text(encoding="utf-8"), "BETA\n")
        self.assertEqual(d.client.get("/v1/operations/op-1", headers=AUTH).json()["state"], "SUCCEEDED")

    def test_reusing_an_op_id_for_a_different_request_is_refused(self):
        d = self.edit_device()
        a = d.call("propose_edit", {"path": "a.txt", "old": "alpha", "new": "ONE"}).json()["result"]["proposal_id"]
        b = d.call("propose_edit", {"path": "a.txt", "old": "alpha", "new": "TWO"}).json()["result"]["proposal_id"]
        d.call("apply_proposal", {"proposal_id": a}, op_id="op-x")
        self.assertEqual(d.call("apply_proposal", {"proposal_id": b}, op_id="op-x").status_code, 409)

    def test_an_interrupted_attempt_is_reported_unknown_never_silently_repeated(self):
        d = self.edit_device()
        pid = d.call("propose_edit", {"path": "a.txt", "old": "alpha", "new": "BETA"}).json()["result"]["proposal_id"]
        request = {"workspace": "shop", "session": "s1", "tool": "apply_proposal", "args": {"proposal_id": pid}}
        d.service.journal.prepare("op-crash", request)
        self.assertTrue(d.service.journal.claim("op-crash"))           # the process 'died' here: claimed, never recorded
        r = d.call("apply_proposal", {"proposal_id": pid}, op_id="op-crash").json()
        self.assertTrue(r["unknown_outcome"])
        self.assertEqual((d.root / "a.txt").read_text(encoding="utf-8"), "alpha\n", "no automatic retry")
        self.assertEqual(d.client.get("/v1/operations/op-crash", headers=AUTH).json()["state"], "UNKNOWN")

    def test_a_conflict_is_recorded_as_a_failed_operation(self):
        d = self.edit_device()
        pid = d.call("propose_edit", {"path": "a.txt", "old": "alpha", "new": "BETA"}).json()["result"]["proposal_id"]
        (d.root / "a.txt").write_text("changed elsewhere\n", encoding="utf-8")
        r = d.call("apply_proposal", {"proposal_id": pid}, op_id="op-c").json()
        self.assertEqual(r["result"]["status"], "CONFLICT")
        self.assertEqual(d.client.get("/v1/operations/op-c", headers=AUTH).json()["state"], "FAILED")
        self.assertEqual((d.root / "a.txt").read_text(encoding="utf-8"), "changed elsewhere\n")

    def test_concurrent_requests_with_one_op_id_execute_once(self):
        d = self.edit_device()
        pid = d.call("propose_edit", {"path": "a.txt", "old": "alpha", "new": "BETA"}).json()["result"]["proposal_id"]
        outcomes, gate = [], threading.Barrier(6)
        def go():
            gate.wait()
            outcomes.append(d.call("apply_proposal", {"proposal_id": pid}, op_id="op-race").json())
        threads = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in threads]
        [t.join(20) for t in threads]
        executed = [o for o in outcomes if "result" in o and not o.get("replayed") and o["result"].get("status") == "APPLIED"]
        self.assertEqual(len(executed), 1)
        self.assertEqual((d.root / "a.txt").read_text(encoding="utf-8"), "BETA\n")

    def test_journal_and_state_live_outside_every_workspace(self):
        d = self.edit_device()
        self.assertTrue((d.state / "journal.db").is_file())
        self.assertEqual([p.name for p in d.root.iterdir() if p.name not in {"a.txt", ".env"}], [], "nothing private is written into the workspace")

class SingleExecutor(Base):
    def test_a_second_device_on_the_same_workspace_refuses_to_start(self):
        d = self.device()
        other_state = d.base / "state2"
        raw = {**d.raw, "state_dir": str(other_state)}   # a different private state, the same shared lock location
        with self.assertRaises(OwnerBusy):
            DeviceService(parse_config(raw))

    def test_session_count_is_bounded(self):
        import device_agent.core as core
        d = self.device()
        old, core.MAX_SESSIONS = core.MAX_SESSIONS, 3
        try:
            for i in range(3):
                self.assertEqual(d.open(session=f"s{i}").status_code, 200)
            self.assertEqual(d.open(session="s9").status_code, 409)
        finally:
            core.MAX_SESSIONS = old

if __name__ == "__main__":
    unittest.main()
