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
        self.assertIn("swapTridentIntoHotbarZero", self.source)

    def test_trident_uses_native_simulated_player_item_path(self):
        self.assertIn("sim.useItemInSlot(0)", self.source)
        self.assertIn("sim.stopUsingItem()", self.source)
        self.assertIn("}, 10);", self.source)
        self.assertNotIn('spawnEntity("minecraft:thrown_trident"', self.source)
        self.assertNotIn('spawnEntity("endstone_bot:thrown_trident"', self.source)

    def test_trident_does_not_fabricate_inventory_items(self):
        self.assertNotIn('new ItemStack("minecraft:trident"', self.source)
        self.assertNotIn("sim.setItem(trident", self.source)

    def test_trident_temporarily_uses_hotbar_slot_zero_and_restores_it(self):
        self.assertIn("sim.selectedSlotIndex = 0", self.source)
        self.assertIn("previousSelected", self.source)
        self.assertIn("restoreHotbarAfterTrident", self.source)

    def test_fake_players_are_test_bound_like_working_addons(self):
        self.assertIn("activeTest.spawnSimulatedPlayer", self.source)
        self.assertNotIn("GameTest.spawnSimulatedPlayer", self.source)
        self.assertIn("gametest run endstone_bot:sim_spawner", self.source)

    def test_native_aim_does_not_use_gametest_relative_body_rotation(self):
        self.assertIn("sim.lookAtLocation(target", self.source)
        self.assertNotIn("sim.setBodyRotation(", self.source)

    def test_view_direction_is_preserved(self):
        self.assertIn("req.dx", self.source)
        self.assertIn("req.dy", self.source)
        self.assertIn("req.dz", self.source)


if __name__ == "__main__":
    unittest.main()
