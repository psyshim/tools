"""Responsibility boundaries of code_attach, enforced as an AST import scan (not runtime isolation).

host layer  (router, providers, __init__): HTTP translation only; no execution, no tool code, no agent internals.
agent layer (engine, session, tools):      no HTTP framework, no network/model client; subprocess only in tools.py.
coding_agent (workspace primitives):       must not depend on code_attach.
"""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "code_attach"
NETWORK = {"openai", "anthropic", "httpx", "requests", "urllib", "socket", "http", "aiohttp"}

HOST_FILES = {"router.py", "providers.py", "__init__.py"}
HOST_FORBIDDEN = {"subprocess", "os", "shutil", "pathlib", "coding_agent", "host_mock"} | NETWORK
HOST_ALLOWED_RELATIVE = {"engine", "router", "providers", ""}

AGENT_FORBIDDEN = {"fastapi", "starlette", "pydantic", "host_mock"} | NETWORK
AGENT_RULES = {
    "engine.py": (AGENT_FORBIDDEN | {"subprocess", "coding_agent"}, {"session", "tools"}),
    "session.py": (AGENT_FORBIDDEN | {"subprocess", "coding_agent"}, {"schemas", "tools"}),
    "tools.py": (AGENT_FORBIDDEN | {"subprocess"}, {"globs", "privacy", "schemas", "testrun"}),
    "schemas.py": (AGENT_FORBIDDEN | {"subprocess", "coding_agent"}, set()),
    "globs.py": (AGENT_FORBIDDEN | {"subprocess", "coding_agent"}, set()),
    "privacy.py": (AGENT_FORBIDDEN | {"subprocess", "coding_agent"}, set()),
    "testrun.py": (AGENT_FORBIDDEN | {"coding_agent"}, set()),  # the only module that starts processes
    "activity.py": (AGENT_FORBIDDEN | {"subprocess"}, set()),
}

def imports(path):
    """Yield (top_module, relative_module_or_None) for every import in the file."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name.split(".")[0], None
        elif isinstance(node, ast.ImportFrom):
            if node.level == 1:
                yield "", (node.module or "").split(".")[0]
            elif node.level > 1:
                yield "..", None  # escapes the package
            else:
                yield (node.module or "").split(".")[0], None

class BoundaryTests(unittest.TestCase):
    def test_every_module_is_classified(self):
        names = {p.name for p in PKG.glob("*.py")}
        self.assertEqual(names, HOST_FILES | set(AGENT_RULES))

    def test_host_layer_has_no_execution_or_agent_internals(self):
        for name in HOST_FILES:
            for top, rel in imports(PKG / name):
                with self.subTest(file=name, top=top, rel=rel):
                    self.assertNotIn(top, HOST_FORBIDDEN | {".."})
                    if top == "":
                        self.assertIn(rel, HOST_ALLOWED_RELATIVE)

    def test_agent_layer_has_no_http_or_network(self):
        for name, (forbidden, allowed_rel) in AGENT_RULES.items():
            for top, rel in imports(PKG / name):
                with self.subTest(file=name, top=top, rel=rel):
                    self.assertNotIn(top, forbidden | {".."})
                    if top == "":
                        self.assertIn(rel, allowed_rel)

    def test_workspace_primitives_do_not_depend_on_attach(self):
        for path in (ROOT / "coding_agent").glob("*.py"):
            for top, _ in imports(path):
                with self.subTest(file=path.name):
                    self.assertNotEqual(top, "code_attach")

    def test_guard_detects_examples(self):
        tmp = PKG.parent / "tests" / "_scan_probe.py"
        try:
            tmp.write_text("import subprocess\nfrom ..x import y\nfrom .tools import ToolBox\n", encoding="utf-8")
            found = set(imports(tmp))
        finally:
            tmp.unlink()
        self.assertEqual(found, {("subprocess", None), ("..", None), ("", "tools")})

if __name__ == "__main__":
    unittest.main()
