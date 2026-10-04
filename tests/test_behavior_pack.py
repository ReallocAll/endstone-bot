import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "endstone_bot" / "behavior_pack" / "scripts" / "main.js"


class BehaviorPackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_trident_is_taken_from_inventory(self):
        self.assertIn('getComponent("minecraft:inventory")', self.source)
        self.assertIn('item.typeId === "minecraft:trident"', self.source)
        self.assertIn("useItemInSlot(slot)", self.source)

    def test_trident_is_never_created_by_script(self):
        self.assertNotIn('new ItemStack("minecraft:trident"', self.source)
        self.assertNotIn("sim.setItem(trident", self.source)
        self.assertNotIn("ItemStack,", self.source)

    def test_view_direction_uses_simulated_player_controller(self):
        self.assertIn("sim.lookAtLocation(target", self.source)
        self.assertIn("LookDuration?.Instant", self.source)
        self.assertIn("req.dx", self.source)
        self.assertIn("req.dy", self.source)
        self.assertIn("req.dz", self.source)


if __name__ == "__main__":
    unittest.main()
