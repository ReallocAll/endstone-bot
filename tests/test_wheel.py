import configparser
import zipfile
import json
import re
from pathlib import Path
import unittest


class WheelTests(unittest.TestCase):
    def test_release_entry_point_and_payload(self):
        version = re.search(r'(?m)^version = "([^"]+)"', Path("pyproject.toml").read_text())[1]
        wheels = list(Path("dist").glob(f"endstone_bot-{version}-*.whl"))
        self.assertEqual(len(wheels), 1)
        with zipfile.ZipFile(wheels[0]) as zf:
            names = set(zf.namelist())
            for path in (
                "endstone_bot/plugin.py",
                "endstone_bot/manager.py",
                "endstone_bot/inventory_journal.py",
                "endstone_bot/settings.py",
                "endstone_bot/bridge.py",
                "endstone_bot/gui.py",
                "endstone_bot/beta_script.py",
                "endstone_bot/enable_beta_template.py",
                "endstone_bot/level_dat.py",
                "endstone_bot/nbt.py",
                "endstone_bot/behavior_pack/manifest.json",
                "endstone_bot/behavior_pack/scripts/main.js",
            ):
                self.assertIn(path, names)
            manifest = json.loads(zf.read("endstone_bot/behavior_pack/manifest.json"))
            self.assertEqual(manifest["header"]["version"], [4, 3, 3])
            self.assertEqual(manifest["modules"][0]["version"], [4, 3, 3])
            for member in ("endstone_bot/behavior_pack/scripts/main.js", "endstone_bot/behavior_pack/manifest.json",
                           "endstone_bot/bridge.py", "endstone_bot/plugin.py"):
                self.assertEqual(zf.read(member), Path(member).read_bytes(), member)
            self.assertIn(b"BRIDGE_PROTOCOL = 3", zf.read("endstone_bot/bridge.py"))
            self.assertIn(b"const PROTOCOL = 3", zf.read("endstone_bot/behavior_pack/scripts/main.js"))
            self.assertIn(f'__version__ = "{version}"'.encode(), zf.read("endstone_bot/__init__.py"))
            ep_name = next(x for x in names if x.endswith(".dist-info/entry_points.txt"))
            cfg = configparser.ConfigParser()
            cfg.read_string(zf.read(ep_name).decode())
            self.assertEqual(cfg["endstone"]["bot"], "endstone_bot:BotPlugin")
            self.assertFalse(
                cfg.has_section("console_scripts")
                and "endstone-bot-enable-beta" in cfg["console_scripts"]
            )
            self.assertFalse(
                any(name.endswith(".mcstructure") for name in names),
                "static mcstructure files must not be shipped",
            )


if __name__ == "__main__":
    unittest.main()
