"""Navigation tools: where is X defined and used, what is in this file, what kind of project is this. Read-only.

Mixin for ToolBox (uses ws and _files). Heuristic outlines are a map, not proof: the model confirms with read_file.
"""
import time

from coding_agent.text_encoding import decode_source
from .detect import overview
from .globs import SKIP_DIRS
from .outline import MAX_ENTRIES, language_of, outline, word_pattern

MAX_SYMBOL_FILE_BYTES = 2 * 1024 * 1024
MAX_DEFINITIONS = 50
MAX_REFERENCES = 50
REFERENCES_PER_FILE = 5
TIME_BUDGET_SECONDS = 20

class NavTools:
    def t_outline_file(self, path, encoding=None):
        target = self.ws.resolve(path)
        if target.is_symlink() or not target.is_file():
            raise ValueError("not a regular file")
        if target.stat().st_size > MAX_SYMBOL_FILE_BYTES * 4:
            raise ValueError("file too large to outline")
        text, _, _ = decode_source(target.read_bytes(), encoding)
        language = language_of(target.suffix)
        entries = outline(text, target.suffix)
        return {"path": target.relative_to(self.ws.root).as_posix(), "language": language, "lines": text.count("\n") + 1,
                "entries": entries, "truncated": len(entries) >= MAX_ENTRIES,
                "note": None if language else "no outline support for this file type; use read_file or search_text"}

    def t_find_symbol(self, name, path=".", kind=None):
        if not isinstance(name, str) or not 0 < len(name) <= 200:
            raise ValueError("name must be a non-empty string of at most 200 characters")
        if kind is not None and not isinstance(kind, str):
            raise ValueError("kind must be a string such as function, class, method, type")
        bare = name.split(".")[-1]
        wanted = word_pattern(bare)
        definitions, references, per_file = [], [], {}
        deadline, scanned, limited = time.monotonic() + TIME_BUDGET_SECONDS, 0, False
        for f in self.ws.iter_files(path, SKIP_DIRS, 20000):
            if language_of(f.suffix) is None:
                continue
            if time.monotonic() > deadline:
                limited = True
                break
            try:
                if f.stat().st_size > MAX_SYMBOL_FILE_BYTES:
                    continue
                text, _, _ = decode_source(f.read_bytes(), None)
            except (OSError, UnicodeError, ValueError):
                continue
            scanned += 1
            if bare.lower() not in text.lower():
                continue
            rel = f.relative_to(self.ws.root).as_posix()
            defined_lines = set()
            for entry in outline(text, f.suffix):
                entry_name = entry["name"].strip()
                matches_name = (entry_name == name or entry_name.endswith("." + name)) if "." in name else entry_name.split(".")[-1] == name
                if matches_name and (kind is None or entry["kind"] == kind):
                    defined_lines.add(entry["line"])
                    if len(definitions) < MAX_DEFINITIONS:
                        definitions.append({"path": rel, "line": entry["line"], "kind": entry["kind"], "name": entry["name"]})
            for n, line in enumerate(text.splitlines(), 1):
                if n in defined_lines or len(line) > 2000:
                    continue
                m = wanted.search(line)
                if m and len(references) < MAX_REFERENCES and per_file.get(rel, 0) < REFERENCES_PER_FILE:
                    per_file[rel] = per_file.get(rel, 0) + 1
                    references.append({"path": rel, "line": n, "column": m.start() + 1, "text": line.strip()[:200]})
        return {"name": name, "definitions": definitions, "references": references, "files_scanned": scanned,
                "references_capped": len(references) >= MAX_REFERENCES, "time_limited": limited}

    def t_project_overview(self):
        result = overview(self.ws, SKIP_DIRS)
        result["configured_commands"] = list(self.command_specs)
        return result
