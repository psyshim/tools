"""Fail-closed source-level architecture boundary checks.

This is an AST import scan only. It does NOT prove runtime privilege isolation;
sandboxed process tests are required for that (ADR-002 section 6).

Run: python -m unittest tests.test_architecture_boundaries -v
"""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "host_mock"
AGENT = ROOT / "coding_agent"
HOST_FORBIDDEN = {"coding_agent", "subprocess", "playwright", "git", "openai", "anthropic", "mcp", "shutil"}
HOST_FORBIDDEN_CALLS = {"exec", "eval", "open", "__import__"}
AGENT_FORBIDDEN = {"host_mock"}

def violations(source, forbidden_imports, forbidden_calls=(), allow_package_relative=False):
    result = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in forbidden_imports:
                    result.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            # level 1 (`from .x import`) stays inside the same package; anything
            # higher escapes it and is treated as a violation (fail-closed).
            escapes_package = node.level > 1 if allow_package_relative else node.level > 0
            if escapes_package or (node.module or "").split(".")[0] in forbidden_imports:
                result.append(f"from {'.' * node.level}{node.module or ''} import")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forbidden_calls:
            result.append(f"call {node.func.id}()")
    return result

class ArchitectureBoundaryTests(unittest.TestCase):
    def test_host_adapter_has_no_agent_or_execution_dependencies(self):
        files = list(HOST.rglob("*.py"))
        self.assertTrue(files, "missing host mock package")
        for path in files:
            with self.subTest(path=path):
                self.assertEqual(violations(path.read_text(encoding="utf-8"), HOST_FORBIDDEN, HOST_FORBIDDEN_CALLS), [])

    def test_agent_has_no_host_mock_dependency(self):
        files = list(AGENT.rglob("*.py"))
        self.assertTrue(files, "missing agent package")
        for path in files:
            with self.subTest(path=path):
                self.assertEqual(violations(path.read_text(encoding="utf-8"), AGENT_FORBIDDEN, allow_package_relative=True), [])

    def test_guard_detects_violation_examples(self):
        self.assertTrue(violations("import coding_agent.service", HOST_FORBIDDEN))
        self.assertTrue(violations("import subprocess", HOST_FORBIDDEN))
        self.assertTrue(violations("from host_mock import server", AGENT_FORBIDDEN))
        self.assertTrue(violations("open('secret')", HOST_FORBIDDEN, HOST_FORBIDDEN_CALLS))
        # same-package relative import is allowed for the agent...
        self.assertEqual(violations("from .journal import X", AGENT_FORBIDDEN, allow_package_relative=True), [])
        # ...but a relative import that escapes the package is not
        self.assertTrue(violations("from ..host_mock import server", AGENT_FORBIDDEN, allow_package_relative=True))

if __name__ == "__main__":
    unittest.main()
