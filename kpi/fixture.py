"""The virtual VS Code project used by tests and by the real-model evaluation."""
CR, LF = chr(13), chr(10)

FILES = {
    ".vscode/settings.json": '{"python.defaultInterpreterPath": ".venv"}\n',
    ".vscode/launch.json": '{"configurations": []}\n',
    ".git/config": "[core]\n",
    "README.md": "# shop-service\n",
    "pyproject.toml": "[project]\nname='shop'\n",
    "src/shop/__init__.py": "",
    "src/shop/orders/__init__.py": "",
    "src/shop/orders/pricing.py": "def apply_discount(price, percent):\n    # percent is 0-100\n    return price - price * percent\n",
    "src/shop/utils/__init__.py": "",
    "src/shop/utils/config.py": (
        "from pathlib import Path\n\n"
        "def load_retry_limit():\n"
        "    ini = Path(__file__).resolve().parents[3] / 'deploy' / 'legacy' / 'settings_old.ini'\n"
        "    for line in ini.read_text(encoding='utf-8').splitlines():\n"
        "        if line.startswith('RETRY_LIMIT'):\n"
        "            return int(line.split('=')[1])\n"
        "    raise KeyError('RETRY_LIMIT')\n"
    ),
    "deploy/legacy/settings_old.ini": "[net]\nTIMEOUT = 30\nRETRY_LIMIT = 1\n",
    "tests/test_existing.py": "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
    "docs/メモ/仕様 メモ.md": "# 仕様\n再試行回数は RETRY_LIMIT で決まる。\n",
    "node_modules/lodash/index.js": "// RETRY_LIMIT noise\n",
    ".venv/lib/site.py": "# RETRY_LIMIT noise\n",
    "__pycache__/x.pyc": "RETRY_LIMIT",
}

def build(root):
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="")
    (root / "legacy_win").mkdir()
    (root / "legacy_win" / "crlf_module.py").write_bytes(("def total():" + CR + LF + "    return 1" + CR + LF).encode())
    (root / "data").mkdir()
    (root / "data" / "blob.bin").write_bytes(bytes([0, 1, 2]) + b"RETRY_LIMIT" + bytes(100))
    (root / "data" / "big.txt").write_text("RETRY_LIMIT\n" * 800000, encoding="utf-8")  # > 8 MiB
    (root / "data" / "many.txt").write_text("".join(f"RETRY_LIMIT line {i}\n" for i in range(200)), encoding="utf-8")
