import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "endstone_bot" / "behavior_pack" / "scripts" / "main.js"
TRIDENT_ENTITY = ROOT / "endstone_bot" / "behavior_pack" / "entities" / "thrown_trident.json"


class BehaviorPackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_trident_is_taken_from_inventory(self):
        self.assertIn('getComponent("minecraft:inventory")', self.source)
        self.assertIn('item.typeId === "minecraft:trident"', self.source)
        self.assertIn("container.setItem(slot)", self.source)

    def test_trident_is_never_created_as_an_inventory_item(self):
        self.assertNotIn('new ItemStack("minecraft:trident"', self.source)
        self.assertNotIn("sim.setItem(trident", self.source)
        self.assertNotIn("ItemStack,", self.source)

    def test_trident_uses_owned_projectile_component(self):
        self.assertIn('spawnEntity("endstone_bot:thrown_trident"', self.source)
        self.assertIn('getComponent("minecraft:projectile")', self.source)
        self.assertIn("projectile.owner = sim", self.source)
        self.assertIn("projectile.shoot({", self.source)
        self.assertIn("const speed = 2.5", self.source)

    def test_custom_trident_entity_matches_vanilla_damage_model(self):
        entity = json.loads(TRIDENT_ENTITY.read_text(encoding="utf-8"))
        desc = entity["minecraft:entity"]["description"]
        projectile = entity["minecraft:entity"]["components"]["minecraft:projectile"]
        self.assertEqual(desc["identifier"], "endstone_bot:thrown_trident")
        self.assertEqual(desc["runtime_identifier"], "minecraft:thrown_trident")
        self.assertTrue(desc["is_summonable"])
        self.assertEqual(projectile["on_hit"]["impact_damage"]["damage"], 8)
        self.assertTrue(projectile["on_hit"]["impact_damage"]["knockback"])
        self.assertEqual(projectile["gravity"], 0.1)
        self.assertEqual(projectile["power"], 4)

    def test_trident_path_no_longer_uses_simulated_player_item_use(self):
        self.assertNotIn("useItemInSlot(slot)", self.source)
        self.assertNotIn("stopUsingItem()", self.source)

    def test_trident_failure_rolls_back_inventory_and_projectile(self):
        self.assertIn("const originalItem = found.item.clone()", self.source)
        self.assertIn("restoreInventoryItem(found.container, found.slot, originalItem)", self.source)
        self.assertIn("cleanupProjectile(projectileEntity)", self.source)

    def test_enchanted_tridents_are_rejected_for_now(self):
        self.assertIn('item.getComponent("minecraft:enchantable")', self.source)
        self.assertIn("enchanted_trident_unsupported", self.source)

    def test_view_direction_syncs_visual_pose(self):
        self.assertIn("sim.lookAtLocation(target", self.source)
        self.assertIn("sim.setBodyRotation(rotation.y)", self.source)
        self.assertIn("sim.setRotation(rotation)", self.source)
        self.assertIn("req.dx", self.source)
        self.assertIn("req.dy", self.source)
        self.assertIn("req.dz", self.source)


if __name__ == "__main__":
    unittest.main()
