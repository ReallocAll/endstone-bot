import configparser
import zipfile
from pathlib import Path
import unittest


class WheelTests(unittest.TestCase):
    def test_release_entry_point_and_payload(self):
        wheels = list(Path("dist").glob("endstone_bot-4.1.1-*.whl"))
        self.assertEqual(len(wheels), 1)
        with zipfile.ZipFile(wheels[0]) as zf:
            names = set(zf.namelist())
            for path in (
                "endstone_bot/plugin.py",
                "endstone_bot/manager.py",
                "endstone_bot/settings.py",
                "endstone_bot/bridge.py",
                "endstone_bot/gui.py",
                "endstone_bot/behavior_pack/manifest.json",
                "endstone_bot/behavior_pack/scripts/main.js",
            ):
                self.assertIn(path, names)
            ep_name = next(x for x in names if x.endswith(".dist-info/entry_points.txt"))
            cfg = configparser.ConfigParser()
            cfg.read_string(zf.read(ep_name).decode())
            self.assertEqual(cfg["endstone"]["bot"], "endstone_bot:BotPlugin")


if __name__ == "__main__":
    unittest.main()
