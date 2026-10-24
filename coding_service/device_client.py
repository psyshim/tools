"""The service's only way to reach files, Git and test commands: typed intents to a Device Agent.

`RemoteToolbox` presents the same small surface the task loop uses (schemas / call / describe_effect / is_effect / cancel), but
every operation is a request to the device. The device decides what exists and what is allowed; this side cannot name a path,
a root, a command or a capability. Effects carry an operation id chosen BEFORE the first attempt so that a retry after a lost
response can never apply twice (the device journals it), and an unreachable device after an effect is reported as an unknown
outcome, never as success or failure.
"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from protocol.json_boundary import loads_object

MAX_RESPONSE = 4 * 1024 * 1024
ERROR_TYPES = {"ValueError": ValueError, "KeyError": KeyError, "TypeError": TypeError, "OSError": OSError, "UnicodeError": UnicodeError}

class DeviceUnavailable(OSError):
    """The device cannot be reached or refused the service's credentials."""

class DeviceClient:
    def __init__(self, base_url, token, timeout=60, retries=3, backoff=0.2, sleep=time.sleep):
        self.base, self._token, self.timeout, self.retries, self.backoff, self._sleep = base_url.rstrip("/"), token, timeout, max(1, retries), backoff, sleep

    def _request(self, method, path, body=None, timeout=None, repeatable=True):
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8") if body is not None else None
        headers = {"Authorization": "Bearer " + self._token, "Content-Type": "application/json", "Accept": "application/json"}
        last = None
        for attempt in range(self.retries if repeatable else 1):
            request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                    raw, status = response.read(MAX_RESPONSE + 1), response.status
            except urllib.error.HTTPError as error:
                raw, status = error.read(MAX_RESPONSE + 1), error.code
            except OSError as exc:
                last = DeviceUnavailable(f"device unreachable: {type(exc).__name__}")
                if attempt + 1 < self.retries and repeatable:
                    self._sleep(self.backoff * (2 ** attempt))
                continue
            if len(raw) > MAX_RESPONSE:
                raise DeviceUnavailable("device answer too large")
            if status in (401, 403):
                raise DeviceUnavailable("device refused the service credentials")
            try:
                payload = loads_object(raw, max_bytes=MAX_RESPONSE)
            except (ValueError, RecursionError):
                raise DeviceUnavailable("device answered with something that is not JSON") from None
            if not isinstance(payload, dict):
                raise DeviceUnavailable("device answered with something that is not an object")
            return status, payload
        raise last or DeviceUnavailable("device unreachable")

    def info(self):
        return self._request("GET", "/v1/info")[1]

    def open_session(self, workspace_id, session_id):
        status, payload = self._request("POST", "/v1/sessions", {"workspace_id": workspace_id, "session_id": session_id})
        if status != 200:
            raise ValueError("device refused the workspace: " + str(payload.get("error", {}).get("code", status)))
        return payload

    def schemas(self, session_id):
        return self._request("GET", f"/v1/sessions/{session_id}/schemas")[1]["schemas"]

    def call(self, session_id, tool, args, op_id=None, timeout=None):
        body = {"tool": tool, "args": args}
        if op_id:
            body["op_id"] = op_id
        return self._request("POST", f"/v1/sessions/{session_id}/call", body, timeout=timeout)

    def describe(self, session_id, tool, args):
        return self._request("POST", f"/v1/sessions/{session_id}/describe", {"tool": tool, "args": args})[1]

    def cancel(self, session_id):
        try:
            self._request("POST", f"/v1/sessions/{session_id}/cancel", {}, timeout=10, repeatable=False)
        except DeviceUnavailable:
            pass   # cancelling must never raise; the loop stops on its own flag regardless

    def close_session(self, session_id):
        try:
            self._request("DELETE", f"/v1/sessions/{session_id}", timeout=10, repeatable=False)
        except DeviceUnavailable:
            pass

    def operation(self, op_id):
        return self._request("GET", f"/v1/operations/{op_id}")[1]

def _raise(error):
    kind = ERROR_TYPES.get(error.get("type"), ValueError)
    raise kind(str(error.get("message", "tool failed"))[:2000])

class RemoteToolbox:
    """Drop-in for the loop's toolbox, backed by a device session. `allow_mutation` mirrors what the device granted AND the
    service policy allows."""
    def __init__(self, device, workspace_id, session_id, allow_effects=False):
        self.device, self.session_id = device, session_id
        info = device.open_session(workspace_id, session_id)
        self._effects = set(info.get("effect_tools", [])) if allow_effects else set()
        self.allow_mutation = bool(self._effects)
        self._schemas = None

    def schemas(self):
        if self._schemas is None:
            self._schemas = [s for s in self.device.schemas(self.session_id) if s["name"] not in {"apply_proposal", "run_tests", "propose_undo"} or s["name"] in self._effects or (s["name"] == "propose_undo" and self.allow_mutation)]
        return self._schemas

    def is_effect(self, name):
        return name in self._effects

    def scrub(self, text):
        return text   # the device scrubs everything it returns; the service holds no path to scrub

    def describe_effect(self, name, args):
        if name not in self._effects:
            raise ValueError("this action is not granted here")
        payload = self.device.describe(self.session_id, name, args)
        if "error" in payload:
            _raise(payload["error"])
        return payload["summary"], payload["fingerprint"]

    def _op_id(self, call_id):
        raw = str(call_id or "")
        return (re.sub(r"[^A-Za-z0-9_.:-]", "_", self.session_id)[:60] + "." + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40])

    def call(self, name, args, call_id=None):
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        effect = name in self._effects
        if not effect and name in {"apply_proposal", "run_tests", "propose_undo"}:
            raise ValueError("this action is not granted here; present the proposal to the user instead")
        op_id = self._op_id(call_id) if effect else None
        try:
            status, payload = self.device.call(self.session_id, name, args, op_id, timeout=3700 if effect else None)
        except DeviceUnavailable:
            if effect:
                return {"status": "UNKNOWN_OUTCOME", "op_id": op_id, "detail": "the device could not be reached; the action may or may not have happened"}
            raise
        if status >= 400:
            raise ValueError("device refused the request: " + str(payload.get("error", {}).get("code", status)))
        if payload.get("unknown_outcome"):
            details = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            return {**details, "status": "UNKNOWN_OUTCOME", "op_id": payload.get("op_id", op_id), "detail": "an earlier attempt may or may not have taken effect"}
        if "error" in payload:
            _raise(payload["error"])
        return payload["result"]

    def request_cancel(self):
        self.device.cancel(self.session_id)

    def close(self):
        self.device.close_session(self.session_id)
