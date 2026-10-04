import hashlib
import importlib.util
import json
import os
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

package = types.ModuleType("endstone_bot")
package.__path__ = [str(ROOT / "endstone_bot")]
sys.modules.setdefault("endstone_bot", package)


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


nbt = load_module("endstone_bot.nbt", "endstone_bot/nbt.py")
level_dat = load_module("level_dat_under_test", "endstone_bot/level_dat.py")


def root_level_dat(body: bytes, version: int = 10) -> bytes:
    payload = bytes([nbt.TAG_COMPOUND]) + struct.pack("<H", 0) + body + bytes([nbt.TAG_END])
    return struct.pack("<II", version, len(payload)) + payload


def int_tag(name: str, value: int) -> bytes:
    encoded = name.encode("utf-8")
    return bytes([nbt.TAG_INT]) + struct.pack("<H", len(encoded)) + encoded + struct.pack("<i", value)


class LevelDatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backups = self.root / "backups"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_level(self, raw: bytes, name: str = "world") -> Path:
        path = self.root / name / "level.dat"
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)
        return path

    def test_patch_is_minimal_semantically_and_backed_up(self):
        original = root_level_dat(nbt.make_byte_tag_bytes("Difficulty", 2))
        path = self._write_level(original)

        result = level_dat.enable_beta_apis_safely(
            path,
            backup_root=self.backups,
            world_name="world",
            backup_keep=5,
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.changed)
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(result.backup_path.read_bytes(), original)
        self.assertTrue(level_dat.is_beta_apis_enabled(path))

        before = nbt.read_bedrock_nbt(original, has_header=True)
        after = nbt.read_bedrock_nbt(path.read_bytes(), has_header=True)
        self.assertEqual(before["Difficulty"], after["Difficulty"])
        self.assertEqual(after["experiments"]["gametest"], 1)
        self.assertEqual(after["experiments"]["experiments_ever_used"], 1)
        self.assertEqual(after["experiments"]["saved_with_toggled_experiments"], 1)

    def test_already_enabled_does_not_create_backup(self):
        exp_body = b"".join(
            nbt.make_byte_tag_bytes(k, v)
            for k, v in level_dat.EXPERIMENTS_FIELDS.items()
        )
        original = root_level_dat(nbt.make_compound_tag_bytes("experiments", exp_body))
        path = self._write_level(original)

        result = level_dat.enable_beta_apis_safely(
            path,
            backup_root=self.backups,
            world_name="world",
        )

        self.assertEqual(result.status, "already-enabled")
        self.assertFalse(result.changed)
        self.assertFalse(self.backups.exists())

    def test_wrong_field_type_is_fail_closed(self):
        exp_body = int_tag("gametest", 0)
        original = root_level_dat(nbt.make_compound_tag_bytes("experiments", exp_body))
        path = self._write_level(original)

        result = level_dat.enable_beta_apis_safely(
            path,
            backup_root=self.backups,
            world_name="world",
        )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(self.backups.exists())

    def test_bad_header_length_is_fail_closed(self):
        original = bytearray(root_level_dat(nbt.make_byte_tag_bytes("Difficulty", 2)))
        struct.pack_into("<I", original, 4, 1)
        path = self._write_level(bytes(original))

        result = level_dat.enable_beta_apis_safely(
            path,
            backup_root=self.backups,
            world_name="world",
        )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(path.read_bytes(), bytes(original))

    @unittest.skipIf(os.name == "nt", "symlink privileges vary on Windows CI")
    def test_symlink_level_dat_is_rejected(self):
        original = root_level_dat(nbt.make_byte_tag_bytes("Difficulty", 2))
        real = self._write_level(original, "real")
        link_dir = self.root / "linked"
        link_dir.mkdir()
        link = link_dir / "level.dat"
        link.symlink_to(real)

        result = level_dat.enable_beta_apis_safely(
            link,
            backup_root=self.backups,
            world_name="linked",
        )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(real.read_bytes(), original)

    def test_post_write_verification_failure_rolls_back_original(self):
        original = root_level_dat(nbt.make_byte_tag_bytes("Difficulty", 2))
        path = self._write_level(original)

        real_checker = level_dat.is_beta_apis_enabled
        level_dat.is_beta_apis_enabled = lambda _: False
        try:
            result = level_dat.enable_beta_apis_safely(
                path,
                backup_root=self.backups,
                world_name="world",
            )
        finally:
            level_dat.is_beta_apis_enabled = real_checker

        self.assertEqual(result.status, "rolled-back")
        self.assertFalse(result.changed)
        self.assertEqual(path.read_bytes(), original)
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(result.backup_path.read_bytes(), original)

    def test_resolver_uses_only_server_properties_level_name(self):
        server_root = self.root / "server"
        world = server_root / "worlds" / "chosen"
        world.mkdir(parents=True)
        (world / "level.dat").write_bytes(root_level_dat(b""))
        (server_root / "worlds" / "other").mkdir()
        (server_root / "worlds" / "other" / "level.dat").write_bytes(root_level_dat(b""))
        (server_root / "server.properties").write_text("level-name=chosen\n", encoding="utf-8")

        resolution = level_dat.resolve_level_dat_for_startup(server_root)

        self.assertEqual(resolution.level_name, "chosen")
        self.assertEqual(resolution.world_dir, world.resolve())
        self.assertEqual(resolution.level_dat, world.resolve() / "level.dat")

    def test_resolver_rejects_path_traversal(self):
        server_root = self.root / "server"
        server_root.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "level.dat").write_bytes(root_level_dat(b""))
        (server_root / "server.properties").write_text("level-name=../../outside\n", encoding="utf-8")

        resolution = level_dat.resolve_level_dat_for_startup(server_root)

        self.assertIsNone(resolution.level_dat)
        self.assertIn("越出 worlds", resolution.error)


if __name__ == "__main__":
    unittest.main()
