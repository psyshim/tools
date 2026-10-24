"""Translate the engine's internal events into protocol envelopes. Pure, deterministic, append-only.

The internal list only ever grows, so mapping it again gives the same envelopes with the same ids: the protocol cursor
(`event_id`) is stable across polls, reconnects and replays without any stored state. One internal event may become several
protocol events; unknown internal event types are dropped (forward compatibility) rather than leaked.

Outcomes stay distinct: a proposal is `proposal_completed` (applied=false, nothing on disk changed); only a successful apply
yields `mutation_applied` with a receipt.
"""
import re
import json

from protocol import PROTOCOL_VERSION

MAX_TEXT = 60000
MAX_DIFF = 32000
MAX_OUTPUT = 8000
_DIFF_FILE = re.compile(r"^\+\+\+ b/(.+?)\s*$", re.M)

def _clip(text, limit):
    text = text if isinstance(text, str) else str(text)
    text = text.encode("utf-8", "backslashreplace").decode("utf-8")
    size = lambda value: len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    if size(text) <= limit:
        return text
    marker, low, high = " ...[truncated]", 0, min(len(text), limit)
    while low < high:
        middle = (low + high + 1) // 2
        if size(text[:middle] + marker) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low] + marker

def _files_of(result):
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    if isinstance(summary.get("files"), list):
        return [{"path": str(f.get("path")), "op": str(f.get("op", "write"))} for f in summary["files"] if isinstance(f, dict) and f.get("path")]
    diff = result.get("diff") or summary.get("diff") or ""
    paths = _DIFF_FILE.findall(diff) if isinstance(diff, str) else []
    if paths:
        return [{"path": p, "op": "write"} for p in dict.fromkeys(paths)]
    path = summary.get("path") or result.get("path")
    return [{"path": str(path), "op": str(summary.get("kind", "write"))}] if path else []

def _tool_events(data):
    tool = str(data.get("tool", ""))
    result = data.get("result")
    if result is None:   # e.g. NOT_APPROVED
        return [("tool_activity", {"tool": tool, "status": str(data.get("status", "UNKNOWN"))})]
    if not isinstance(result, dict):
        return [("tool_activity", {"tool": tool, "status": "OK"})]
    status = str(result.get("status") or "OK")
    out = [("tool_activity", {"tool": tool, "status": status, **({"detail": _clip(result["error"], 300)} if result.get("error") else {})})]
    if tool == "run_tests":
        body = {"status": status, "output": _clip(result.get("output", ""), MAX_OUTPUT)}
        for key in ("exit_code", "parsed", "verification"):
            if key in result:
                body[key] = result[key]
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > 60000:
            body.pop("parsed", None)
            body["parsed_truncated"] = True
        out.append(("test_result", body))
    elif tool == "apply_proposal":
        if status == "APPLIED":
            receipt = {k: v for k, v in result.items() if k in ("status", "path", "files", "sha256", "applied_id")}
            out.append(("mutation_applied", {"receipt": receipt}))
        else:
            notice = {"code": "mutation_not_applied", "status": status, "detail": _clip(result.get("detail", ""), 300)}
            if isinstance(result.get("op_id"), str):
                notice["op_id"] = result["op_id"][:128]   # lets an operator look the outcome up in the device journal
            out.append(("notice", notice))
    elif result.get("proposal_id"):
        diff = result.get("diff") or (result.get("summary") or {}).get("diff") or ""
        body = {"proposal_id": str(result["proposal_id"]), "files": _files_of(result), "applied": False}
        if diff:
            body["diff"] = _clip(diff, MAX_DIFF)
        out.append(("proposal_completed", body))
    return out

def map_event(event):
    """One internal event -> a list of (protocol_type, data). Pure; unknown internal types map to nothing."""
    kind, data = event.get("type"), event.get("data") or {}
    if kind in {"model_usage", "model_call_started"}:
        return [("notice", {"code": kind, **data})]
    if kind == "assistant":
        return [("assistant_message", {"text": _clip(data.get("text", ""), MAX_TEXT)})]
    if kind == "tool_result":
        return _tool_events(data)
    if kind == "approval_required":
        return [("approval_required", {k: data[k] for k in ("action_id", "fingerprint", "tool", "summary") if k in data})]
    if kind == "approval_decision":
        return [("approval_decision", {"action_id": data.get("action_id", ""), "approve": bool(data.get("approve"))})]
    if kind == "state":
        return [("state", {k: data[k] for k in ("state", "detail") if k in data})]
    if kind in ("tool_calls_dropped", "approval_expired", "cancel_requested"):
        return [("notice", {"code": kind, **{k: v for k, v in data.items() if isinstance(v, (str, int, bool))}})]
    return []   # anything else is internal and is not part of the contract

def envelope(task_id, event_id, kind, data):
    return {"protocol": PROTOCOL_VERSION, "task_id": task_id, "event_id": str(event_id), "type": kind, "data": data}

def to_protocol_events(task_id, internal_events):
    """Return the full list of protocol envelopes for a task, ids 1..n."""
    mapped = [pair for event in internal_events for pair in map_event(event)]
    return [envelope(task_id, i, kind, data) for i, (kind, data) in enumerate(mapped, 1)]
