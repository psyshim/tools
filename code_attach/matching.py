"""Locating the text an edit refers to, and explaining failures so the model can correct itself in one more step.

The commonest edit failures are not logic errors but copying errors: tabs shown as spaces, line endings, a stale or
ambiguous snippet. `locate` therefore tolerates line-ending differences (safe: same characters), and for everything else
fails with the *actual* text and line numbers instead of a bare "not found". It never guesses across whitespace.
"""
import difflib
import re

MAX_SNIPPET = 400
MAX_REPORTED = 10
FUZZY_MIN_RATIO = 0.6
FUZZY_MAX_LINES = 80

class MatchError(ValueError):
    """A ValueError (handled as an ERROR tool result) whose message is written for the model."""

def _line_of(text, index):
    return text.count("\n", 0, index) + 1

def _snippet(text):
    text = text.replace("\r", "")
    return text if len(text) <= MAX_SNIPPET else text[:MAX_SNIPPET] + " ..."

def _lines_report(text, spans):
    return ", ".join(str(_line_of(text, s)) for s, _ in spans[:MAX_REPORTED]) + (", ..." if len(spans) > MAX_REPORTED else "")

def _indent_style(block):
    lines = [l for l in block.splitlines() if l[:1] in (" ", "\t")]
    if not lines:
        return "no indentation"
    tabs = sum(l.startswith("\t") for l in lines)
    return "TAB indentation" if tabs > len(lines) / 2 else "space indentation"

def _eol_tolerant(old):
    return r"\r?\n".join(re.escape(part) for part in re.split(r"\r?\n", old))

def _whitespace_tolerant(old):
    parts = []
    for line in re.split(r"\r?\n", old):
        stripped = line.strip()
        parts.append(r"[ \t]*" + r"[ \t]+".join(re.escape(tok) for tok in stripped.split()) + r"[ \t]*" if stripped else r"[ \t]*")
    return r"\r?\n".join(parts)

def _fuzzy_hint(text, old):
    """Closest block of the same line count, by similarity of stripped lines; None if nothing is convincingly close."""
    want = [l.strip() for l in old.splitlines() if l.strip()]
    if not want or len(want) > FUZZY_MAX_LINES:
        return None
    lines = text.splitlines()
    best, best_at = 0.0, None
    anchor = want[0]
    for i, line in enumerate(lines):
        if difflib.SequenceMatcher(None, line.strip(), anchor).ratio() < 0.6:
            continue
        window = [l.strip() for l in lines[i:i + len(want)]]
        ratio = difflib.SequenceMatcher(None, "\n".join(window), "\n".join(want)).ratio()
        if ratio > best:
            best, best_at = ratio, i
    if best_at is None or best < FUZZY_MIN_RATIO:
        return None
    return best_at + 1, _snippet("\n".join(lines[best_at:best_at + len(want)]))

def locate(text, old):
    """Return (start, end) of the single place `old` refers to in `text`, or raise MatchError with guidance."""
    if not old:
        raise MatchError("old text is empty; give the exact text to replace")
    exact = [(m.start(), m.end()) for m in re.finditer(re.escape(old), text)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise MatchError(f"old text occurs {len(exact)} times (lines {_lines_report(text, exact)}); include more surrounding lines so it matches exactly one place")
    tolerant = [(m.start(), m.end()) for m in re.finditer(_eol_tolerant(old), text)]
    if len(tolerant) == 1:
        return tolerant[0]
    if len(tolerant) > 1:
        raise MatchError(f"old text occurs {len(tolerant)} times ignoring line endings (lines {_lines_report(text, tolerant)}); include more surrounding lines")
    spaced = [(m.start(), m.end()) for m in re.finditer(_whitespace_tolerant(old), text)]
    if spaced:
        s, e = spaced[0]
        more = f" (and {len(spaced) - 1} other place(s) at lines {_lines_report(text, spaced[1:])})" if len(spaced) > 1 else ""
        raise MatchError(f"no exact match, but the same text with different whitespace is at line {_line_of(text, s)}{more}. The file uses "
                         f"{_indent_style(text[s:e])}. Actual text:\n{_snippet(text[s:e])}\nCopy it exactly (including tabs/spaces).")
    near = _fuzzy_hint(text, old)
    if near:
        raise MatchError(f"no match. The closest text is at line {near[0]}:\n{near[1]}\nRe-read the file around that line and copy the exact text.")
    raise MatchError("old text not found anywhere in the file; use read_file or search_text to find the current text")

def adapt_eol(text, start, end, new):
    """Give `new` the line ending of the place it replaces (the region itself, else the terminator of its line, else the
    file's dominant ending), so an LF-writing model never creates mixed endings."""
    if "\r" in new or "\n" not in new:
        return new
    region = text[start:end]
    if "\r\n" in region:
        crlf = True
    elif "\n" in region:
        crlf = False
    else:
        tail = text[end:end + 2]
        crlf = tail == "\r\n" or (tail[:1] != "\n" and text.count("\r\n") * 2 > text.count("\n"))
    return new.replace("\n", "\r\n") if crlf else new

