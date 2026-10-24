"""Provider-reported token accounting; missing usage is never invented as zero."""

TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_output_tokens")


def normalize_usage(raw, *, provider=None, model=None):
    raw = raw if isinstance(raw, dict) else {}
    def label(value):
        return "".join(c for c in str(value or "unknown")[:128] if ord(c) >= 32)
    result = {"provider": label(provider or raw.get("provider")), "model": label(model or raw.get("model"))}
    values = {"input_tokens": raw.get("input_tokens", raw.get("prompt_tokens")),
              "output_tokens": raw.get("output_tokens", raw.get("completion_tokens")),
              "total_tokens": raw.get("total_tokens"),
              "cached_input_tokens": raw.get("cached_input_tokens"),
              "reasoning_output_tokens": raw.get("reasoning_output_tokens")}
    for name, key in (("cached_input_tokens", "prompt_tokens_details"), ("reasoning_output_tokens", "completion_tokens_details")):
        details = raw.get(key)
        if isinstance(details, dict) and values[name] is None:
            values[name] = details.get("cached_tokens" if name == "cached_input_tokens" else "reasoning_tokens")
    invalid = raw.get("reason") == "invalid_usage"
    for key, value in values.items():
        if value is not None and (type(value) is not int or not 0 <= value <= 10**12):
            invalid = True
            value = None
        result[key] = value
    if result["total_tokens"] is None and result["input_tokens"] is not None and result["output_tokens"] is not None:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    a, b, total = (result[k] for k in TOKEN_FIELDS[:3])
    if a is not None and b is not None and total is not None and total != a + b:
        invalid = True
    for subset, parent in (("cached_input_tokens", "input_tokens"), ("reasoning_output_tokens", "output_tokens")):
        if result[subset] is not None and result[parent] is not None and result[subset] > result[parent]:
            invalid = True
    result["available"] = not invalid and all(result[k] is not None for k in TOKEN_FIELDS[:3])
    if not result["available"]:
        result["reason"] = "invalid_usage" if invalid else "provider_not_reported_or_partial"
    if invalid:
        # Incoherent telemetry cannot contribute to measured subtotals.
        result.update({key: None for key in TOKEN_FIELDS})
    return result
