"""Glob semantics for find_files / search_text."""
import fnmatch
import re

# Directories that are build output, dependencies or tool state: skipped when walking a project.
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".tox", "dist", "build", ".idea"}

def glob_match(rel, name, pattern):
    """Patterns without '/' match the file name; patterns with '/' match the relative path ('**' spans directories)."""
    if "/" not in pattern:
        return fnmatch.fnmatch(name, pattern)
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch("".join(out), rel) is not None
