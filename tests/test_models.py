import importlib.util
import sys
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("models_under_test", root / "endstone_bot/models.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
FakePlayer = module.FakePlayer
validate_name = module.validate_name


class ModelTests(unittest.TestCase):
    def test_old_record_migrates_to_simulated_model(self):
        fp = FakePlayer.from_record({
            "id": "x",
            "name": "FarmBot",
            "ownerName": "Alice",
            "ownerUuid": "u",
            "location": [1, 64, 2],
            "dimension": "minecraft:nether",
            "type": "entity",
        })
        self.assertEqual(fp.dimension, "nether")
        self.assertEqual(fp.pitch, 0.0)
        self.assertEqual(fp.yaw, 0.0)
        self.assertEqual(fp.to_record()["type"], "simulated")

    def test_rotation_round_trips(self):
        fp = FakePlayer.from_record({
            "id": "r",
            "name": "AimBot",
            "ownerName": "Alice",
            "ownerUuid": "u",
            "location": [1, 64, 2],
            "dimension": "overworld",
            "rotation": [-23.5, 91.25],
        })
        self.assertEqual(fp.pitch, -23.5)
        self.assertEqual(fp.yaw, 91.25)
        self.assertEqual(fp.to_record()["rotation"], [-23.5, 91.25])

    def test_name_validation_blocks_command_injection(self):
        self.assertIsNotNone(validate_name('x";kill @a', set(), set()))
        self.assertIsNone(validate_name("Farm_Bot-1", set(), set()))


if __name__ == "__main__":
    unittest.main()
