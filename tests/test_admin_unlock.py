"""Behavioral tests for administrator override of a persistent inventory lock."""
from __future__ import annotations

import ast
import json
import os
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace as NS


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
FakeBotManager = load_class("manager.py", "FakeBotManager", {})


class AdminUnlockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logger = Logger()
        self.journal_path = Path(self.tmp.name) / "inventory_journal.json"
        self.journal = InventoryJournal(self.journal_path, self.logger)
        self.fp = NS(id="a" * 32, name="Farshs2")
        self.session = {
            "bot_id": self.fp.id, "bot_name": self.fp.name,
            "player_name": "Farshs54", "state": "BOT_RESTORED",
            "player_backup": {"size": 2, "slots": [{"type": "test:item"}, None]},
            "bot_backup": {"size": 2, "slots": [None, {"type": "test:other"}]},
            "edited_bot": {"size": 2, "slots": [None, None]},
        }
        self.journal.put(self.fp.id, self.session)
        self.m = FakeBotManager.__new__(FakeBotManager)
        self.m.inventory_journal = self.journal
        self.m.bots = {self.fp.id: self.fp}
        self.m._plugin = NS(_stopping=False, is_admin=lambda sender: sender.name == "Admin")
        self.m._logger = self.logger
        self.admin = NS(name="Admin")
        self.player = NS(name="Farshs54")

    def test_non_admin_cannot_unlock(self):
        ok, message = self.m.admin_unlock_inventory(self.player, self.fp)
        self.assertFalse(ok)
        self.assertIn("只有管理员", message)
        self.assertEqual(self.journal.get_for_bot(self.fp.id), self.session)
        self.assertFalse((self.journal_path.parent / "inventory_recovery").exists())

    def test_admin_unlock_archives_complete_transaction_and_removes_lock(self):
        ok, message = self.m.admin_unlock_inventory(self.admin, self.fp)
        self.assertTrue(ok, message)
        self.assertIsNone(self.journal.get_for_bot(self.fp.id))
        archives = list((self.journal_path.parent / "inventory_recovery").glob("*.json"))
        self.assertEqual(len(archives), 1)
        data = json.loads(archives[0].read_text(encoding="utf-8"))
        self.assertEqual(data["session"], self.session)
        self.assertEqual(data["operator"], "Admin")
        self.assertEqual(data["bot_id"], self.fp.id)
        self.assertIn(str(archives[0]), message)
        self.assertTrue(any("BOT_RESTORED" in line for line in self.logger.warnings))
        self.assertEqual(InventoryJournal(self.journal_path, self.logger).sessions, {})

    def test_archive_failure_retains_lock(self):
        with mock.patch.object(Path, "open", side_effect=OSError("disk full")):
            ok, message = self.m.admin_unlock_inventory(self.admin, self.fp)
        self.assertFalse(ok)
        self.assertIn("锁仍然保留", message)
        self.assertEqual(self.journal.get_for_bot(self.fp.id), self.session)
        self.assertEqual(InventoryJournal(self.journal_path, self.logger).sessions[self.fp.id], self.session)

    def test_journal_remove_failure_retains_lock(self):
        original = self.journal._write_sessions

        def fail(new_sessions):
            raise OSError("synthetic fsync failure")

        self.journal._write_sessions = fail
        ok, message = self.m.admin_unlock_inventory(self.admin, self.fp)
        self.assertFalse(ok)
        self.assertIn("锁仍然保留", message)
        self.assertEqual(self.journal.get_for_bot(self.fp.id), self.session)
        self.assertEqual(InventoryJournal(self.journal_path, self.logger).sessions[self.fp.id], self.session)
        self.assertEqual(len(list((self.journal_path.parent / "inventory_recovery").glob("*.json"))), 1)
        self.journal._write_sessions = original

    def test_stale_definition_and_stopping_rejected(self):
        stale = NS(id=self.fp.id, name=self.fp.name)
        self.assertFalse(self.m.admin_unlock_inventory(self.admin, stale)[0])
        self.m._plugin._stopping = True
        self.assertFalse(self.m.admin_unlock_inventory(self.admin, self.fp)[0])
        self.assertEqual(self.journal.get_for_bot(self.fp.id), self.session)

    def test_missing_session_does_not_create_archive(self):
        self.journal.remove(self.fp.id)
        ok, _ = self.m.admin_unlock_inventory(self.admin, self.fp)
        self.assertFalse(ok)
        self.assertFalse((self.journal_path.parent / "inventory_recovery").exists())


if __name__ == "__main__":
    unittest.main()
