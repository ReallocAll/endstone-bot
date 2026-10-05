import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "endstone_bot" / "behavior_pack" / "scripts" / "main.js"
MANIFEST = ROOT / "endstone_bot" / "behavior_pack" / "manifest.json"
PLUGIN = ROOT / "endstone_bot" / "plugin.py"


class BehaviorPackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SCRIPT.read_text(encoding="utf-8")
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.plugin_source = PLUGIN.read_text(encoding="utf-8")

    def test_behavior_pack_version_matches_plugin_deployer(self):
        header_version = self.manifest["header"]["version"]
        module_version = self.manifest["modules"][0]["version"]
        self.assertEqual(header_version, module_version)
        match = re.search(r"BEHAVIOR_PACK_VERSION = \[(\d+), (\d+), (\d+)\]", self.plugin_source)
        self.assertIsNotNone(match)
        self.assertEqual(header_version, [int(x) for x in match.groups()])

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

    def test_fake_players_use_standalone_api_only(self):
        self.assertIn("GameTest.spawnSimulatedPlayer(", self.source)
        self.assertIn("survivalGameMode", self.source)
        self.assertIn("dimension: getDimension(req.d)", self.source)
        self.assertIn("standaloneSimulatedPlayerSupported", self.source)

        for forbidden in (
            "GameTest.register",
            "activeTest",
            "startSimulatedPlayerGameTest",
            "ensureGameTestStructure",
            "structureManager.createEmpty",
            "StructureSaveMode",
            "gametest run endstone_bot:sim_spawner",
            "snapshotGameRules",
            "restoreGameRulesAfterGameTest",
        ):
            self.assertNotIn(forbidden, self.source)

    def test_standalone_spawn_helper_uses_dimension_location(self):
        start = self.source.index("function spawnStandaloneSimulatedPlayer(req)")
        end = self.source.index("\nfunction isDeadSim(", start)
        block = self.source[start:end]
        self.assertIn("GameTest.spawnSimulatedPlayer(", block)
        self.assertIn("dimension: getDimension(req.d)", block)
        self.assertIn("x: Number(req.x)", block)
        self.assertIn("y: Number(req.y)", block)
        self.assertIn("z: Number(req.z)", block)
        self.assertIn("survivalGameMode", block)
        self.assertNotIn("teleportSim(", block)

    def test_new_spawn_fail_closes_without_legacy_fallback(self):
        start = self.source.index("function doSpawn(req)")
        end = self.source.index("\nfunction finishRemove(", start)
        block = self.source[start:end]
        self.assertIn("spawnStandaloneSimulatedPlayer(pose)", block)
        self.assertIn("standalone SimulatedPlayer spawn failed", block)
        self.assertIn("try { sim.disconnect(); } catch (_) {}", block)
        self.assertIn("desiredPoses.delete(name)", block)
        self.assertIn("existing SimulatedPlayer teleport failed", block)
        self.assertNotIn("startSimulatedPlayerGameTest", block)

    def test_hello_ack_advertises_standalone_capability(self):
        self.assertIn("standalone: standaloneSimulatedPlayerSupported", self.source)
        self.assertIn("legacy test-bound fallback is intentionally disabled", self.source)

    def test_move_uses_world_coordinates_until_move(self):
        self.assertIn("sim.lookAtLocation(", self.source)
        self.assertIn('GameTest.LookDuration?.UntilMove ?? "UntilMove"', self.source)
        self.assertIn("head.x + dx * invLength * 32", self.source)
        self.assertIn("head.y + dy * invLength * 32", self.source)
        self.assertIn("head.z + dz * invLength * 32", self.source)
        self.assertIn("sim.lookAtLocation(\n                target,", self.source)
        self.assertNotIn("relativeLocation", self.source)
        self.assertNotIn("activeTest", self.source)
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
