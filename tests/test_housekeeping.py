import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from code_attach.tools import ToolBox


class HousekeepingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.backups = self.base / "recycle"
        self.tools = ToolBox(self.root, recycle_dir=self.backups)
        self.raw = bytes(range(256)) * 200
        (self.root / "data.bin").write_bytes(self.raw)

    def apply(self, proposal):
        result = self.tools.t_apply_proposal(proposal["proposal_id"])
        self.assertEqual(result["status"], "APPLIED", result)
        return result

    def test_new_file_never_overwrites(self):
        p = self.tools.t_propose_new_file("新規/空白 file.txt", "日本語\r\n")
        self.assertFalse((self.root / "新規").exists())
        self.apply(p)
        self.assertEqual((self.root / "新規/空白 file.txt").read_bytes(), "日本語\r\n".encode())
        with self.assertRaises(ValueError):
            self.tools.t_propose_new_file("data.bin", "overwrite")

    def test_copy_binary_preserves_original_and_all_bytes(self):
        p = self.tools.t_propose_copy_file("data.bin", "控え/複製.bin")
        self.assertFalse((self.root / "控え").exists())
        self.apply(p)
        self.assertEqual((self.root / "data.bin").read_bytes(), self.raw)
        self.assertEqual((self.root / "控え/複製.bin").read_bytes(), self.raw)

    def test_move_and_rename_binary(self):
        self.apply(self.tools.t_propose_move_file("data.bin", "移動先/renamed.bin"))
        self.assertFalse((self.root / "data.bin").exists())
        self.assertEqual((self.root / "移動先/renamed.bin").read_bytes(), self.raw)

    def test_delete_backup_survives_new_toolbox_and_restores(self):
        p = self.tools.t_propose_delete_file("data.bin")
        self.assertTrue((self.root / "data.bin").exists())
        self.assertFalse(self.backups.exists())
        result = self.apply(p)
        self.assertFalse((self.root / "data.bin").exists())
        self.tools = ToolBox(self.root, recycle_dir=self.backups)
        self.assertEqual(self.tools.t_list_file_backups()["backups"][0]["backup_id"], result["backup_id"])
        self.apply(self.tools.t_propose_restore_file(result["backup_id"]))
        self.assertEqual((self.root / "data.bin").read_bytes(), self.raw)

    def test_restore_never_clobbers_recreated_source(self):
        result = self.apply(self.tools.t_propose_delete_file("data.bin"))
        (self.root / "data.bin").write_bytes(b"human")
        with self.assertRaises(FileExistsError):
            self.tools.t_propose_restore_file(result["backup_id"])
        self.apply(self.tools.t_propose_restore_file(result["backup_id"], "recovered.bin"))
        self.assertEqual((self.root / "data.bin").read_bytes(), b"human")
        self.assertEqual((self.root / "recovered.bin").read_bytes(), self.raw)

    def test_nested_create_and_empty_remove(self):
        self.apply(self.tools.t_propose_directory("nested/empty", "create"))
        self.assertTrue((self.root / "nested/empty").is_dir())
        self.apply(self.tools.t_propose_directory("nested/empty", "remove_empty"))
        self.assertFalse((self.root / "nested/empty").exists())

    def test_nonempty_directory_is_never_removed(self):
        with self.assertRaises(ValueError):
            self.tools.t_propose_directory(".", "remove_empty")
        (self.root / "folder").mkdir()
        p = self.tools.t_propose_directory("folder", "remove_empty")
        (self.root / "folder/human.txt").write_text("human")
        self.assertEqual(self.tools.t_apply_proposal(p["proposal_id"])["status"], "CONFLICT")
        self.assertTrue((self.root / "folder/human.txt").exists())

    def test_stale_sources_for_move_copy_delete(self):
        for operation in ("move", "copy", "delete"):
            with self.subTest(operation=operation):
                (self.root / "data.bin").write_bytes(self.raw)
                fn = getattr(self.tools, "t_propose_" + operation + "_file")
                p = fn("data.bin") if operation == "delete" else fn("data.bin", "new.bin")
                (self.root / "data.bin").write_bytes(b"human")
                self.assertEqual(self.tools.t_apply_proposal(p["proposal_id"])["status"], "CONFLICT")
                self.assertEqual((self.root / "data.bin").read_bytes(), b"human")
                self.assertFalse((self.root / "new.bin").exists())

    def test_destination_created_during_approval_is_preserved(self):
        for op in ("move", "copy"):
            with self.subTest(operation=op):
                destination = op + ".bin"
                p = getattr(self.tools, "t_propose_" + op + "_file")("data.bin", destination)
                (self.root / destination).write_bytes(b"human")
                self.assertEqual(self.tools.t_apply_proposal(p["proposal_id"])["status"], "CONFLICT")
                self.assertEqual((self.root / destination).read_bytes(), b"human")
                self.assertEqual((self.root / "data.bin").read_bytes(), self.raw)

    def test_backup_failure_never_deletes_source(self):
        p = self.tools.t_propose_delete_file("data.bin")
        with patch.object(self.tools.files, "_backup", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.tools.t_apply_proposal(p["proposal_id"])
        self.assertEqual((self.root / "data.bin").read_bytes(), self.raw)

    def test_invalid_paths_and_backup_ids_are_rejected(self):
        for name in ("..", "../outside", ".git/config", ".env", ".", "NUL.txt", "a.txt:stream", "trailing."):
            with self.subTest(path=name), self.assertRaises((ValueError, OSError)):
                self.tools.t_propose_directory(name, "create")
        for bid in ("../bad", "", "not-a-uuid"):
            with self.assertRaises(ValueError):
                self.tools.t_propose_restore_file(bid)

    def test_reusing_proposal_is_rejected(self):
        p = self.tools.t_propose_copy_file("data.bin", "copy.bin")
        self.apply(p)
        with self.assertRaises(ValueError):
            self.tools.t_apply_proposal(p["proposal_id"])
