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

    def test_gametest_restores_changed_gamerules(self):
        self.assertIn("function snapshotGameRules()", self.source)
        self.assertIn("function restoreGameRulesAfterGameTest(snapshot)", self.source)
        self.assertIn("for (const rule in world.gameRules)", self.source)
        self.assertIn("world.gameRules[rule] = wanted", self.source)
        self.assertIn("}, 2);", self.source)
        self.assertIn("GameTest changed gamerules; restored:", self.source)

        start = self.source.index("function startSimulatedPlayerGameTest()")
        end = self.source.index("\ntry {\n    if (typeof GameTest.register", start)
        block = self.source[start:end]
        snapshot = block.index("const gameRulesBeforeGameTest = snapshotGameRules();")
        run = block.index("gametest run endstone_bot:sim_spawner")
        restore = block.index("restoreGameRulesAfterGameTest(gameRulesBeforeGameTest);")
        self.assertLess(snapshot, run)
        self.assertLess(run, restore)

    def test_gametest_rule_restore_uses_snapshot_not_hardcoded_defaults(self):
        start = self.source.index("function restoreGameRulesAfterGameTest(snapshot)")
        end = self.source.index("\nconst simulatedPlayers", start)
        block = self.source[start:end]
        self.assertNotIn("doDayLightCycle", block)
        self.assertNotIn("doMobSpawning", block)
        self.assertNotIn("randomTickSpeed", block)
        self.assertIn("Object.entries(snapshot)", block)

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

    def test_routine_bridge_handshake_is_quiet(self):
        self.assertNotIn("hello received:", self.source)
        self.assertNotIn("hello_ack dispatched", self.source)
        self.assertNotIn("bridge loaded, protocol=", self.source)
        self.assertIn("callback failed", self.source)
        self.assertIn("rejected malformed hello payload", self.source)


if __name__ == "__main__":
    unittest.main()
