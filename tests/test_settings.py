import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("settings_under_test", root / "endstone_bot/settings.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
SettingsManager = module.SettingsManager


class Logger:
    def warning(self, *_):
        pass


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SettingsManager(Path(self.tmp.name), Logger())

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_are_conservative(self):
        self.assertEqual(self.settings.max_total, 6)
        self.assertEqual(self.settings.max_per_player, 1)
        self.assertEqual(self.settings.spawn_cooldown_seconds, 10)

    def test_unlimited_override(self):
        key, name = self.settings.resolve_target_key("RedstonePlayer", [])
        self.settings.set_unlimited(key, name)
        effective = self.settings.effective_for_key(key)
        self.assertTrue(effective.unlimited)
        self.assertEqual(effective.cooldown_seconds, 0)
        self.assertTrue(effective.bypass_global_limit)

    def test_name_override_migrates_to_uuid(self):
        key, name = self.settings.resolve_target_key("Alice", [])
        self.settings.set_override(key, name=name, max_bots=4)
        uuid_key = self.settings.remember_player("uuid-1", "Alice")
        self.assertEqual(uuid_key, "uuid:uuid-1")
        self.assertEqual(self.settings.effective_for_key(uuid_key).max_bots, 4)
        self.assertNotIn("name:alice", dict(self.settings.known_players()))

    def test_reset_uses_new_global_defaults(self):
        key, name = self.settings.resolve_target_key("Bob", [])
        self.settings.set_unlimited(key, name)
        self.settings.set_global("maxperplayer", 3)
        self.settings.reset_override(key, name)
        effective = self.settings.effective_for_key(key)
        self.assertEqual(effective.max_bots, 3)
        self.assertFalse(effective.bypass_global_limit)


if __name__ == "__main__":
    unittest.main()
