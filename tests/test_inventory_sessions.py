import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


class ScalarTag:
    def __init__(self, value=0):
        self.value = value


class ByteTag(ScalarTag):
    pass


class ShortTag(ScalarTag):
    pass


class IntTag(ScalarTag):
    pass


class LongTag(ScalarTag):
    pass


class FloatTag(ScalarTag):
    pass


class DoubleTag(ScalarTag):
    pass


class StringTag(ScalarTag):
    pass


class ByteArrayTag(list):
    pass


class IntArrayTag(list):
    pass


class ListTag(list):
    pass


class CompoundTag(dict):
    pass


class ItemType:
    def __init__(self, type_id):
        self.id = type_id


class ItemStack:
    def __init__(self, type, amount=1, data=0):
        self.type = ItemType(type)
        self.amount = amount
        self.data = data
        self.nbt = CompoundTag()


endstone = types.ModuleType("endstone")
endstone_inventory = types.ModuleType("endstone.inventory")
endstone_nbt = types.ModuleType("endstone.nbt")
endstone_inventory.ItemStack = ItemStack
for cls in (
    ByteArrayTag,
    ByteTag,
    CompoundTag,
    DoubleTag,
    FloatTag,
    IntArrayTag,
    IntTag,
    ListTag,
    LongTag,
    ShortTag,
    StringTag,
):
    setattr(endstone_nbt, cls.__name__, cls)
sys.modules.setdefault("endstone", endstone)
sys.modules["endstone.inventory"] = endstone_inventory
sys.modules["endstone.nbt"] = endstone_nbt

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "inventory_sessions_under_test",
    ROOT / "endstone_bot" / "inventory_sessions.py",
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class FakeInventory:
    def __init__(self):
        self.slots = [None] * 36
        self.held_item_slot = 0

    def __len__(self):
        return len(self.slots)

    def get_item(self, index):
        return self.slots[index]

    def set_item(self, index, item):
        self.slots[index] = item

    def clear(self, index):
        self.slots[index] = None


class FakePlayer:
    def __init__(self, name="Player", uuid="uuid-1"):
        self.name = name
        self.unique_id = uuid
        self.inventory = FakeInventory()


class FakeBot:
    def __init__(self, bot_id="bot-id", name="Bot"):
        self.id = bot_id
        self.name = name


class Logger:
    def warning(self, *_):
        pass

    def error(self, *_):
        pass


class InventorySessionTests(unittest.TestCase):
    def test_snapshot_round_trip_preserves_typed_nbt(self):
        player = FakePlayer()
        item = ItemStack("minecraft:diamond_sword", 1, 7)
        item.nbt = CompoundTag(
            {
                "display": CompoundTag({"Name": StringTag("Looting sword")}),
                "ench": ListTag(
                    [
                        CompoundTag(
                            {
                                "id": ShortTag(14),
                                "lvl": ShortTag(3),
                            }
                        )
                    ]
                ),
                "flags": ByteArrayTag([-1, 0, 1]),
                "numbers": IntArrayTag([1, 2, 3]),
            }
        )
        player.inventory.set_item(0, item)
        player.inventory.held_item_slot = 4

        snapshot = module.capture_player_inventory(player)
        for index in range(36):
            player.inventory.clear(index)

        module.restore_player_inventory(player, snapshot)
        restored = module.capture_player_inventory(player)

        self.assertEqual(module.snapshot_digest(snapshot), module.snapshot_digest(restored))
        self.assertEqual(player.inventory.held_item_slot, 4)

    def test_restore_refuses_non_empty_destination(self):
        player = FakePlayer()
        snapshot = module.capture_player_inventory(player)
        player.inventory.set_item(2, ItemStack("minecraft:stone", 1))

        with self.assertRaises(module.InventorySnapshotError):
            module.restore_player_inventory(player, snapshot)

        self.assertEqual(player.inventory.get_item(2).type.id, "minecraft:stone")

    def test_only_confirmed_editing_state_auto_recovers_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            player = FakePlayer()
            bot = FakeBot()
            store = module.InventorySessionStore(root, Logger())
            rec = store.create(player, bot)
            store.set_state(rec["session_id"], "editing")

            reloaded = module.InventorySessionStore(root, Logger())
            self.assertEqual(reloaded.for_bot_id(bot.id)["state"], "recovery_pending")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            player = FakePlayer()
            bot = FakeBot()
            store = module.InventorySessionStore(root, Logger())
            store.create(player, bot)

            reloaded = module.InventorySessionStore(root, Logger())
            rec = reloaded.for_bot_id(bot.id)
            self.assertEqual(rec["state"], "manual_review")
            self.assertEqual(rec["restart_from_state"], "preparing")

    def test_prepared_state_is_recoverable_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            player = FakePlayer()
            bot = FakeBot()
            store = module.InventorySessionStore(root, Logger())
            rec = store.create(player, bot)
            player.inventory.set_item(0, ItemStack("minecraft:diamond_sword", 1))
            marked = store.mark_swapped(
                rec["session_id"],
                player,
                bot_held_slot=0,
                bot_game_mode="Survival",
                player_game_mode="Survival",
            )
            self.assertEqual(marked["state"], "prepared")

            reloaded = module.InventorySessionStore(root, Logger())
            self.assertEqual(reloaded.for_bot_id(bot.id)["state"], "recovery_pending")

    def test_one_player_cannot_own_two_inventory_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            store = module.InventorySessionStore(Path(td), Logger())
            player = FakePlayer()
            store.create(player, FakeBot("a", "A"))

            with self.assertRaises(module.InventorySnapshotError):
                store.create(player, FakeBot("b", "B"))

    def test_mark_swapped_persists_bot_backup_before_editing(self):
        with tempfile.TemporaryDirectory() as td:
            store = module.InventorySessionStore(Path(td), Logger())
            player = FakePlayer()
            player.inventory.set_item(0, ItemStack("minecraft:dirt", 5))
            rec = store.create(player, FakeBot())

            # Simulate the physical swap: the real player's live inventory now
            # contains the bot's inventory before editing is exposed.
            player.inventory.set_item(0, ItemStack("minecraft:diamond_sword", 1))
            marked = store.mark_swapped(
                rec["session_id"],
                player,
                bot_held_slot=2,
                bot_game_mode="Survival",
                player_game_mode="Survival",
            )

            self.assertEqual(marked["state"], "prepared")
            self.assertIsNotNone(marked["bot_backup"])
            self.assertEqual(marked["bot_backup"]["held_slot"], 2)
            self.assertEqual(marked["bot_backup"]["slots"][0]["type"], "minecraft:diamond_sword")


if __name__ == "__main__":
    unittest.main()
