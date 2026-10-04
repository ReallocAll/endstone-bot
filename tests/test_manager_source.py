import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "endstone_bot" / "manager.py"


class ManagerSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MANAGER.read_text(encoding="utf-8")

    def test_trident_throw_uses_saved_pose_only(self):
        start = self.source.index("    def throw_trident_here(")
        end = self.source.index("\n    def teleport_to(", start)
        block = self.source[start:end]
        self.assertNotIn("_capture_player_pose(", block)
        self.assertIn('payload = self._pose_payload(fp)', block)
        self.assertIn("已保存的位置和视角", block)


if __name__ == "__main__":
    unittest.main()
