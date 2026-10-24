"""Strict JSON at trust boundaries. No repair, I/O or application dependencies."""
import json
import math


def loads_object(raw, *, max_bytes=1048576, max_depth=64):
    """Decode bounded UTF-8 JSON; reject ambiguous keys and non-JSON numbers.

    Depth is checked lexically before decoding so nesting cannot exhaust the
    interpreter stack. Brackets inside strings do not count.
    """
    if isinstance(raw, bytes):
        if len(raw) > max_bytes:
            raise ValueError("JSON body too large")
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        raise ValueError("JSON body must be text")
    if len(raw.encode("utf-8")) > max_bytes:
        raise ValueError("JSON body too large")
    depth, quoted, escaped = 0, False, False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > max_depth:
                raise ValueError("JSON nesting too deep")
        elif char in "]}":
            depth -= 1

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            key.encode("utf-8")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("non-finite JSON number")

    def number(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return value

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    if not isinstance(value, dict):
        raise ValueError("JSON body must be an object")
    # Also reject escaped lone surrogates in values (including nested strings).
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return value


async def read_limited_body(request, max_bytes):
    """Bound memory while reading chunked requests, before JSON parsing."""
    declared = request.headers.get("content-length")
    if declared:
        if not declared.isascii() or not declared.isdigit() or len(declared) > 12:
            raise ValueError("invalid content length")
        if int(declared) > max_bytes:
            raise OverflowError("request body too large")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > max_bytes:
            raise OverflowError("request body too large")
        data.extend(chunk)
    return bytes(data)
