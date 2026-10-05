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

    def test_spawn_helper_does_not_hide_initial_teleport_failure(self):
        start = self.source.index("function spawnWithTestApi(req)")
        end = self.source.index("\nfunction isDeadSim(", start)
        block = self.source[start:end]
        self.assertNotIn("teleportSim(", block)

    def test_new_spawn_disconnects_if_initial_teleport_fails(self):
        start = self.source.index("function doSpawn(req)")
        end = self.source.index("\nfunction finishRemove(", start)
        block = self.source[start:end]
        self.assertIn("initial SimulatedPlayer teleport failed", block)
        self.assertIn("try { sim.disconnect(); } catch (_) {}", block)
        self.assertIn("desiredPoses.delete(name)", block)
        self.assertIn("existing SimulatedPlayer teleport failed", block)

    def test_gametest_structure_is_created_at_runtime(self):
        self.assertIn('world.structureManager.createEmpty(', self.source)
        self.assertIn('StructureSaveMode.World', self.source)
        self.assertIn('structure.saveToWorld()', self.source)
        self.assertIn('world.structureManager.get(structureId)', self.source)
        self.assertIn('startSimulatedPlayerGameTest()', self.source)

    def test_move_uses_gametest_relative_controller_view_until_move(self):
        self.assertIn("sim.lookAtLocation(", self.source)
        self.assertIn('GameTest.LookDuration?.UntilMove ?? "UntilMove"', self.source)
        self.assertIn("const relativeTarget = activeTest.relativeLocation(target)", self.source)
        self.assertIn("head.x + dx * invLength * 32", self.source)
        self.assertIn("head.y + dy * invLength * 32", self.source)
        self.assertIn("head.z + dz * invLength * 32", self.source)
        self.assertNotIn("function savedPitch(req)", self.source)
        self.assertNotIn("function convergeSavedPitch(sim, req", self.source)
        self.assertNotIn("const compensatedPitch =", self.source)
        self.assertNotIn("rotation: poseRotation(req)", self.source)
        self.assertNotIn("sim.setBodyRotation(", self.source)
        self.assertIn("verifyViewSync(name, sim, pose)", self.source)
        self.assertIn("sim.headRotation.x", self.source)
        self.assertIn("sim.getViewDirection()", self.source)

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

    def test_inventory_edit_uses_physical_36_slot_swaps_with_rollback(self):
        self.assertIn("const MAIN_INVENTORY_SLOTS = 36", self.source)
        self.assertIn("first.swapItems(slot, slot, second)", self.source)
        self.assertIn("for (let index = completed.length - 1; index >= 0; index--)", self.source)
        self.assertIn("inventory swap failed and rollback failed", self.source)

    def test_inventory_edit_locks_both_participants_until_dual_backup_ready(self):
        self.assertIn('gameMode = lockInventoryBot(sim)', self.source)
        self.assertIn('playerGameMode = lockInventoryBot(player)', self.source)
        self.assertIn("function doInventoryReady(req)", self.source)
        self.assertIn("if (!lease.ready && !rollbackRequest)", self.source)
        self.assertIn("unlockInventoryBot(player, lease.playerGameMode)", self.source)
        self.assertIn("inventoryLeases", self.source)
        self.assertIn("inventory_custody", self.source)

    def test_inventory_recovery_refuses_non_empty_replacement_bot(self):
        start = self.source.index("function doInventoryRecover(req)")
        end = self.source.index("\nfunction doInventoryRecoveryFinalize(", start)
        block = self.source[start:end]
        self.assertIn("if (!mainInventoryEmpty(botInventory))", block)
        self.assertIn("recovery_bot_not_empty", block)
        self.assertIn("swapMainInventories(playerInventory, botInventory)", block)

    def test_inventory_finish_makes_players_selected_hotbar_slot_the_bot_main_hand(self):
        start = self.source.index("function doInventoryFinish(req)")
        end = self.source.index("\nfunction doInventoryRecover(", start)
        block = self.source[start:end]
        self.assertIn("const editedSelected =", block)
        self.assertIn("sim.selectedSlotIndex = editedSelected", block)
        self.assertIn("player.selectedSlotIndex = lease.playerSelected", block)

    def test_inventory_bridge_protocol_is_v3(self):
        self.assertIn("const PROTOCOL = 3;", self.source)
        for event in (
            "bot:inventory_begin",
            "bot:inventory_ready",
            "bot:inventory_finish",
            "bot:inventory_recover",
            "bot:inventory_recovery_finalize",
        ):
            self.assertIn(event, self.source)

    def test_routine_bridge_handshake_is_quiet(self):
        self.assertNotIn("hello received:", self.source)
        self.assertNotIn("hello_ack dispatched", self.source)
        self.assertNotIn("bridge loaded, protocol=", self.source)
        self.assertIn("callback failed", self.source)
        self.assertIn("rejected malformed hello payload", self.source)


if __name__ == "__main__":
    unittest.main()
