"""Where the model comes from. Configured by the service operator; the host never supplies or sees it.

A model is a function `complete(messages, tools) -> {"text": str, "tool_calls": [...]}` in the neutral message format.
"""
import importlib
import importlib.util
import json
import urllib.request
from pathlib import Path

from .providers import from_openai_response, to_openai

def _load_callable(spec):
    target, _, name = spec.rpartition(":")
    if not target or not name:
        raise ValueError("model spec must look like 'module_or_file.py:function'")
    if target.endswith(".py"):
        module_spec = importlib.util.spec_from_file_location(Path(target).stem, target)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(target)
    return getattr(module, name)

def openai_compatible(base_url, model, api_key):
    def complete(messages, tools):
        payload = {"model": model, **to_openai(messages, tools)}
        request = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", json.dumps(payload).encode("utf-8"),
                                         {"Content-Type": "application/json", "Authorization": "Bearer " + api_key})
        with urllib.request.urlopen(request, timeout=180) as response:
            return from_openai_response(response.read(4 * 1024 * 1024 + 1))
    return complete

def load_model(spec, env):
    if isinstance(spec, str):
        return _load_callable(spec)
    cfg = spec["openai_compatible"]
    return openai_compatible(cfg["base_url"], cfg["model"], env[cfg["api_key_env"]])
