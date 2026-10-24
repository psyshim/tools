"""Real multi-process stack for tests: a Device Agent and a Coding Service as separate OS processes, each started from a
MINIMAL COPY of the source tree (so a missing dependency is a failure, not something found in the repository), talking over
loopback HTTP. The host side is the powerless host_client.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "host_client"))

SCRIPTED_MODEL = r'''
import json, os, time

def complete(messages, tools):
    text = messages[1]["text"]
    done = sum(m["role"] == "assistant" for m in messages)
    def call(i, tool, **args):
        return {"text": "", "tool_calls": [{"id": f"c{done}-{i}", "name": tool, "args": args}]}
    flag = os.environ.get("MODEL_RELEASE_FLAG")
    if flag and text.startswith("wait"):
        while not os.path.exists(flag):          # blocks mid-task until the test releases it (deterministic kill points)
            time.sleep(0.05)
    if text.startswith("propose") and done == 0:
        return call(0, "propose_edit", path="a.txt", old="alpha", new="BETA")
    if text.startswith("approve"):
        if done == 0:
            return call(0, "propose_edit", path="a.txt", old="alpha", new="BETA")
        if done == 1:
            return call(1, "apply_proposal", proposal_id=json.loads(messages[-1]["content"]).get("proposal_id", "none"))
    if text.startswith("runtests") and done == 0:
        return call(0, "run_tests", name="unit")
    if text.startswith("traverse") and done == 0:
        return {"text": "", "tool_calls": [{"id": "t1", "name": "read_file", "args": {"path": "../outside.txt"}}, {"id": "t2", "name": "read_file", "args": {"path": ".env"}},
                                           {"id": "t3", "name": "read_file", "args": {"path": "C:/Windows/win.ini"}}, {"id": "t4", "name": "run_shell", "args": {"cmd": "whoami"}}]}
    return {"text": "done", "tool_calls": []}
'''

def _ignore(_, names):
    return [n for n in names if n == "__pycache__" or n.endswith(".pyc")]

class Stack:
    def __init__(self, test, *, device_caps=("read", "propose"), experimental=False, allow_mutation=False, test_commands=None, hosts=None,
                 workspace_target="shop", device_token="dev-token-1", env_extra=None):
        self.test = test
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.close)
        self.base = Path(self.tmp.name).resolve()
        self.project = self.base / "shop"
        self.project.mkdir()
        (self.project / "a.txt").write_text("alpha\n", encoding="utf-8")
        (self.project / ".env").write_text("KEY=STACK-SECRET", encoding="utf-8")
        (self.base / "outside.txt").write_text("STACK-SECRET", encoding="utf-8")
        self.caps, self.experimental, self.allow_mutation, self.test_commands = list(device_caps), experimental, allow_mutation, test_commands
        self.workspace_target, self.device_token, self.env_extra = workspace_target, device_token, dict(env_extra or {})
        self.credentials = {"chat-a": "credential-a", "chat-b": "credential-b"}
        self.hosts = hosts
        self.procs = {}
        # minimal trees: the service gets NO tool, file or Git code; the device gets NO service code
        self.service_tree, self.device_tree = self.base / "service_tree", self.base / "device_tree"
        for name in ("protocol", "coding_service"):
            shutil.copytree(ROOT / name, self.service_tree / name, ignore=_ignore)
        for name in ("protocol", "device_agent", "code_attach", "coding_agent"):
            shutil.copytree(ROOT / name, self.device_tree / name, ignore=_ignore)
        (self.base / "model.py").write_text(SCRIPTED_MODEL, encoding="utf-8")
        self.device_state, self.locks, self.db = self.base / "device_state", self.base / "locks", self.base / "service" / "state.db"
        self.db.parent.mkdir()

    # --- configuration ---
    def grants(self):
        ws = {"root": str(self.project), "capabilities": self.caps}
        if self.test_commands:
            ws["test_commands"] = self.test_commands
        return {"device_id": "dev1", "token_sha256": hashlib.sha256(self.device_token.encode()).hexdigest(), "state_dir": str(self.device_state), "lock_dir": str(self.locks),
                "experimental_mutation": self.experimental, "workspaces": {"shop": ws}}

    def service_config(self):
        hosts = self.hosts or {h: {"credential_sha256": hashlib.sha256(c.encode()).hexdigest(), "workspaces": {"shop": {"device": "dev1", "workspace": self.workspace_target}}}
                               for h, c in list(self.credentials.items())[:1]}
        if self.hosts is None:   # host B exists but has no workspaces: it must not reach host A's anything
            hosts["chat-b"] = {"credential_sha256": hashlib.sha256(self.credentials["chat-b"].encode()).hexdigest(), "workspaces": {}}
        return {"db_path": str(self.db), "allow_mutation": self.allow_mutation, "max_tasks": 8, "model": str(self.base / "model.py") + ":complete",
                "devices": {"dev1": {"url": self.device_url, "token_env": "SVC_DEVICE_TOKEN"}}, "hosts": hosts}

    # --- processes ---
    def _launch(self, name, tree, module, config_path, env_extra):
        ready = self.base / f"{name}.ready"
        ready.unlink(missing_ok=True)
        env = {**os.environ, "PYTHONPATH": str(tree), "PYTHONDONTWRITEBYTECODE": "1", **env_extra, **self.env_extra}
        env.pop("PYTHONHOME", None)
        out, err = open(self.base / f"{name}.out", "ab"), open(self.base / f"{name}.err", "ab")
        self.handles = getattr(self, "handles", []) + [out, err]
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        proc = subprocess.Popen([sys.executable, "-m", module, "--config", str(config_path), "--port", "0", "--ready-file", str(ready)], cwd=str(tree), env=env, stdout=out, stderr=err, creationflags=flags)
        self.procs[name] = proc
        deadline = time.time() + 40
        while time.time() < deadline:
            if ready.exists() and ready.stat().st_size:
                try:
                    return json.loads(ready.read_text(encoding="utf-8"))["port"]
                except ValueError:
                    pass
            if proc.poll() is not None:
                raise AssertionError(f"{name} exited early with {proc.returncode}: {self.log(name)[-1500:]}")
            time.sleep(0.05)
        raise AssertionError(f"{name} did not become ready: {self.log(name)[-1500:]}")

    def log(self, name):
        return (self.base / f"{name}.err").read_text(encoding="utf-8", errors="replace") + (self.base / f"{name}.out").read_text(encoding="utf-8", errors="replace")

    def start_device(self):
        path = self.base / "grants.json"
        path.write_text(json.dumps(self.grants()), encoding="utf-8")
        port = self._launch("device", self.device_tree, "device_agent", path, {})
        self.device_url = f"http://127.0.0.1:{port}"

    def start_service(self):
        path = self.base / "service.json"
        path.write_text(json.dumps(self.service_config()), encoding="utf-8")
        port = self._launch("service", self.service_tree, "coding_service", path, {"SVC_DEVICE_TOKEN": self.device_token})
        self.service_url = f"http://127.0.0.1:{port}"

    def start(self):
        self.start_device()
        self.start_service()
        return self

    def stop(self, name, kill=False):
        proc = self.procs.get(name)
        if proc is None or proc.poll() is not None:
            return
        if kill:
            proc.kill()
        elif os.name == "nt":
            proc.terminate()
        else:
            proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    def gateway(self, host="chat-a", **kw):
        from host_client import AgentGateway
        return AgentGateway(self.service_url, self.credentials[host], timeout=15, retries=kw.pop("retries", 2), backoff=0.05, **kw)

    def close(self):
        for name in list(self.procs):
            self.stop(name, kill=True)
        for handle in getattr(self, "handles", []):
            handle.close()
        self.tmp.cleanup()

def drain(gw, user, handle, seconds=40):
    return list(gw.observe_coding(user, handle, poll_interval=0.05, max_seconds=seconds))
