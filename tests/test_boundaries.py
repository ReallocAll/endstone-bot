"""Behavioral tests of production decisions with synthetic native inventory boundaries."""
import ast
import json
import math
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]


def load_class(file, name, namespace):
    tree = ast.parse((ROOT / 'endstone_bot' / file).read_text())
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    code = ast.Module(body=[ast.ImportFrom('__future__', [ast.alias('annotations')], 0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(code), file, 'exec'), namespace)
    return namespace[name]


class Inventory:
    def __init__(self, items):
        self.contents = list(items)
        self.size = len(items)
        self.held_item_slot = 0

    @property
    def is_empty(self):
        return not any(self.contents)

    def clear(self):
        self.contents = [None] * self.size


def snapshot(inv):
    return {'size': inv.size, 'slots': list(inv.contents)}


def digest(value):
    return json.dumps({k: v for k, v in value.items() if k != 'runtime'}, sort_keys=True)


def restore(inv, value):
    inv.contents = list(value['slots'])


class Journal:
    available = True
    def __init__(self): self.sessions = {}
    def get_for_bot(self, key): return self.sessions.get(key)
    def get_for_player(self, *args): return None
    def put(self, key, value): self.sessions[key] = dict(value)
    def remove(self, key): self.sessions.pop(key, None)


class Store:
    available = True
    def __init__(self, value=None): self.value = value
    def get(self, key): return self.value
    def put(self, key, value): self.value = dict(value)
    def remove(self, key): self.value = None


class Log:
    def __getattr__(self, key): return lambda *args: None


Manager = load_class('manager.py', 'FakeBotManager', dict(
    json=json, time=time, math=math, secrets=secrets, snapshot_inventory=snapshot,
    snapshot_digest=digest, inventory_digest=lambda inv: digest(snapshot(inv)),
    restore_inventory=restore, empty_inventory=lambda inv: inv.clear()))


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fp = NS(id='a'*32, name='Bot', owner_uuid='owner', owner_name='Owner',
                     desired_online=True, sim_spawn_confirmed=True)
        self.owner = NS(name='Owner', unique_id='owner', inventory=Inventory(['personal', None]))
        self.bot = NS(name='Bot', unique_id='runtime-uuid', inventory=Inventory([None, None]),
                      scoreboard_tags=['endstone_bot_def:'+'a'*32,
                                       'endstone_bot_runtime:'+'b'*32,
                                       'endstone_bot_request:'+'c'*32])
        self.online = [self.bot, self.owner]
        m = self.m = Manager.__new__(Manager)
        m._plugin = NS(server=NS(get_player=lambda n: next((p for p in self.online if p.name == n), None),
                                 online_players=self.online), is_admin=lambda p: getattr(p, 'is_op', False))
        self.sent = []
        m._bridge = NS(active=True, send_bridge=lambda *a: self.sent.append(a) or True)
        m._settings = NS(remember_player=lambda *args: 'owner')
        m._logger = Log()
        m.bots = {self.fp.id: self.fp}
        m.name_index = {'bot': self.fp.id}
        m._runtime = {self.fp.id: {'runtime': 'b'*32, 'generation': 'c'*32}}
        m._pending_spawn = {}
        m._last_spawn_request = {}
        m.inventory_journal = Journal()
        m.bot_inventory_store = Store()
        m.save = lambda: None

    def test_human_name_collision_never_reads_or_changes_inventory(self):
        self.bot.scoreboard_tags = []
        self.bot.inventory.contents = ['human-item', None]
        self.assertFalse(self.m.begin_inventory_edit(self.owner, self.fp)[0])
        self.assertFalse(self.m.persist_bot_inventory(self.fp))
        self.assertEqual(self.bot.inventory.contents, ['human-item', None])
        self.assertEqual(self.owner.inventory.contents, ['personal', None])
        self.assertEqual(self.sent, [])

    def test_verified_bot_borrow_preserves_existing_journal_contract(self):
        self.bot.inventory.contents = ['trident', None]
        self.assertTrue(self.m.begin_inventory_edit(self.owner, self.fp)[0])
        self.assertTrue(self.bot.inventory.is_empty)
        self.assertEqual(self.owner.inventory.contents, ['trident', None])
        self.assertEqual(self.m.inventory_journal.get_for_bot(self.fp.id)['state'], 'BORROWED')

    def test_runtime_uuid_or_request_change_is_rejected(self):
        self.assertIs(self.m._bot_player(self.fp), self.bot)
        self.bot.unique_id = 'new-uuid'
        self.assertIsNone(self.m._bot_player(self.fp))
        self.bot.unique_id = 'runtime-uuid'
        self.bot.scoreboard_tags[-1] = 'endstone_bot_request:'+'d'*32
        self.assertIsNone(self.m._bot_player(self.fp))

    def test_old_and_duplicate_spawn_ack_cannot_bind(self):
        self.m._pending_spawn[self.fp.id] = 'c'*32
        ack = dict(n='Bot', definition=self.fp.id, generation='d'*32, runtime='b'*32, ok=True)
        self.assertIsNone(self.m.accept_spawn(ack))
        ack['generation'] = 'c'*32
        self.assertIs(self.m.accept_spawn(ack), self.fp)
        self.assertIsNone(self.m.accept_spawn(ack))
        ack['definition'] = 'f'*32
        self.assertFalse(self.m.matches_runtime(ack))

    def test_fresh_join_gift_preserves_both_copies_and_blocks_checkpoint(self):
        saved = {'size': 2, 'slots': ['saved-trident', None], 'runtime': 'e'*32}
        self.m.bot_inventory_store.value = saved
        self.bot.inventory.contents = ['join-clock', None]
        for _ in range(2):
            self.assertFalse(self.m.restore_persisted_bot_inventory(self.fp))
            self.assertFalse(self.m.persist_bot_inventory(self.fp))
        self.assertEqual(self.m.bot_inventory_store.value, saved)
        self.assertEqual(self.bot.inventory.contents, ['join-clock', None])
        self.assertFalse(self.m.begin_inventory_edit(self.owner, self.fp)[0])
        # After an administrator reconciles gifts, explicit retry can safely restore.
        self.bot.inventory.clear()
        self.assertTrue(self.m.restore_persisted_bot_inventory(self.fp))
        self.assertEqual(self.bot.inventory.contents, ['saved-trident', None])
        self.assertIsNone(self.m.bot_inventory_store.value)

    def test_adopted_runtime_preserves_changed_or_empty_inventory(self):
        for items in (['current', None], [None, None]):
            self.bot.inventory.contents = items
            self.m.bot_inventory_store.value = {'size': 2, 'slots': ['old', None], 'runtime': 'b'*32}
            self.assertTrue(self.m.restore_persisted_bot_inventory(self.fp))
            self.assertEqual(self.bot.inventory.contents, items)
            self.assertIsNone(self.m.bot_inventory_store.value)

    def test_legacy_snapshot_conflict_fails_closed(self):
        self.m.bot_inventory_store.value = {'size': 2, 'slots': ['saved', None]}
        self.bot.inventory.contents = ['gift', None]
        self.assertFalse(self.m.restore_persisted_bot_inventory(self.fp))
        self.assertEqual(self.bot.inventory.contents, ['gift', None])

    def test_store_remove_failure_does_not_clear_restored_inventory(self):
        self.m.bot_inventory_store.value = {'size': 2, 'slots': ['saved', None]}
        def fail(*args): raise OSError('synthetic disk failure')
        self.m.bot_inventory_store.remove = fail
        self.assertFalse(self.m.restore_persisted_bot_inventory(self.fp))
        self.assertEqual(self.bot.inventory.contents, ['saved', None])
        self.assertIsNotNone(self.m.bot_inventory_store.value)

    def test_stale_definition_and_revoked_owner_cannot_mutate(self):
        old = self.fp
        replacement = NS(id='d'*32, name='Bot', owner_uuid='other', owner_name='Other')
        self.m.bots = {replacement.id: replacement}
        self.m.name_index = {'bot': replacement.id}
        self.assertFalse(self.m.remove(self.owner, old)[0])
        self.owner.is_op = True
        self.assertFalse(self.m.remove(self.owner, old)[0])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.m.name_index['bot'], replacement.id)

    def test_disabled_manager_rejects_retained_form_mutations(self):
        self.m._plugin._stopping = True
        self.assertFalse(self.m.can_manage(self.owner, self.fp))
        self.assertFalse(self.m.create_for_player(self.owner, 'NewBot')[0])
        self.assertFalse(self.m.retry_inventory_restore(self.owner, self.fp)[0])
        self.assertEqual(self.sent, [])

    def test_nonfinite_position_is_not_accepted(self):
        seen = []
        self.fp.mark_seen = lambda *a: seen.append(a)
        self.m.update_position('Bot', float('nan'), 2, 3, 'overworld')
        self.assertEqual(seen, [])

    def test_dimension_change_activates_position_guard(self):
        self.fp.sim_has_position = True
        self.fp.is_recently_seen = lambda: True
        self.fp.sim_actual_x = self.fp.location_x = 0
        self.fp.sim_actual_y = self.fp.location_y = 0
        self.fp.sim_actual_z = self.fp.location_z = 0
        self.fp.sim_actual_dimension = 'nether'
        self.fp.dimension = 'overworld'
        self.m._settings.guard_enabled = True
        self.m._settings.guard_distance = 2
        self.m._pose_payload = lambda fp: {'n': fp.name}
        self.m.guard_positions()
        self.assertEqual(self.sent[0][0], 'teleport')


class Form:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)
    def add_button(self, *args, **kwargs): pass


GUI = load_class('gui.py', 'BotGUI', dict(json=json, **{k: Form for k in
                 ['ActionForm', 'Label', 'MessageForm', 'ModalForm', 'Slider', 'TextInput', 'Toggle']}))


class PermissionTests(unittest.TestCase):
    def test_revoke_admin_before_every_settings_mutation(self):
        writes = []
        settings = NS(override=lambda k: {}, effective_for_key=lambda k: NS(max_bots=2, cooldown_seconds=1, bypass_global_limit=False),
                      max_per_player=2, max_total=10, spawn_cooldown_seconds=1, guard_enabled=True, guard_distance=2,
                      set_override=lambda *a, **kw: writes.append(1), set_unlimited=lambda *a: writes.append(1),
                      reset_override=lambda *a: writes.append(1), set_global=lambda *a: writes.append(1), set_guard=lambda **kw: writes.append(1))
        player = NS(is_op=True, send_message=lambda m: None)
        forms = []
        player.send_form = forms.append
        gui = GUI(NS(settings=settings, is_admin=lambda p: p.is_op))
        gui.open_limit_edit(player, 'key', 'Name')
        gui.open_global_settings(player)
        player.is_op = False
        forms[0].on_submit(player, '[true, 20, true, 0, true]')
        forms[1].on_submit(player, '[20, 10, 0, false, 8]')
        gui._set_unlimited(player, 'key', 'Name')
        gui._reset_limits(player, 'key', 'Name')
        self.assertEqual(writes, [])





class PersistenceAndWireTests(unittest.TestCase):
    def test_runtime_provenance_survives_store_reload_and_is_not_content_digest(self):
        import hashlib
        import os
        namespace = dict(json=json, os=os, Path=Path)
        PersistentStore = load_class('inventory_journal.py', 'PersistentInventoryStore', namespace)
        tree = ast.parse((ROOT / 'endstone_bot/inventory_journal.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'snapshot_digest')
        ns = dict(json=json, hashlib=hashlib, Any=object)
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'inventory_journal.py', 'exec'), ns)
        value = {'size': 1, 'slots': [None], 'runtime': 'a'*32}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'inventory.json'
            PersistentStore(path, Log()).put('bot', value)
            recovered = PersistentStore(path, Log()).get('bot')
            self.assertEqual(recovered, value)
            self.assertEqual(ns['snapshot_digest'](recovered), ns['snapshot_digest']({'size': 1, 'slots': [None]}))

    def test_actual_js_envelope_roundtrips_through_python_bridge_and_consumer(self):
        import subprocess
        from tests.test_bridge import BridgeManager, Logger
        raw = subprocess.check_output(['node', str(ROOT/'tests/bridge_runtime.cjs'), '--json'], text=True)
        batches = json.loads(raw.splitlines()[-1])
        bridge = BridgeManager(Logger(), lambda _: True)
        bridge._token = '1'*32
        bridge._accept_message('bot:hello_ack', {'p':3,'t':bridge._token,'standalone':True})
        Plugin = load_class('plugin.py', 'BotPlugin', {'Plugin':object, 'event_handler':lambda f:f})
        consumer = Plugin()
        positions = []
        consumer.manager = NS(matches_runtime=lambda d: d.get('definition') == 'a'*32,
                              update_position=lambda *args: positions.append(args))
        for batch in batches:
            parsed = bridge.handle_command_callback('positions', json.dumps(batch).encode().hex())
            self.assertIsNotNone(parsed)
            consumer._handle_bridge_message(parsed)
        self.assertEqual(len(positions), 40)
        consumer._handle_bridge_message({'id':'bot:positions','data':{'positions':3}})
        consumer._handle_bridge_message({'id':'bot:positions','data':{'positions':[None, {}]}})
        self.assertEqual(len(positions), 40)

    def test_disable_save_exception_still_relinquishes_session(self):
        Plugin = load_class('plugin.py', 'BotPlugin', {'Plugin':object, 'event_handler':lambda f:f})
        plugin = Plugin()
        plugin.logger = Log()
        sent = []
        def fail(): raise OSError('synthetic save failure')
        plugin.manager = NS(persist_online_bot_inventories=fail, save=fail)
        plugin.bridge = NS(suspend=lambda: sent.append('suspend') or True,
                           shutdown=lambda: sent.append('shutdown') or True)
        plugin.on_disable()
        self.assertTrue(plugin._stopping)
        self.assertEqual(sent, ['suspend'])


if __name__ == '__main__': unittest.main()
