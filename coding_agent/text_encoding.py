"""Strict, reversible source decoding. No heuristic fallback or lossy replacement."""
import codecs

_BOMS = ((codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
         (codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"))
_ALLOWED = {"utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be", "cp932", "shift_jis"}


def normalize(encoding):
    if not isinstance(encoding, str):
        raise ValueError("encoding must be a string")
    try:
        value = codecs.lookup(encoding).name
    except LookupError:
        raise ValueError("unsupported source encoding") from None
    if value == "utf-8-sig":
        return "utf-8"
    if value not in _ALLOWED:
        raise ValueError("unsupported source encoding")
    return value


def decode_source(data, encoding=None):
    prefix, detected = next(((bom, name) for bom, name in _BOMS if data.startswith(bom)), (b"", None))
    chosen = normalize(encoding) if encoding is not None else detected or "utf-8"
    if detected is not None and chosen != detected:
        raise ValueError("requested encoding conflicts with BOM")
    text = data[len(prefix):].decode(chosen, errors="strict")
    if "\x00" in text:
        raise ValueError("NUL in source text; binary or incorrect encoding")
    if prefix + text.encode(chosen, errors="strict") != data:
        raise ValueError("source encoding does not round-trip exactly")
    return text, chosen, prefix


def encode_source(text, encoding, prefix=b""):
    return prefix + text.encode(normalize(encoding), errors="strict")


def encode_new_source(text, encoding="utf-8"):
    chosen = normalize(encoding)
    if "\x00" in text:
        raise ValueError("NUL in source text")
    requested_sig = codecs.lookup(encoding).name == "utf-8-sig"
    prefix = next((bom for bom, name in _BOMS if name == chosen), b"") if chosen.startswith(("utf-16", "utf-32")) or requested_sig else b""
    return encode_source(text, chosen, prefix)
