import importlib.util
import json
import sys
import types
import unittest
import time
from pathlib import Path

root = Path(__file__).resolve().parents[1]

endstone = types.ModuleType("endstone")
endstone_event = types.ModuleType("endstone.event")
endstone_event.ScriptMessageEvent = object
sys.modules.setdefault("endstone", endstone)
sys.modules.setdefault("endstone.event", endstone_event)

spec = importlib.util.spec_from_file_location("bridge_under_test", root / "endstone_bot/bridge.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
BridgeManager = module.BridgeManager
BRIDGE_PROTOCOL = module.BRIDGE_PROTOCOL


class Logger:
    def debug(self, *_):
        pass

    def info(self, *_):
        pass

    def warning(self, *_):
        pass

    def error(self, *_):
        pass


class BridgeTests(unittest.TestCase):
    def test_hex_callback_accepts_authenticated_hello_ack(self):
        bridge = BridgeManager(Logger(), lambda _: True)
        payload = {
            "ok": True,
            "t": bridge._token,
            "p": BRIDGE_PROTOCOL,
        }
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8").hex()

        parsed = bridge.handle_command_callback("hello_ack", encoded)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["id"], "bot:hello_ack")
        self.assertTrue(bridge.active)

    def test_hex_callback_rejects_wrong_token(self):
        bridge = BridgeManager(Logger(), lambda _: True)
        payload = {
            "ok": True,
            "t": "0" * 32,
            "p": BRIDGE_PROTOCOL,
        }
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8").hex()

        self.assertIsNone(bridge.handle_command_callback("hello_ack", encoded))
        self.assertFalse(bridge.active)

    def test_hex_callback_handles_unicode_payload(self):
        bridge = BridgeManager(Logger(), lambda _: True)
        ack = {
            "ok": True,
            "t": bridge._token,
            "p": BRIDGE_PROTOCOL,
        }
        ack_hex = json.dumps(ack, ensure_ascii=False, separators=(",", ":")).encode("utf-8").hex()
        self.assertIsNotNone(bridge.handle_command_callback("hello_ack", ack_hex))

        payload = {
            "n": "测试假人",
            "e": "错误：三叉戟",
            "t": bridge._token,
            "p": BRIDGE_PROTOCOL,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8").hex()
        parsed = bridge.handle_command_callback("error", encoded)

        self.assertEqual(parsed["data"]["n"], "测试假人")
        self.assertEqual(parsed["data"]["e"], "错误：三叉戟")

    def test_inventory_actions_dispatch_authenticated_script_events(self):
        commands = []
        bridge = BridgeManager(Logger(), lambda command: commands.append(command) or True)
        bridge._ready = True
        bridge._last_seen_at = time.monotonic()

        self.assertEqual(BRIDGE_PROTOCOL, 3)
        self.assertTrue(
            bridge.send_bridge(
                "inventory_begin",
                {"n": "Bot", "r": "Player", "s": "a" * 32},
            )
        )
        self.assertTrue(commands[0].startswith("scriptevent bot:inventory_begin "))
        self.assertIn('"s":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"', commands[0])

    def test_trident_action_dispatches_script_event(self):
        commands = []
        bridge = BridgeManager(Logger(), lambda command: commands.append(command) or True)
        bridge._ready = True
        bridge._last_seen_at = time.monotonic()

        ok = bridge.send_bridge("trident", {
            "n": "AimBot",
            "x": 1,
            "y": 64,
            "z": 2,
            "d": "overworld",
            "pitch": -20,
            "yaw": 90,
        })

        self.assertTrue(ok)
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0].startswith("scriptevent bot:trident "))
        self.assertIn('"pitch":-20', commands[0])
        self.assertIn('"yaw":90', commands[0])


if __name__ == "__main__":
    unittest.main()
