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

    def test_view_direction_syncs_all_controller_rotations(self):
        self.assertIn("sim.lookAtLocation(target", self.source)
        self.assertIn("LookDuration?.Instant", self.source)
        self.assertIn("sim.setBodyRotation(rotation.y)", self.source)
        self.assertIn("sim.setRotation(rotation)", self.source)
        self.assertIn("Math.atan2(-nx, nz)", self.source)
        self.assertIn("-Math.asin", self.source)
        self.assertIn("req.dx", self.source)
        self.assertIn("req.dy", self.source)
        self.assertIn("req.dz", self.source)

    def test_trident_use_waits_one_tick_after_controller_rotation(self):
        self.assertIn("Do not start item use in the same tick", self.source)
        self.assertIn("system.runTimeout(() => {", self.source)


if __name__ == "__main__":
    unittest.main()
