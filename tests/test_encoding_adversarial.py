"""Hostile encoding fixtures: preserve bytes or reject before an actionable proposal."""
import codecs
from pathlib import Path
import tempfile
import unittest
import sys
from code_attach.tools import ToolBox

FORMATS = {
    "utf8": ("utf-8", b"", None),
    "utf8_bom": ("utf-8", codecs.BOM_UTF8, None),
    "utf16_le_bom": ("utf-16-le", codecs.BOM_UTF16_LE, None),
    "utf16_be_bom": ("utf-16-be", codecs.BOM_UTF16_BE, None),
    "utf32_le_bom": ("utf-32-le", codecs.BOM_UTF32_LE, None),
    "utf32_be_bom": ("utf-32-be", codecs.BOM_UTF32_BE, None),
    "cp932_explicit": ("cp932", b"", "cp932"),
    "shift_jis_explicit": ("shift_jis", b"", "shift_jis"),
    "utf16_no_bom_explicit": ("utf-16-le", b"", "utf-16-le"),
}


class EncodingAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tools = ToolBox(self.root)

    def rejected_without_change(self, raw, encoding=None):
        path = self.root / "source.txt"
        path.write_bytes(raw)
        with self.assertRaises((ValueError, UnicodeError)):
            self.tools.t_propose_edit("source.txt", "value = 1", "value = 2", encoding=encoding)
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse(self.tools.proposals)

    def test_cp932_is_not_guessed_or_decoded_with_replacement(self):
        raw = "# 日本語\nvalue = 1\n".encode("cp932")
        self.rejected_without_change(raw)
        result = self.tools.t_search_text("value", glob="source.txt")
        self.assertFalse(result["matches"])
        self.assertEqual(result["skipped_encoding_files"], 1)
        self.assertEqual(self.tools.t_read_file("source.txt", encoding="cp932")["encoding"], "cp932")

    def test_invalid_utf8_truncation_and_overlong_sequences(self):
        for raw in (b"value = 1\n\xe3\x81", b"value = 1\n\xc0\xaf", b"value = 1\n\xed\xa0\x80", b"value = 1\n\xff"):
            with self.subTest(raw=raw):
                self.rejected_without_change(raw)

    def test_mixed_encoding_is_not_silently_converted(self):
        raw = "# 日本語 UTF8\n".encode() + "# 日本語 CP932\nvalue = 1\n".encode("cp932")
        self.rejected_without_change(raw)

    def test_binary_nul_is_refused_even_if_ascii_matches(self):
        self.rejected_without_change(b"value = 1\x00binary payload\n")

    def test_truncated_utf16_and_utf32(self):
        self.rejected_without_change(codecs.BOM_UTF16_LE + b"v")
        self.rejected_without_change(codecs.BOM_UTF32_BE + b"\x00\x00v")

    def test_explicit_encoding_cannot_override_conflicting_bom(self):
        self.rejected_without_change(codecs.BOM_UTF16_LE + "value = 1\n".encode("utf-16-le"), "cp932")

    def test_unrepresentable_replacement_is_refused_before_approval(self):
        raw = "# 日本語\nvalue = 1\n".encode("cp932")
        (self.root / "source.txt").write_bytes(raw)
        with self.assertRaises(UnicodeEncodeError):
            self.tools.t_propose_edit("source.txt", "value = 1", "value = '😀'", encoding="cp932")
        self.assertEqual((self.root / "source.txt").read_bytes(), raw)
        self.assertFalse(self.tools.proposals)

    def test_lossy_legacy_alias_bytes_are_rejected(self):
        # CP932 has duplicate byte representations. Refuse normalization of an
        # original representation that Python would re-encode differently.
        self.rejected_without_change(b"#\xfa\x5c\nvalue = 1\n", "cp932")

    def test_unknown_or_catchall_codec_is_rejected(self):
        for codec in ("latin1", "utf-7", "not-a-codec"):
            with self.subTest(codec=codec):
                self.rejected_without_change(b"value = 1\n", codec)
                with self.assertRaises(ValueError):
                    self.tools.t_search_text("value", encoding=codec)

    def test_encoding_change_after_proposal_is_conflict(self):
        text = "# 日本語\nvalue = 1\n"
        path = self.root / "source.txt"
        path.write_bytes(text.encode())
        p = self.tools.t_propose_edit("source.txt", "value = 1", "value = 2")
        external = text.encode("cp932")
        path.write_bytes(external)
        self.assertEqual(self.tools.t_apply_proposal(p["proposal_id"])["status"], "CONFLICT")
        self.assertEqual(path.read_bytes(), external)

    def test_new_file_explicit_legacy_and_bom_formats(self):
        for name, (codec, bom, explicit) in FORMATS.items():
            with self.subTest(name=name):
                chosen = "utf-8-sig" if name == "utf8_bom" else codec
                text = "# 日本語\r\nvalue = 1\r\n"
                p = self.tools.t_propose_new_file(name + ".txt", text, encoding=chosen)
                self.assertEqual(self.tools.t_apply_proposal(p["proposal_id"])["status"], "APPLIED")
                raw = (self.root / (name + ".txt")).read_bytes()
                info = self.tools.t_read_file(name + ".txt", encoding=explicit or None)
                self.assertIn("value = 1", info["lines"])
                self.assertEqual(raw[-len("value = 1\r\n".encode(codec)):], "value = 1\r\n".encode(codec))

    def test_cp932_python_with_cookie_really_executes_after_edit(self):
        raw = "# coding: cp932\r\n# 日本語\r\nvalue = 1\r\nassert value == 2\r\n".encode("cp932")
        path = self.root / "legacy.py"
        path.write_bytes(raw)
        tools = ToolBox(self.root, {"regression": [sys.executable, "legacy.py"]})
        self.assertEqual(tools.t_run_tests("regression")["status"], "FAIL")
        p = tools.t_propose_edit("legacy.py", "value = 1", "value = 2", encoding="cp932")
        self.assertEqual(tools.t_apply_proposal(p["proposal_id"])["status"], "APPLIED")
        self.assertEqual(tools.t_run_tests("regression")["status"], "PASS")
        self.assertEqual(path.read_bytes(), raw.replace(b"value = 1", b"value = 2"))


def format_test(codec, bom, explicit):
    def test(self):
        before = "# 日本語・残す部分\r\nvalue = 1\r\n# 末尾"
        raw = bom + before.encode(codec)
        path = self.root / "source.txt"
        path.write_bytes(raw)
        read = self.tools.t_read_file("source.txt", encoding=explicit)
        self.assertEqual(read["encoding"], codec)
        self.assertEqual(read["bom"], bool(bom))
        self.assertEqual(self.tools.t_search_text("value", glob="source.txt", encoding=explicit)["matches"][0]["line"], 2)
        p = self.tools.t_propose_edit("source.txt", "value = 1", "value = 2", encoding=explicit)
        self.assertEqual(path.read_bytes(), raw)
        summary, _ = self.tools.describe_effect("apply_proposal", {"proposal_id": p["proposal_id"]})
        self.assertEqual(summary["encoding"], codec)
        self.assertEqual(self.tools.t_apply_proposal(p["proposal_id"])["status"], "APPLIED")
        self.assertEqual(path.read_bytes(), bom + before.replace("value = 1", "value = 2").encode(codec))
    return test


for _name, _format in FORMATS.items():
    setattr(EncodingAdversarialTests, "test_roundtrip_" + _name, format_test(*_format))
