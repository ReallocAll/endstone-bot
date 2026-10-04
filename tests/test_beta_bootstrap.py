import importlib.util
import json
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
level_dat = load_module("endstone_bot.level_dat", "endstone_bot/level_dat.py")
bootstrap = load_module("endstone_bot.beta_bootstrap", "endstone_bot/beta_bootstrap.py")


def root_level_dat(body: bytes, version: int = 10) -> bytes:
    payload = bytes([nbt.TAG_COMPOUND]) + struct.pack("<H", 0) + body + bytes([nbt.TAG_END])
    return struct.pack("<II", version, len(payload)) + payload


class BetaBootstrapTests(unittest.TestCase):
    def setUp(self):
        bootstrap.cancel_exit_patch()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.level = self.root / "worlds" / "level" / "level.dat"
        self.level.parent.mkdir(parents=True)
        self.original = root_level_dat(nbt.make_byte_tag_bytes("Difficulty", 2))
        self.level.write_bytes(self.original)

    def tearDown(self):
        bootstrap.cancel_exit_patch()
        self.tmp.cleanup()

    def test_schedule_does_not_modify_running_world(self):
        bootstrap.schedule_exit_patch(
            self.level,
            backup_root=self.root / "backups",
            world_name="level",
            backup_keep=5,
            status_path=self.root / "status.json",
        )

        self.assertEqual(self.level.read_bytes(), self.original)
        self.assertIsNotNone(bootstrap.pending_exit_patch())

    def test_deferred_patch_runs_and_records_status(self):
        status = self.root / "status.json"
        bootstrap.schedule_exit_patch(
            self.level,
            backup_root=self.root / "backups",
            world_name="level",
            backup_keep=5,
            status_path=status,
        )

        result = bootstrap.run_pending_exit_patch()

        self.assertIsNotNone(result)
        self.assertTrue(result.ok)
        self.assertTrue(result.changed)
        self.assertTrue(level_dat.is_beta_apis_enabled(self.level))
        self.assertIsNone(bootstrap.pending_exit_patch())

        data = json.loads(status.read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "patched")
        self.assertTrue(data["changed"])

    def test_cancel_prevents_exit_patch(self):
        bootstrap.schedule_exit_patch(
            self.level,
            backup_root=self.root / "backups",
            world_name="level",
            backup_keep=5,
            status_path=self.root / "status.json",
        )
        bootstrap.cancel_exit_patch()

        self.assertIsNone(bootstrap.run_pending_exit_patch())
        self.assertEqual(self.level.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
