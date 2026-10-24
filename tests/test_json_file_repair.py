"""Repair an actually broken config; preserve bytes and require a real parse oracle."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from code_attach.tools import ToolBox


class JsonFileRepairTests(unittest.TestCase):
    def test_broken_configuration_is_repairable_without_reformatting(self):
        fixtures = [
            ('{\r\n  "label": "日本語",\r\n  "enabled": true,\r\n}\r\n', ',\r\n}', '\r\n}', "utf-8"),
            ('{"label":"日本語","enabled":true', 'true', 'true}', "utf-8-sig"),
            ('{"label":"日本語","enabled":True}', 'True', 'true', "cp932"),
            ('{"label":"日本語","enabled" true}', '"enabled" true', '"enabled": true', "utf-16"),
        ]
        for content, old, new, encoding in fixtures:
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "settings.json"
                before = content.encode(encoding)
                path.write_bytes(before)
                explicit = "cp932" if encoding == "cp932" else None
                box = ToolBox(root)
                read = box.t_read_file("settings.json", encoding=explicit)
                self.assertTrue(read["lines"])
                check = [sys.executable, "-c", "import json,pathlib,sys; json.loads(pathlib.Path('settings.json').read_text(encoding=sys.argv[1]))", encoding]
                self.assertNotEqual(subprocess.run(check, cwd=root, capture_output=True, timeout=15).returncode, 0)
                proposal = box.t_propose_edit("settings.json", old, new, encoding=explicit)
                self.assertEqual(path.read_bytes(), before, "proposing cannot repair without approval")
                self.assertEqual(box.t_apply_proposal(proposal["proposal_id"])["status"], "APPLIED")
                self.assertEqual(path.read_bytes(), content.replace(old, new).encode(encoding))
                after = subprocess.run(check, cwd=root, capture_output=True, timeout=15)
                self.assertEqual(after.returncode, 0, after.stderr)
                self.assertEqual(json.loads(path.read_text(encoding=encoding))["label"], "日本語")
