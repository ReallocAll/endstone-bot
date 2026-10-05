import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "endstone_bot" / "manager.py"
PLUGIN = ROOT / "endstone_bot" / "plugin.py"


class ManagerSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MANAGER.read_text(encoding="utf-8")
        cls.plugin_source = PLUGIN.read_text(encoding="utf-8")

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

    def test_inventory_edit_has_single_owner_locking(self):
        self.assertIn("def toggle_inventory_edit(", self.source)
        self.assertIn("self._plugin.inventory_sessions.for_player(sender)", self.source)
        self.assertIn("self.inventory_locked(fp)", self.source)
        self.assertIn('"inventory_begin"', self.source)
        self.assertIn('"inventory_finish"', self.source)

    def test_plugin_blocks_removeall_during_inventory_custody(self):
        self.assertIn("if self.inventory_sessions.all():", self.plugin_source)
        self.assertIn("不能执行 removeall", self.plugin_source)

    def test_plugin_recovers_only_from_persisted_recovery_state(self):
        self.assertIn("def _inventory_recovery_poll(", self.plugin_source)
        self.assertIn('"recovery_pending"', self.plugin_source)
        self.assertIn('"inventory_recover"', self.plugin_source)
        self.assertIn('"inventory_recovery_finalize"', self.plugin_source)

    def test_bridge_dispatch_uses_quiet_command_sender_wrapper(self):
        self.assertIn("CommandSenderWrapper", self.plugin_source)
        self.assertIn("on_message=lambda _message: None", self.plugin_source)
        self.assertIn("on_error=self._on_bridge_command_error", self.plugin_source)
        self.assertIn('getattr(self, "_bridge_command_sender", self.server.command_sender)', self.plugin_source)

    def test_bridge_errors_remain_visible(self):
        self.assertIn('self.logger.warning(f"bridge 命令执行失败: {message}")', self.plugin_source)


if __name__ == "__main__":
    unittest.main()
