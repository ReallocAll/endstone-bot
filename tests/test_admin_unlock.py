"""Behavioral tests of OP-only forced bot return with durable recovery evidence."""
from __future__ import annotations

import ast
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_class(filename: str, name: str, namespace: dict):
    tree = ast.parse((ROOT / "endstone_bot" / filename).read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    module = ast.Module(
        body=[ast.ImportFrom("__future__", [ast.alias("annotations")], 0), node],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), filename, "exec"), namespace)
    return namespace[name]


class Inventory:
    def __init__(self, items):
        self.contents = copy.deepcopy(items)
        self.size = len(items)
        self.held_item_slot = 0
        self.corrupt_on_restore = False
        self.normalized_digest = False

    @property
    def is_empty(self):
        return not any(self.contents)

    def clear(self):
        self.contents = [None] * self.size


def snapshot(inv):
    return {
        "size": inv.size,
        "slots": copy.deepcopy(inv.contents),
        "held_item_slot": inv.held_item_slot,
    }


def digest(value):
    return json.dumps(value, sort_keys=True)


def restore(inv, value):
    inv.contents = copy.deepcopy(value["slots"])
    inv.held_item_slot = value.get("held_item_slot", 0)
    if inv.corrupt_on_restore:
        inv.contents[0] = {"type": "corrupted", "amount": 1, "data": 0}


def inventory_digest(inv):
    return "native-NBT-normalization" if inv.normalized_digest else digest(snapshot(inv))


class Logger:
    def __init__(self):
        self.warnings = []
        self.errors = []

    def warning(self, message):
        self.warnings.append(message)

    def error(self, message):
        self.errors.append(message)


InventoryJournal = load_class(
    "inventory_journal.py", "InventoryJournal",
    {"json": json, "os": os, "time": time, "Path": Path},
)
FakeBotManager = load_class(
    "manager.py", "FakeBotManager",
    {
        "time": time, "snapshot_inventory": snapshot, "snapshot_digest": digest,
        "inventory_digest": inventory_digest, "restore_inventory": restore,
        "empty_inventory": lambda inv: inv.clear(),
    },
)


def item(name, amount=1):
    return {"type": name, "amount": amount, "data": 0, "nbt": {"t": "compound", "v": {}}}


class AdminForceReturnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logger = Logger()
        self.journal_path = Path(self.tmp.name) / "inventory_journal.json"
        self.journal = InventoryJournal(self.journal_path, self.logger)
        self.fp = NS(id="a" * 32, name="Farshs2")
        self.original = [item("diamond_sword"), None]
        self.edited = [item("rotten_flesh", 2), item("clock")]
        self.current_player = [item("partial_restore"), None]
        self.current_bot = copy.deepcopy(self.edited)
        self.borrower = NS(
            name="Farshs54", unique_id="player-uuid", inventory=Inventory(self.current_player)
        )
        self.borrower.messages = []
        self.borrower.send_message = self.borrower.messages.append
        self.bot = NS(name=self.fp.name, inventory=Inventory(self.current_bot))
        self.online = {"farshs54": self.borrower, "farshs2": self.bot}
        self.session = {
            "bot_id": self.fp.id, "bot_name": self.fp.name,
            "player_uuid": self.borrower.unique_id,
            "player_name": self.borrower.name, "state": "BOT_RESTORED",
            "player_backup": snapshot(Inventory(self.original)),
            "player_digest": digest(snapshot(Inventory(self.original))),
            "bot_backup": snapshot(Inventory([item("clock"), None])),
            "bot_digest": digest(snapshot(Inventory([item("clock"), None]))),
            "edited_bot": snapshot(Inventory(self.edited)),
            "edited_bot_digest": digest(snapshot(Inventory(self.edited))),
        }
        self.journal.put(self.fp.id, self.session)
        manager = self.m = FakeBotManager.__new__(FakeBotManager)
        manager.inventory_journal = self.journal
        manager.bots = {self.fp.id: self.fp}
        manager.name_index = {self.fp.name.lower(): self.fp.id}
        manager._plugin = NS(
            _stopping=False, is_admin=lambda sender: sender.name == "Admin"
        )
        manager._logger = self.logger
        manager._bridge = NS(active=True)
        manager._pending_spawn = {}
        manager._online_player_named = lambda name: self.online.get(name.lower())
        manager._bot_player = lambda fp: self.online.get(fp.name.lower())
        self.spawn_calls = []
        self.spawn_ok = True

        def spawn(fp, force=False):
            self.spawn_calls.append((fp.id, force))
            return self.spawn_ok

        manager.spawn = spawn
        self.admin = NS(name="Admin")
        self.other = NS(name="Farshs54")

    def archive(self):
        files = list((self.journal_path.parent / "inventory_recovery").glob("*.json"))
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text(encoding="utf-8"))

    def test_only_admin_may_force_return(self):
        ok, message = self.m.admin_force_return_inventory(self.other, self.fp)
        self.assertFalse(ok)
        self.assertIn("只有管理员", message)
        self.assertEqual(self.journal.get_for_bot(self.fp.id), self.session)
        self.assertEqual(self.borrower.inventory.contents, self.current_player)
        self.assertFalse((self.journal_path.parent / "inventory_recovery").exists())

    def test_bot_restored_force_restores_player_and_preserves_live_copies(self):
        # The native layer can change NBT representation despite a successful write.
        self.borrower.inventory.normalized_digest = True
        self.bot.inventory.normalized_digest = True
        ok, message = self.m.admin_force_return_inventory(self.admin, self.fp)
        self.assertTrue(ok, message)
        self.assertEqual(self.borrower.inventory.contents, self.original)
        self.assertEqual(self.bot.inventory.contents, self.edited)
        self.assertIsNone(self.journal.get_for_bot(self.fp.id))
        saved = self.archive()
        self.assertEqual(saved["session"]["state"], "BOT_RESTORED")
        self.assertEqual(saved["session"]["player_backup"]["slots"], self.original)
        self.assertEqual(saved["live_player"]["slots"], self.current_player)
        self.assertEqual(saved["live_bot"]["slots"], self.current_bot)
        self.assertEqual(saved["operator"], "Admin")
        self.assertTrue(any("NBT/digest" in msg for msg in self.logger.warnings))
        self.assertEqual(InventoryJournal(self.journal_path, self.logger).sessions, {})

    def test_borrowed_force_returns_current_edited_bag_after_bot_spawn(self):
        self.online.pop("farshs2")
        self.borrower.inventory.contents = copy.deepcopy(self.edited)
        session = dict(self.session)
        session["state"] = "BORROWED"
        session.pop("edited_bot", None)
        session.pop("edited_bot_digest", None)
        self.journal.put(self.fp.id, session)

        ok, message = self.m.admin_force_return_inventory(self.admin, self.fp)
        self.assertTrue(ok, message)
        self.assertEqual(self.journal.get_for_bot(self.fp.id)["state"], "WAIT_SPAWN_FOR_RETURN")
        self.assertEqual(self.borrower.inventory.contents, self.edited)
        self.assertEqual(self.spawn_calls, [(self.fp.id, True)])

        self.bot.inventory.clear()
        self.online["farshs2"] = self.bot
        self.m._complete_spawned_inventory_session(self.fp)
        self.assertIsNone(self.journal.get_for_bot(self.fp.id))
        self.assertEqual(self.borrower.inventory.contents, self.original)
        self.assertEqual(self.bot.inventory.contents, self.edited)
        saved = self.archive()
        self.assertEqual(saved["live_player"]["slots"], self.edited)
        self.assertEqual(saved["live_bot"]["slots"], [None, None])

    def test_spawn_failure_reverts_to_borrowed_without_forced_marker(self):
        self.online.pop("farshs2")
        self.borrower.inventory.contents = copy.deepcopy(self.edited)
        session = dict(self.session)
        session["state"] = "BORROWED"
        self.journal.put(self.fp.id, session)
        self.spawn_ok = False
        ok, _ = self.m.admin_force_return_inventory(self.admin, self.fp)
        self.assertFalse(ok)
        actual = self.journal.get_for_bot(self.fp.id)
        self.assertEqual(actual["state"], "BORROWED")
        self.assertNotIn("admin_force_return", actual)
        self.assertEqual(self.borrower.inventory.contents, self.edited)

    def test_interrupted_player_cleared_transaction_can_complete(self):
        self.borrower.inventory.clear()
        self.bot.inventory.clear()
        session = dict(self.session)
        session["state"] = "PLAYER_CLEARED"
        self.journal.put(self.fp.id, session)
        ok, message = self.m.admin_force_return_inventory(self.admin, self.fp)
        self.assertTrue(ok, message)
        self.assertEqual(self.borrower.inventory.contents, self.original)
        self.assertEqual(self.bot.inventory.contents, self.edited)
        self.assertIsNone(self.journal.get_for_bot(self.fp.id))

    def test_borrower_must_be_online_with_matching_identity(self):
        self.online.pop("farshs54")
        ok, message = self.m.admin_force_return_inventory(self.admin, self.fp)
        self.assertFalse(ok)
        self.assertIn("必须在线", message)
        self.assertEqual(self.journal.get_for_bot(self.fp.id), self.session)

    def test_archive_failure_never_modifies_live_items(self):
        with mock.patch.object(self.journal, "archive_session", side_effect=OSError("disk full")):
            ok, message = self.m.admin_force_return_inventory(self.admin, self.fp)
        self.assertFalse(ok)
        self.assertIn("锁保留", message)
        self.assertEqual(self.borrower.inventory.contents, self.current_player)
        self.assertEqual(self.bot.inventory.contents, self.current_bot)
        self.assertIsNotNone(self.journal.get_for_bot(self.fp.id))

    def test_player_restore_failure_keeps_lock_and_archives_before_changes(self):
        self.borrower.inventory.corrupt_on_restore = True
        ok, message = self.m.admin_force_return_inventory(self.admin, self.fp)
        self.assertFalse(ok)
        self.assertEqual(self.journal.get_for_bot(self.fp.id)["state"], "BOT_RESTORED")
        self.assertEqual(self.archive()["live_player"]["slots"], self.current_player)
        self.assertEqual(self.bot.inventory.contents, self.edited)

    def test_final_journal_write_failure_keeps_recovery_state(self):
        orig = self.journal._write_sessions

        def fail_remove(values):
            if not values:
                raise OSError("synthetic remove failure")
            return orig(values)

        self.journal._write_sessions = fail_remove
        ok, _ = self.m.admin_force_return_inventory(self.admin, self.fp)
        self.assertFalse(ok)
        self.assertEqual(self.journal.get_for_bot(self.fp.id)["state"], "PLAYER_RESTORED")
        self.assertEqual(self.borrower.inventory.contents, self.original)
        self.assertEqual(self.bot.inventory.contents, self.edited)
        self.archive()

    def test_unsupported_state_and_stale_definition_are_rejected(self):
        session = dict(self.session)
        session["state"] = "PREPARED"
        self.journal.put(self.fp.id, session)
        self.assertFalse(self.m.admin_force_return_inventory(self.admin, self.fp)[0])
        self.assertFalse(self.m.admin_force_return_inventory(
            self.admin, NS(id=self.fp.id, name=self.fp.name)
        )[0])
        self.m._plugin._stopping = True
        self.assertFalse(self.m.admin_force_return_inventory(self.admin, self.fp)[0])
        self.assertEqual(self.journal.get_for_bot(self.fp.id), session)

    def test_empty_transaction_not_forced(self):
        self.journal.remove(self.fp.id)
        ok, _ = self.m.admin_force_return_inventory(self.admin, self.fp)
        self.assertFalse(ok)
        self.assertFalse((self.journal_path.parent / "inventory_recovery").exists())


if __name__ == "__main__":
    unittest.main()
