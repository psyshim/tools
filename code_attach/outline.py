"""Structure extraction for navigation: file outlines and whole-word matching. Pure functions, no I/O.

Python uses the real parser; other languages use line-based heuristics that favour precision over recall. An outline is a
map for the model ("where is X defined"), never a source of truth for edits: edits still go through exact-text proposals.
"""
import ast
import re

MAX_ENTRIES = 500

_RX = {
    "js": [("class", r"^\s*(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"),
           ("function", r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("),
           ("function", r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>"),
           ("interface", r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)"),
           ("type", r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*="),
           ("enum", r"^\s*(?:export\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)")],
    "jvm": [("class", r"^\s*(?:(?:public|private|protected|internal|abstract|final|static|sealed|data|open|partial)\s+)*(?:class|record|object)\s+([A-Za-z_]\w*)"),
            ("interface", r"^\s*(?:(?:public|private|protected|internal|sealed|partial)\s+)*interface\s+([A-Za-z_]\w*)"),
            ("enum", r"^\s*(?:(?:public|private|protected|internal)\s+)*enum\s+(?:class\s+)?([A-Za-z_]\w*)"),
            ("function", r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|override|virtual|async|synchronized|open)\s+)+[\w<>\[\],.?\s]+?\s+([A-Za-z_]\w*)\s*\([^;]*$"),
            ("function", r"^\s*(?:(?:public|private|protected|internal|override|open|suspend)\s+)*fun\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?([A-Za-z_]\w*)\s*\(")],
    "go": [("function", r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*[(\[]"), ("type", r"^type\s+([A-Za-z_]\w*)\s+")],
    "rust": [("function", r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)"),
             ("type", r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|union)\s+([A-Za-z_]\w*)"), ("impl", r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>]+\s+for\s+)?([A-Za-z_]\w*)")],
    "c": [("type", r"^\s*(?:typedef\s+)?(?:struct|class|enum|union|namespace)\s+([A-Za-z_]\w*)"),
          ("function", r"^[A-Za-z_][\w\s\*&:<>,~]*?[\s\*&]([A-Za-z_~][\w:~]*)\s*\([^;]*\)\s*(?:const\s*)?(?:\{|$)")],
    "ruby": [("class", r"^\s*(?:class|module)\s+([A-Z][\w:]*)"), ("function", r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[?!=]?)")],
    "php": [("class", r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait|enum)\s+([A-Za-z_]\w*)"), ("function", r"^\s*(?:(?:public|private|protected|static|abstract|final)\s+)*function\s+&?([A-Za-z_]\w*)")],
    "shell": [("function", r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{?"), ("function", r"^\s*function\s+([A-Za-z_][\w-]*)")],
    "powershell": [("function", r"^\s*(?:function|filter)\s+([A-Za-z_][\w-]*)"), ("class", r"^\s*class\s+([A-Za-z_]\w*)")],
    "sql": [("table", r"(?i)^\s*create\s+(?:or\s+replace\s+)?(?:temporary\s+)?table\s+(?:if\s+not\s+exists\s+)?([\w.\"`\[\]]+)"),
            ("function", r"(?i)^\s*create\s+(?:or\s+replace\s+)?(?:function|procedure|view)\s+([\w.\"`\[\]]+)")],
    "markdown": [("heading", r"^(#{1,6})\s+(.+?)\s*#*\s*$")],
    "ini": [("section", r"^\s*\[([^\]\r\n]+)\]\s*$")],
}
_LANG = {".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "js", ".tsx": "js", ".java": "jvm", ".kt": "jvm", ".kts": "jvm", ".cs": "jvm", ".scala": "jvm",
         ".go": "go", ".rs": "rust", ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".hpp": "c", ".cxx": "c", ".rb": "ruby", ".php": "php", ".sh": "shell", ".bash": "shell",
         ".ps1": "powershell", ".psm1": "powershell", ".sql": "sql", ".md": "markdown", ".ini": "ini", ".cfg": "ini", ".toml": "ini", ".conf": "ini"}

def language_of(suffix):
    suffix = suffix.lower()
    return "python" if suffix in {".py", ".pyw"} else _LANG.get(suffix)

def _python(text):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    out = []
    def visit(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(node, ast.ClassDef) else ("method" if prefix else "function")
                out.append({"kind": kind, "name": prefix + node.name, "line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno)})
                if isinstance(node, ast.ClassDef):
                    visit(node.body, prefix + node.name + ".")
            elif not prefix and isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        out.append({"kind": "constant", "name": target.id, "line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno)})
    visit(tree.body, "")
    return out

def outline(text, suffix):
    """Return [{kind, name, line, end_line?}] for a file's text, capped at MAX_ENTRIES. Unknown languages give []."""
    lang = language_of(suffix)
    if lang is None:
        return []
    if lang == "python":
        parsed = _python(text)
        if parsed is not None:
            return parsed[:MAX_ENTRIES]
        rules = [("class", r"^\s*class\s+([A-Za-z_]\w*)"), ("function", r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)")]  # broken file: heuristics
    else:
        rules = _RX[lang]
    compiled = [(kind, re.compile(rx)) for kind, rx in rules]
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if len(line) > 400:
            continue
        for kind, rx in compiled:
            m = rx.search(line)
            if m:
                name = m.group(m.lastindex or 1)
                if lang == "markdown":
                    name = ("  " * (len(m.group(1)) - 1)) + m.group(2)
                out.append({"kind": kind, "name": name, "line": n})
                break
        if len(out) >= MAX_ENTRIES:
            break
    return out

def word_pattern(word):
    """Whole-word matcher built from escaped text only (no user regex, so no catastrophic backtracking)."""
    return re.compile(r"(?<![\w$])" + re.escape(word) + r"(?![\w$])", re.IGNORECASE)
