import importlib.util
import os
import struct
import subprocess
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
beta_script = load_module("endstone_bot.beta_script", "endstone_bot/beta_script.py")


def root_level_dat(body: bytes, version: int = 10) -> bytes:
    payload = bytes([nbt.TAG_COMPOUND]) + struct.pack("<H", 0) + body + bytes([nbt.TAG_END])
    return struct.pack("<II", version, len(payload)) + payload


class BetaScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.world = self.root / "worlds" / "level"
        self.world.mkdir(parents=True)
        self.level = self.world / "level.dat"
        self.level.write_bytes(root_level_dat(nbt.make_byte_tag_bytes("Difficulty", 2)))
        self.data = self.root / "plugins" / "bot"

    def tearDown(self):
        self.tmp.cleanup()

    def test_generated_script_uses_exact_world_path_and_no_plugin_imports(self):
        script = beta_script.write_patch_script(self.data, self.world)

        self.assertEqual(script, self.data / "enable_beta.py")
        source = script.read_text(encoding="utf-8")
        self.assertIn(repr(str(self.level.resolve())), source)
        self.assertIn("BDS must be fully stopped", source)
        self.assertNotIn("from endstone_bot", source)
        self.assertNotIn("import endstone_bot", source)
        self.assertNotIn("atexit", source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("__LEVEL_DAT_LITERAL__", source)
        self.assertNotIn("__WORLD_NAME_LITERAL__", source)

    def test_generated_script_runs_in_isolated_python_without_package(self):
        script = beta_script.write_patch_script(self.data, self.world)
        self.assertFalse(level_dat.is_beta_apis_enabled(self.level))

        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, "-I", str(script)],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )

        self.assertEqual(result.returncode, 0, msg=result.stdout + "\n" + result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)
        self.assertTrue(level_dat.is_beta_apis_enabled(self.level))
        backups = list((self.data / "level_dat_backups" / "level").glob("level.dat.*.bak"))
        self.assertEqual(len(backups), 1)

    def test_generation_alone_never_modifies_level_dat(self):
        before = self.level.read_bytes()
        beta_script.write_patch_script(self.data, self.world)
        self.assertEqual(self.level.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
