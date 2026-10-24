"""Every way a path can be written in text that must not leave the process.

One directory has several spellings: as given, resolved, forward slashes, escaped backslashes (as in a repr), and on Windows the
long and the 8.3 short form (`C:\\Users\\RUNNER~1\\...` is the same folder as `C:\\Users\\runneradmin\\...`). A scrubber that knows
only one of them leaks the others, so both scrubbers (code_attach.privacy and coding_agent.git_ops) use this single source.
"""
import os
from pathlib import Path

BS = chr(92)
_WIN_BUFFER = 32768


def _windows_alias(path, function):
    import ctypes
    buffer = ctypes.create_unicode_buffer(_WIN_BUFFER)
    size = getattr(ctypes.windll.kernel32, function)(str(path), buffer, _WIN_BUFFER)
    return buffer.value if 0 < size < _WIN_BUFFER else None


def spellings(path):
    """All textual forms of `path`, longest first. Never raises; a path that does not exist simply has fewer aliases."""
    bases = []
    for candidate in (path, None):
        try:
            value = Path(path) if candidate is not None else Path(path).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        bases.append(value)
    forms = set()
    for base in bases:
        texts = {str(base)}
        if os.name == "nt":
            for function in ("GetLongPathNameW", "GetShortPathNameW"):
                try:
                    alias = _windows_alias(base, function)
                except (OSError, AttributeError, ValueError):
                    alias = None
                if alias:
                    texts.add(alias)
        for text in texts:
            stripped = text[4:] if text.startswith(BS * 2 + "?" + BS) else text
            for form in (text, stripped):
                forms.add(form)
                forms.add(Path(form).as_posix())
                forms.add(form.replace(BS, BS + BS))
    return sorted((f for f in forms if f), key=len, reverse=True)
