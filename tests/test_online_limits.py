"""Exercise creation, admission and inventory returns at the online boundary."""
import ast
import importlib.util
import json
import math
import secrets
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("online_limit_models", ROOT / "endstone_bot/models.py")
models = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = models
spec.loader.exec_module(models)
tree = ast.parse((ROOT / "endstone_bot/manager.py").read_text(encoding="utf-8"))
node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FakeBotManager")
namespace = dict(
    json=json, time=time, math=math, secrets=secrets, FakePlayer=models.FakePlayer,
    generate_id=models.generate_id, validate_name=models.validate_name,
    format_date_time_beijing=models.format_date_time_beijing,
)
exec(compile(ast.fix_missing_locations(ast.Module(
    body=[ast.ImportFrom("__future__", [ast.alias("annotations")], 0), node], type_ignores=[])),
    "manager.py", "exec"), namespace)
Manager = namespace["FakeBotManager"]


class OnlineLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.online = []
        self.sent = []
        self.sessions = {}
        self.messages = []
        self.limits = NS(unlimited=False, max_bots=100, cooldown_seconds=0, bypass_global_limit=True)
        self.owner = NS(name="Owner", unique_id="owner", is_op=False,
                        location=NS(x=0, y=80, z=0, dimension="overworld"))
        self.m = self.new_manager()

    def new_manager(self):
        m = Manager.__new__(Manager)
        m._plugin = NS(
            _stopping=False, is_admin=lambda sender: sender.is_op,
            dimension_id=lambda d: d,
            server=NS(online_players=self.online,
                      get_player=lambda n: next((p for p in self.online if p.name == n), None)),
        )
        m._bridge = NS(active=True, send_bridge=lambda *a: self.sent.append(a) or True)
        m._settings = NS(max_total=10, remember_player=lambda *args: "owner",
                         effective_for_key=lambda key: self.limits)
        m._logger = NS(warning=self.messages.append, error=self.messages.append, info=self.messages.append)
        m._db_path = Path(self.tmp.name) / "bots.json"
        m.bots, m.name_index, m._runtime = {}, {}, {}
        m._pending_spawn, m._last_spawn_request, m._spawn_cooldowns = {}, {}, {}
        m._dirty = False
        m.inventory_journal = NS(get_for_bot=lambda key: self.sessions.get(key),
                                 put=lambda key, value: self.sessions.update({key: dict(value)}))
        m._notify_session_player = lambda *a, **k: self.messages.append(a[1])
        return m

    def definition(self, i, online=False):
        fp = models.FakePlayer(
            id=f"{i:032x}", name=f"Bot{i}", owner_name="Owner", owner_uuid="owner",
            location_x=0, location_y=80, location_z=0, dimension="overworld",
            pitch=0, yaw=0, created="2026-11-27", desired_online=online,
        )
        self.m.bots[fp.id] = fp
        self.m.name_index[fp.name.lower()] = fp.id
        return fp

    def attach(self, fp):
        player = NS(name=fp.name, scoreboard_tags=["endstone_bot_managed"])
        self.online.append(player)
        fp.sim_spawn_confirmed = True
        return player

    def fill(self, count):
        return [self.attach(self.definition(i, online=True)) for i in range(count)]

    def test_more_than_ten_definitions_can_be_created_by_player_and_console(self):
        for i in range(10):
            self.definition(i)
        ok, message = self.m.create_for_player(self.owner, "Eleventh")
        self.assertTrue(ok, message)
        ok, message = self.m.create_at("Twelfth", "Owner", "owner", 0, 80, 0, "overworld")
        self.assertTrue(ok, message)
        self.assertEqual(len(json.loads(self.m._db_path.read_text())["bots"]), 12)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(payload["maxOnline"] == 10 for _, payload in self.sent))

    def test_personal_ownership_quota_still_applies_to_offline_definitions(self):
        self.limits.max_bots = 1
        self.definition(0)
        ok, message = self.m.create_for_player(self.owner, "Second")
        self.assertFalse(ok)
        self.assertIn("拥有 1", message)
        self.assertEqual(len(self.m.bots), 1)

    def test_full_server_keeps_new_definitions_offline_even_for_admin(self):
        self.fill(10)
        self.owner.is_op = True
        self.assertTrue(self.m.create_for_player(self.owner, "Eleventh")[0])
        self.assertTrue(self.m.create_at("Twelfth", "Owner", "owner", 0, 80, 0, "overworld")[0])
        self.assertFalse(self.m.get_by_name("Eleventh").desired_online)
        self.assertFalse(self.m.get_by_name("Twelfth").desired_online)
        self.assertEqual(len(self.m.bots), 12)
        self.assertEqual(self.sent, [])

    def test_manual_online_at_capacity_does_not_change_offline_intent(self):
        self.fill(10)
        candidate = self.definition(11)
        self.owner.is_op = True
        ok, message = self.m.set_online(self.owner, candidate, True)
        self.assertFalse(ok)
        self.assertIn("同时在线", message)
        self.assertFalse(candidate.desired_online)
        self.assertEqual(self.sent, [])

    def test_pending_spawn_claims_last_slot_and_live_ack_does_not_count_twice(self):
        self.fill(9)
        first, second = self.definition(10, True), self.definition(11, True)
        self.assertTrue(self.m.spawn(first, force=True))
        self.assertFalse(self.m.spawn(second, force=True))
        self.attach(first)
        self.assertIsNone(self.m.online_limit_error(first))
        self.assertIsNotNone(self.m.online_limit_error(second))
        self.assertEqual(self.m.online_count(), 10)
        self.assertEqual(len(self.sent), 1)

    def test_slot_is_released_only_after_actual_disconnect(self):
        players = self.fill(10)
        retiring = self.m.get_by_name("Bot0")
        candidate = self.definition(11)
        self.m.persist_bot_inventory = lambda fp: True
        self.assertTrue(self.m.set_online(self.owner, retiring, False)[0])
        self.assertFalse(retiring.sim_spawn_confirmed)
        self.assertEqual(self.m.online_count(), 10)
        self.assertFalse(self.m.set_online(self.owner, candidate, True)[0])
        self.online.remove(players[0])
        self.assertTrue(self.m.set_online(self.owner, candidate, True)[0])

    def test_send_failure_does_not_hold_a_slot(self):
        self.fill(9)
        first, second = self.definition(10, True), self.definition(11, True)
        self.m._bridge.send_bridge = lambda *a: False
        self.assertFalse(self.m.spawn(first, force=True))
        self.assertNotIn(first.id, self.m._pending_spawn)
        self.m._bridge.send_bridge = lambda *a: self.sent.append(a) or True
        self.assertTrue(self.m.spawn(second, force=True))

    def test_only_matching_failure_releases_current_pending_request(self):
        self.fill(9)
        first, second = self.definition(10, True), self.definition(11, True)
        self.assertTrue(self.m.spawn(first, force=True))
        error = dict(n=first.name, definition=first.id, generation="f" * 32, op="spawn")
        self.assertFalse(self.m.reject_spawn(error))
        self.assertFalse(self.m.spawn(second, force=True))
        error["generation"] = self.m._pending_spawn[first.id]
        self.assertTrue(self.m.reject_spawn(error))
        self.assertTrue(self.m.spawn(second, force=True))

    def test_lost_ack_reservation_expires_at_existing_retry_window(self):
        self.fill(9)
        first, second = self.definition(10, True), self.definition(11, True)
        self.assertTrue(self.m.spawn(first, force=True))
        self.m._last_spawn_request[first.id] = time.monotonic() - 11
        self.assertFalse(self.m._spawn_request_pending(first.id))
        self.assertTrue(self.m.spawn(second, force=True))

    def test_restart_restores_all_definitions_but_only_admits_ten(self):
        for i in range(12):
            self.definition(i, online=True)
        self.m.save()
        self.m = self.new_manager()
        self.m.restore()
        self.m.ensure_all_spawned()
        self.assertEqual(len(self.m.bots), 12)
        self.assertEqual(len(self.sent), 10)
        self.assertEqual(len(self.m._pending_spawn), 10)
        self.assertTrue(all(fp.desired_online for fp in self.m.bots.values()))

    def test_existing_object_can_be_reauthenticated_at_zero_capacity(self):
        self.fill(1)
        fp = self.m.get_by_name("Bot0")
        fp.desired_online = False
        self.m._settings.max_total = 0
        self.assertTrue(self.m.spawn(fp, force=True, authenticate=True))
        self.assertEqual(self.sent[-1][1]["maxOnline"], 0)
        other = self.definition(1, True)
        self.assertFalse(self.m.spawn(other, force=True, authenticate=True))

    def test_humans_do_not_use_bot_slots(self):
        self.online.extend(NS(name=f"Human{i}", scoreboard_tags=[]) for i in range(20))
        candidate = self.definition(1, True)
        self.assertTrue(self.m.spawn(candidate, force=True))
        self.assertEqual(self.m.online_count(), 0)

    def test_inventory_return_at_capacity_keeps_borrowed_state_and_items(self):
        self.fill(10)
        candidate = self.definition(11)
        session = dict(state="BORROWED", player_name="Owner", player_uuid="owner",
                       player_backup={"slots": ["original"]}, bot_backup={"slots": ["bot-item"]})
        self.sessions[candidate.id] = session
        self.owner.inventory = ["edited-bot-item"]
        ok, message = self.m.finish_inventory_edit(self.owner, candidate)
        self.assertFalse(ok)
        self.assertEqual(self.sessions[candidate.id]["state"], "BORROWED")
        self.assertEqual(self.owner.inventory, ["edited-bot-item"])
        self.assertEqual(self.sent, [])

    def test_async_spawn_failure_returns_inventory_session_to_borrowed(self):
        candidate = self.definition(11)
        self.sessions[candidate.id] = dict(state="BORROWED", player_name="Owner", player_uuid="owner")
        self.owner.inventory = ["edited-bot-item"]
        self.assertTrue(self.m.finish_inventory_edit(self.owner, candidate)[0])
        self.assertEqual(self.sessions[candidate.id]["state"], "WAIT_SPAWN_FOR_RETURN")
        self.assertTrue(self.m.reject_spawn(dict(
            op="spawn", n=candidate.name, definition=candidate.id,
            generation=self.m._pending_spawn[candidate.id], reason="online_limit",
        )))
        self.assertEqual(self.sessions[candidate.id]["state"], "BORROWED")
        self.assertEqual(self.owner.inventory, ["edited-bot-item"])
        self.assertNotIn(candidate.id, self.m._pending_spawn)


if __name__ == "__main__":
    unittest.main()
