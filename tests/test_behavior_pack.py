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

    def test_gametest_structure_is_created_at_runtime(self):
        self.assertIn('world.structureManager.createEmpty(', self.source)
        self.assertIn('StructureSaveMode.World', self.source)
        self.assertIn('structure.saveToWorld()', self.source)
        self.assertIn('world.structureManager.get(structureId)', self.source)
        self.assertIn('startSimulatedPlayerGameTest()', self.source)

    def test_move_restores_previously_verified_world_space_look(self):
        self.assertIn("sim.lookAt({", self.source)
        self.assertIn("head.x + dx * invLength * 32", self.source)
        self.assertIn("head.y + dy * invLength * 32", self.source)
        self.assertIn("head.z + dz * invLength * 32", self.source)
        self.assertNotIn("rotation: poseRotation(req)", self.source)
        self.assertNotIn("sim.lookAtLocation(target", self.source)
        self.assertNotIn("sim.setBodyRotation(", self.source)
        self.assertIn("verifyViewSync(name, sim, pose)", self.source)

    def test_throw_does_not_change_position_or_view(self):
        start = self.source.index("function doThrowTrident(req)")
        end = self.source.index("\nfunction clearAll()", start)
        block = self.source[start:end]
        self.assertNotIn("teleportSim(", block)
        self.assertNotIn("orientSim(", block)
        self.assertNotIn("lookAtEntity(", block)
        self.assertNotIn('spawnEntity("minecraft:armor_stand"', block)

    def test_trident_refuses_other_players_within_one_block(self):
        self.assertIn("playerWithinOneBlock(sim)", self.source)
        self.assertIn("maxDistance: 1.01", self.source)
        self.assertIn("dx * dx + dy * dy + dz * dz <= 1.0", self.source)
        self.assertIn("player_too_close", self.source)
        self.assertIn("if (player === sim) continue", self.source)

    def test_view_direction_is_preserved(self):
        self.assertIn("req.dx", self.source)
        self.assertIn("req.dy", self.source)
        self.assertIn("req.dz", self.source)


if __name__ == "__main__":
    unittest.main()
