"""Device grants. The device, not the service, decides which folders exist, what may be done in each, and where private state lives.

A grants file that is malformed, ambiguous or unsafe is REFUSED as a whole and the device does not start: it never starts in
a partly valid state. Every refusal is listed so the owner can fix the file in one pass.

    {
      "device_id": "laptop-1",
      "token_sha256": "<sha256 hex of the bearer token the coding service will present>",
      "state_dir": "C:/Users/me/.device-agent",                  # journal, locks, recovery copies; outside every workspace
      "experimental_mutation": false,                            # without this, grants of apply/git_commit/test are refused
      "workspaces": {
        "shop": {"root": "C:/Users/me/code/shop",
                 "capabilities": ["read", "propose"],           # read, propose, and (experimental only) apply, git_commit, test
                 "test_commands": {"unit": ["python", "-m", "pytest", "-q"]}}
      }
    }
"""
import re
from dataclasses import dataclass
from pathlib import Path

from code_attach.testrun import normalize_commands

CAPABILITIES = ("read", "propose", "apply", "git_commit", "test")
EFFECT_CAPABILITIES = ("apply", "git_commit", "test")
ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")

class ConfigError(Exception):
    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = list(problems)

@dataclass(frozen=True)
class Workspace:
    id: str
    root: Path
    capabilities: frozenset
    test_commands: dict

    @property
    def effects(self):
        return frozenset(self.capabilities & set(EFFECT_CAPABILITIES))

@dataclass(frozen=True)
class DeviceConfig:
    device_id: str
    token_sha256: str
    state_dir: Path
    experimental_mutation: bool
    allow_remote_clients: bool
    workspaces: dict
    lock_dir: object = None

def _inside(child, parent):
    return child == parent or parent in child.parents

def parse_config(raw):
    problems = []
    if not isinstance(raw, dict):
        raise ConfigError(["the grants file must contain a JSON object"])
    allowed = {"device_id", "token_sha256", "state_dir", "experimental_mutation", "allow_remote_clients", "workspaces", "lock_dir"}
    problems += [f"unknown key {k!r}" for k in sorted(set(raw) - allowed)]
    device_id, token = raw.get("device_id"), raw.get("token_sha256")
    if not isinstance(device_id, str) or not ID.match(device_id):
        problems.append("device_id must match [A-Za-z0-9_.-]{1,64}")
    if not isinstance(token, str) or not HEX64.match(token):
        problems.append("token_sha256 must be 64 lowercase hex characters (the sha256 of the bearer token)")
    experimental = raw.get("experimental_mutation", False)
    remote = raw.get("allow_remote_clients", False)
    for name, value in (("experimental_mutation", experimental), ("allow_remote_clients", remote)):
        if not isinstance(value, bool):
            problems.append(f"{name} must be true or false")
    state_dir = None
    try:
        if not isinstance(raw.get("state_dir"), str) or not Path(raw["state_dir"]).is_absolute():
            problems.append("state_dir must be an absolute path")
        else:
            state_dir = Path(raw["state_dir"]).resolve()
    except (OSError, ValueError):
        problems.append("state_dir is not a usable path")
    workspaces_raw = raw.get("workspaces")
    if not isinstance(workspaces_raw, dict):
        problems.append("workspaces must be an object")
        workspaces_raw = {}
    workspaces, roots = {}, {}
    for wid, spec in workspaces_raw.items():
        where = f"workspace {wid!r}"
        if not isinstance(wid, str) or not ID.match(wid):
            problems.append(f"{where}: id must match [A-Za-z0-9_.-]{{1,64}}")
            continue
        if not isinstance(spec, dict):
            problems.append(f"{where}: must be an object")
            continue
        problems += [f"{where}: unknown key {k!r}" for k in sorted(set(spec) - {"root", "capabilities", "test_commands"})]
        root = None
        try:
            if not isinstance(spec.get("root"), str) or not Path(spec["root"]).is_absolute():
                problems.append(f"{where}: root must be an absolute path")
            else:
                root = Path(spec["root"]).resolve(strict=True)
                if not root.is_dir():
                    problems.append(f"{where}: root is not a directory")
                    root = None
                elif root == Path(root.anchor) or root == Path.home().resolve():
                    problems.append(f"{where}: root is a filesystem root or the whole home directory; name a project folder")
                    root = None
        except (OSError, ValueError):
            problems.append(f"{where}: root does not exist")
        caps = spec.get("capabilities")
        if not isinstance(caps, list) or not caps or not all(isinstance(c, str) for c in caps):
            problems.append(f"{where}: capabilities must be a non-empty list of strings")
            caps = []
        else:
            problems += [f"{where}: unknown capability {c!r}" for c in caps if c not in CAPABILITIES]
            if "read" not in caps:
                problems.append(f"{where}: every workspace needs the 'read' capability")
            if "apply" in caps or "git_commit" in caps or "test" in caps:
                if not experimental:
                    problems.append(f"{where}: {sorted(set(caps) & set(EFFECT_CAPABILITIES))} need experimental_mutation=true; this device is read/propose only")
        try:
            commands = normalize_commands(spec.get("test_commands"))
        except ValueError as exc:
            problems.append(f"{where}: test_commands: {exc}")
            commands = {}
        if "test" in caps and not commands:
            problems.append(f"{where}: the 'test' capability needs at least one test_commands entry")
        if commands and "test" not in caps:
            problems.append(f"{where}: test_commands given without the 'test' capability")
        if root is not None:
            for other_id, other_root in roots.items():
                if _inside(root, other_root) or _inside(other_root, root):
                    problems.append(f"{where}: overlaps workspace {other_id!r} (workspaces must not be nested or identical)")
            roots[wid] = root
            workspaces[wid] = Workspace(wid, root, frozenset(caps), commands)
    if state_dir is not None:
        for wid, root in roots.items():
            if _inside(state_dir, root) or _inside(root, state_dir):
                problems.append(f"state_dir overlaps workspace {wid!r}: private state must live outside every workspace")
    if problems:
        raise ConfigError(problems)
    lock_dir = None
    if raw.get("lock_dir") is not None:
        if not isinstance(raw["lock_dir"], str) or not Path(raw["lock_dir"]).is_absolute():
            raise ConfigError(["lock_dir must be an absolute path"])
        lock_dir = Path(raw["lock_dir"]).resolve()
    return DeviceConfig(device_id, token, state_dir, experimental, remote, workspaces, lock_dir)

def load_config(path):
    from protocol.json_boundary import loads_object
    try:
        with Path(path).open("rb") as handle:
            raw = loads_object(handle.read(1048577).decode("utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigError([f"cannot read grants file: {type(exc).__name__}"]) from None
    return parse_config(raw)
