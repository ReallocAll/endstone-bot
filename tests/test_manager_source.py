import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "endstone_bot" / "manager.py"
PLUGIN = ROOT / "endstone_bot" / "plugin.py"
JOURNAL = ROOT / "endstone_bot" / "inventory_journal.py"
GUI = ROOT / "endstone_bot" / "gui.py"


class ManagerSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MANAGER.read_text(encoding="utf-8")
        cls.plugin_source = PLUGIN.read_text(encoding="utf-8")
        cls.journal_source = JOURNAL.read_text(encoding="utf-8")
        cls.gui_source = GUI.read_text(encoding="utf-8")

    def test_trident_throw_uses_saved_pose_only(self):
        start = self.source.index("    def throw_trident_here(")
        end = self.source.index("\n    def teleport_to(", start)
        block = self.source[start:end]
        self.assertNotIn("_capture_player_pose(", block)
        self.assertIn('payload = self._pose_payload(fp)', block)
        self.assertIn("已保存的位置和视角", block)

    def test_plugin_registers_decorated_event_handlers(self):
        start = self.plugin_source.index("    def on_enable(")
        end = self.plugin_source.index("\n    def _write_beta_patch_script(", start)
        block = self.plugin_source[start:end]
        self.assertIn("self.register_events(self)", block)

    def test_plugin_event_annotations_are_runtime_classes(self):
        # Endstone 0.11 validates inspect.signature() annotations directly and
        # rejects postponed string annotations for Event subclasses.
        self.assertNotIn("from __future__ import annotations", self.plugin_source)
        self.assertIn("def on_player_join(self, event: PlayerJoinEvent) -> None:", self.plugin_source)
        self.assertIn("def on_script_message(self, event: ScriptMessageEvent) -> None:", self.plugin_source)

    def test_bridge_dispatch_uses_quiet_command_sender_wrapper(self):
        self.assertIn("CommandSenderWrapper", self.plugin_source)
        self.assertIn("on_message=lambda _message: None", self.plugin_source)
        self.assertIn("on_error=self._on_bridge_command_error", self.plugin_source)
        self.assertIn('getattr(self, "_bridge_command_sender", self.server.command_sender)', self.plugin_source)

    def test_inventory_borrow_is_exclusive_and_journaled(self):
        self.assertIn("InventoryJournal", self.source)
        self.assertIn('self._put_inventory_session(fp, session, "BOT_CLEARED")', self.source)
        self.assertIn('self._bridge.send_bridge("remove", {"n": fp.name})', self.source)
        self.assertIn('self._put_inventory_session(fp, session, "BORROWED")', self.source)
        self.assertIn('self._put_inventory_session(fp, session, "PLAYER_CLEARED")', self.source)
        self.assertIn('self._put_inventory_session(fp, session, "BOT_RESTORED")', self.source)
        self.assertIn('self._put_inventory_session(fp, session, "PLAYER_RESTORED")', self.source)
        self.assertIn("empty_inventory(bot_player.inventory)", self.source)

    def test_inventory_lock_blocks_mutating_bot_actions(self):
        for method in ("move_here", "throw_trident_here", "teleport_to", "remove"):
            start = self.source.index(f"    def {method}(")
            next_def = self.source.find("\n    def ", start + 5)
            block = self.source[start:] if next_def < 0 else self.source[start:next_def]
            self.assertIn("inventory_locked", block, method)

    def test_inventory_backup_preserves_typed_nbt(self):
        for token in (
            "ByteTag",
            "ShortTag",
            "IntTag",
            "LongTag",
            "FloatTag",
            "DoubleTag",
            "StringTag",
            "ByteArrayTag",
            "IntArrayTag",
            "ListTag",
            "CompoundTag",
        ):
            self.assertIn(token, self.journal_source)
        self.assertIn('"nbt": tag_to_record(item.nbt)', self.journal_source)
        self.assertIn("item.nbt = restored", self.journal_source)
        self.assertIn("os.fsync", self.journal_source)
        self.assertIn("self._write_sessions(updated)", self.journal_source)
        self.assertIn("self.sessions = updated", self.journal_source)

    def test_manual_offline_is_persistent_and_blocks_autospawn(self):
        self.assertIn("not fp.desired_online", self.source)
        self.assertIn("def set_online(", self.source)
        self.assertIn('self._bridge.send_bridge("remove", {"n": fp.name})', self.source)
        self.assertIn('return "手动下线"', self.source)
        self.assertIn('"/bot (online|offline)<action: BotOnlineAction> <name: str>"', self.plugin_source)

    def test_gui_never_uses_dark_gray_color_code(self):
        self.assertNotIn("§7", self.gui_source)

    def test_inventory_guidance_uses_player_language(self):
        self.assertIn("使用 /bot 重新打开假人菜单", self.source)
        self.assertIn("盔甲和副手不会同步", self.source)
        self.assertIn("希望假人拿着的物品拿在手上", self.source)
        self.assertNotIn("热栏槽", self.source)
        self.assertNotIn("主手槽", self.source)
        self.assertIn("仅主背包，不含盔甲和副手", self.gui_source)

    def test_bot_action_callbacks_do_not_reopen_management_gui(self):
        for method in ("_inventory_start", "_inventory_done", "_move_here", "_throw_trident", "_set_online"):
            start = self.gui_source.index(f"    def {method}(")
            next_def = self.gui_source.find("\n    def ", start + 5)
            block = self.gui_source[start:] if next_def < 0 else self.gui_source[start:next_def]
            self.assertNotIn("open_bot(", block, method)
            self.assertNotIn("open_my_bots(", block, method)
            self.assertNotIn("open_admin_bots(", block, method)

    def test_create_success_does_not_auto_open_bot_list(self):
        start = self.gui_source.index("    def open_create(")
        end = self.gui_source.index("\n    def open_my_bots(", start)
        block = self.gui_source[start:end]
        self.assertNotIn("self.open_my_bots(p)", block)

    def test_bridge_errors_remain_visible(self):
        self.assertIn('self.logger.warning(f"bridge 命令执行失败: {message}")', self.plugin_source)


if __name__ == "__main__":
    unittest.main()
