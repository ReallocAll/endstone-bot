import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "endstone_bot" / "plugin.py"


class LoggingNoiseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PLUGIN.read_text(encoding="utf-8")

    def test_bridge_dispatch_uses_quiet_command_sender_wrapper(self):
        self.assertIn("CommandSenderWrapper", self.source)
        self.assertIn("on_message=lambda _message: None", self.source)
        self.assertIn("on_error=self._on_bridge_command_error", self.source)
        self.assertIn('getattr(self, "_bridge_command_sender", self.server.command_sender)', self.source)

    def test_bridge_errors_remain_visible(self):
        self.assertIn('self.logger.warning(f"bridge 命令执行失败: {message}")', self.source)


if __name__ == "__main__":
    unittest.main()
