"""Pure converters between the neutral message format and OpenAI / Anthropic request/response shapes.

Neutral message: {"role": system|user|assistant|tool, "text", "tool_calls": [{"id","name","args"}], "tool_call_id","name","content"}.
No SDK imports: the host calls its own client with the returned payload.
"""
import json

def to_openai(messages, tools):
    out = []
    for m in messages:
        if m["role"] == "assistant":
            msg = {"role": "assistant", "content": m.get("text") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c.get("args", {}))}} for c in m["tool_calls"]]
            out.append(msg)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            out.append({"role": m["role"], "content": m["text"]})
    return {"messages": out, "tools": [{"type": "function", "function": t} for t in tools]}

def from_openai(response_message):
    """Tolerant: a malformed tool call becomes a call with empty name/args (Task answers it with an error) instead of raising."""
    calls = []
    for c in response_message.get("tool_calls") or []:
        c = c if isinstance(c, dict) else {}
        fn = c.get("function") if isinstance(c.get("function"), dict) else {}
        calls.append({"id": c.get("id"), "name": fn.get("name"), "args": _loads(fn.get("arguments"))})
    content = response_message.get("content")
    return {"text": content if isinstance(content, str) else "", "tool_calls": calls}

def to_anthropic(messages, tools):
    system = "\n".join(m["text"] for m in messages if m["role"] == "system")
    out = []
    for m in messages:
        if m["role"] == "system":
            continue
        if m["role"] == "assistant":
            blocks = ([{"type": "text", "text": m["text"]}] if m.get("text") else []) + [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c.get("args", {})} for c in m.get("tool_calls") or []]
            out.append({"role": "assistant", "content": blocks})
        elif m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        else:
            out.append({"role": "user", "content": m["text"]})
    return {"system": system, "messages": out, "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]}

def from_anthropic(content_blocks):
    blocks = [b for b in content_blocks or [] if isinstance(b, dict)]
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text" and isinstance(b.get("text", ""), str))
    calls = [{"id": b.get("id"), "name": b.get("name"), "args": b.get("input") if isinstance(b.get("input"), dict) else {}}
             for b in blocks if b.get("type") == "tool_use"]
    return {"text": text, "tool_calls": calls}

def _loads(s):
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}
