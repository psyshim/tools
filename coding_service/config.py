"""Service configuration: which hosts may call it, which workspaces each may use (and on which device), which model it uses.

Refused as a whole, with every problem listed, when anything is malformed or unsafe. Secrets are never in the file: hosts are
identified by the SHA-256 of their credential, device tokens and model keys are read from environment variables it names.

    {
      "db_path": "C:/svc/state.db",
      "allow_mutation": false,
      "max_tasks": 32,
      "model": "C:/svc/model.py:complete"   |   {"openai_compatible": {"base_url": "https://...", "model": "...", "api_key_env": "SVC_MODEL_KEY"}},
      "devices": {"laptop": {"url": "http://127.0.0.1:8780", "token_env": "SVC_LAPTOP_TOKEN"}},
      "hosts": {"chat-backend": {"credential_sha256": "<hex>", "workspaces": {"shop": {"device": "laptop", "workspace": "shop"}}}}
    }
"""
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}

class ConfigError(Exception):
    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = list(problems)

@dataclass(frozen=True)
class ServiceConfig:
    db_path: Path
    allow_mutation: bool
    max_tasks: int
    model: object
    devices: dict      # id -> {"url", "token"}
    hosts: dict        # id -> {"credential_sha256", "workspaces": {id: {"device", "workspace"}}}

    def host_for_credential(self, credential):
        digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()
        for host_id, host in self.hosts.items():
            if hashlib_compare(digest, host["credential_sha256"]):
                return host_id
        return None

def hashlib_compare(a, b):
    import hmac
    return hmac.compare_digest(a, b)

def parse_config(raw, env=None):
    env = os.environ if env is None else env
    problems = []
    if not isinstance(raw, dict):
        raise ConfigError(["the configuration must be a JSON object"])
    problems += [f"unknown key {k!r}" for k in sorted(set(raw) - {"db_path", "allow_mutation", "max_tasks", "model", "devices", "hosts"})]
    db_path = raw.get("db_path")
    if not isinstance(db_path, str) or not Path(db_path).is_absolute():
        problems.append("db_path must be an absolute path")
        db_path = None
    allow = raw.get("allow_mutation", False)
    if not isinstance(allow, bool):
        problems.append("allow_mutation must be true or false")
    max_tasks = raw.get("max_tasks", 32)
    if type(max_tasks) is not int or not 1 <= max_tasks <= 1000:
        problems.append("max_tasks must be an integer from 1 to 1000")
    model = raw.get("model")
    if not (isinstance(model, str) and ":" in model) and not (isinstance(model, dict) and set(model) == {"openai_compatible"} and isinstance(model["openai_compatible"], dict)):
        problems.append("model must be 'module_or_file.py:function' or {\"openai_compatible\": {...}}")
    elif isinstance(model, dict):
        spec = model["openai_compatible"]
        if set(spec) != {"base_url", "model", "api_key_env"} or not all(isinstance(v, str) and v for v in spec.values()):
            problems.append("openai_compatible needs base_url, model and api_key_env (the NAME of an environment variable)")
        elif not env.get(spec["api_key_env"]):
            problems.append(f"environment variable {spec['api_key_env']} (model key) is not set")
    devices_raw = raw.get("devices")
    devices = {}
    if not isinstance(devices_raw, dict) or not devices_raw:
        problems.append("devices must be a non-empty object")
        devices_raw = {}
    for did, spec in devices_raw.items():
        where = f"device {did!r}"
        if not isinstance(did, str) or not ID.match(did) or not isinstance(spec, dict) or set(spec) != {"url", "token_env"}:
            problems.append(f"{where}: needs an id like [A-Za-z0-9_.-]+ and exactly url and token_env")
            continue
        parts = urlsplit(spec["url"]) if isinstance(spec["url"], str) else None
        if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
            problems.append(f"{where}: url must be http(s)")
            continue
        if parts.scheme == "http" and parts.hostname not in LOOPBACK:
            problems.append(f"{where}: plain http is only allowed to loopback; use https for a remote device")
        token = env.get(spec["token_env"]) if isinstance(spec["token_env"], str) else None
        if not token:
            problems.append(f"{where}: environment variable {spec['token_env']} (device token) is not set")
            continue
        devices[did] = {"url": spec["url"], "token": token}
    hosts_raw = raw.get("hosts")
    hosts, seen = {}, {}
    if not isinstance(hosts_raw, dict) or not hosts_raw:
        problems.append("hosts must be a non-empty object")
        hosts_raw = {}
    for hid, spec in hosts_raw.items():
        where = f"host {hid!r}"
        if not isinstance(hid, str) or not ID.match(hid) or not isinstance(spec, dict) or set(spec) != {"credential_sha256", "workspaces"}:
            problems.append(f"{where}: needs an id like [A-Za-z0-9_.-]+ and exactly credential_sha256 and workspaces")
            continue
        if not isinstance(spec["credential_sha256"], str) or not HEX64.match(spec["credential_sha256"]):
            problems.append(f"{where}: credential_sha256 must be 64 lowercase hex characters")
            continue
        if spec["credential_sha256"] in seen:
            problems.append(f"{where}: shares a credential with host {seen[spec['credential_sha256']]!r}")
        seen[spec["credential_sha256"]] = hid
        workspaces = {}
        if not isinstance(spec["workspaces"], dict):
            problems.append(f"{where}: workspaces must be an object")
            continue
        for wid, target in spec["workspaces"].items():
            if not isinstance(wid, str) or not ID.match(wid) or not isinstance(target, dict) or set(target) != {"device", "workspace"}:
                problems.append(f"{where}: workspace {wid!r} needs an id like [A-Za-z0-9_.-]+ and exactly device and workspace")
            elif target["device"] not in devices_raw:
                problems.append(f"{where}: workspace {wid!r} names unknown device {target['device']!r}")
            else:
                workspaces[wid] = dict(target)
        hosts[hid] = {"credential_sha256": spec["credential_sha256"], "workspaces": workspaces}
    if problems:
        raise ConfigError(problems)
    return ServiceConfig(Path(db_path), allow, max_tasks, model, devices, hosts)

def load_config(path, env=None):
    from protocol.json_boundary import loads_object
    try:
        with Path(path).open("rb") as handle:
            raw = loads_object(handle.read(1048577).decode("utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigError([f"cannot read the configuration: {type(exc).__name__}"]) from None
    return parse_config(raw, env)
