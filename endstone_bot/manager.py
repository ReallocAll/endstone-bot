from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from endstone.command import CommandSender

from endstone_bot.inventory_journal import (
    InventoryJournal,
    empty_inventory,
    inventory_digest,
    restore_inventory,
    snapshot_digest,
    snapshot_inventory,
)
from endstone_bot.models import FakePlayer, format_date_time_beijing, generate_id, validate_name


class FakeBotManager:
    def __init__(self, plugin: Any, data_folder: Path, bridge: Any, settings: Any, logger: Any) -> None:
        self._plugin = plugin
        self._bridge = bridge
        self._settings = settings
        self._logger = logger
        self._db_path = data_folder / "bots.json"
        self.inventory_journal = InventoryJournal(data_folder / "inventory_journal.json", logger)
        self.bots: dict[str, FakePlayer] = {}
        self.name_index: dict[str, str] = {}
        self._spawn_cooldowns: dict[str, float] = {}
        self._last_spawn_request: dict[str, float] = {}
        self._dirty = False

    def restore(self) -> None:
        if not self._db_path.exists():
            return
        try:
            data = json.loads(self._db_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._logger.warning(f"读取 bots.json 失败: {exc}")
            return
        count = 0
        for item in data.get("bots", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            try:
                fp = FakePlayer.from_record(item)
            except Exception as exc:
                self._logger.warning(f"跳过损坏的假人记录: {exc}")
                continue
            if not fp.name or fp.name.lower() in self.name_index:
                continue
            self.bots[fp.id] = fp
            self.name_index[fp.name.lower()] = fp.id
            count += 1
        if count:
            self._logger.info(f"从磁盘恢复了 {count} 个假人定义。")

    def save(self) -> None:
        try:
            tmp = self._db_path.with_suffix(".json.tmp")
            payload = {"version": 7, "bots": [fp.to_record() for fp in self.bots.values()]}
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._db_path)
            self._dirty = False
        except Exception as exc:
            self._logger.warning(f"保存 bots.json 失败: {exc}")

    def save_if_dirty(self) -> None:
        if self._dirty:
            self.save()

    def get_by_name(self, name: str) -> FakePlayer | None:
        fp_id = self.name_index.get(str(name or "").strip().lower())
        return self.bots.get(fp_id) if fp_id else None

    def bots_for_owner(self, owner_uuid: str, owner_name: str) -> list[FakePlayer]:
        owner_uuid = str(owner_uuid or "")
        owner_name = str(owner_name or "")
        rows = []
        for fp in self.bots.values():
            if owner_uuid and fp.owner_uuid == owner_uuid:
                rows.append(fp)
            elif not fp.owner_uuid and fp.owner_name.lower() == owner_name.lower():
                rows.append(fp)
        rows.sort(key=lambda x: x.name.lower())
        return rows

    def can_manage(self, sender: Any, fp: FakePlayer) -> bool:
        if self._plugin.is_admin(sender):
            return True
        sender_uuid = str(getattr(sender, "unique_id", "") or "")
        sender_name = str(getattr(sender, "name", "") or "")
        if fp.owner_uuid:
            return bool(sender_uuid and sender_uuid == fp.owner_uuid)
        return bool(sender_name and sender_name.lower() == fp.owner_name.lower())

    def _player_identity(self, sender: Any) -> tuple[str, str, str]:
        name = str(getattr(sender, "name", "") or "")
        uuid = str(getattr(sender, "unique_id", "") or "")
        key = self._settings.remember_player(uuid, name)
        return key, uuid, name

    def _online_player_named(self, name: str) -> Any | None:
        wanted_raw = str(name or "")
        try:
            direct = self._plugin.server.get_player(wanted_raw)
            if direct is not None:
                return direct
        except Exception:
            pass
        wanted = wanted_raw.lower()
        for player in self._plugin.server.online_players:
            try:
                if str(player.name).lower() == wanted:
                    return player
            except Exception:
                continue
        return None

    def inventory_session(self, fp: FakePlayer) -> dict[str, Any] | None:
        return self.inventory_journal.get_for_bot(fp.id)

    def inventory_locked(self, fp: FakePlayer) -> bool:
        return self.inventory_session(fp) is not None

    def inventory_session_for_player(self, player: Any, fp: FakePlayer) -> dict[str, Any] | None:
        session = self.inventory_session(fp)
        if session is None or not self._session_matches_player(session, player):
            return None
        return session

    def active_inventory_sessions(self) -> int:
        return len(self.inventory_journal.sessions)

    def _session_matches_player(self, session: dict[str, Any], player: Any) -> bool:
        uuid = str(getattr(player, "unique_id", "") or "")
        name = str(getattr(player, "name", "") or "")
        expected_uuid = str(session.get("player_uuid", "") or "")
        if expected_uuid:
            return bool(uuid and uuid == expected_uuid)
        return bool(name and name.lower() == str(session.get("player_name", "")).lower())

    def _notify_session_player(self, session: dict[str, Any], message: str, *, error: bool = False) -> None:
        player = self._online_player_named(str(session.get("player_name", "")))
        if player is None or not self._session_matches_player(session, player):
            return
        try:
            player.send_message(("§c" if error else "§a") + message)
        except Exception:
            pass

    def _put_inventory_session(self, fp: FakePlayer, session: dict[str, Any], state: str) -> None:
        session["state"] = state
        session["updated_at"] = time.time()
        self.inventory_journal.put(fp.id, session)

    def begin_inventory_edit(self, sender: Any, fp: FakePlayer) -> tuple[bool, str]:
        if not self.can_manage(sender, fp):
            return False, "你没有权限管理该假人。"
        if not self.inventory_journal.available:
            return False, "背包事务日志不可用，为防止刷物已禁用整理功能。"
        if not self._bridge.active:
            return False, "行为包桥接未就绪。"
        if not hasattr(sender, "inventory"):
            return False, "该操作只能由在线玩家执行。"
        if self.inventory_session(fp) is not None:
            return False, "该假人的背包已经处于整理事务中。"
        _, player_uuid, player_name = self._player_identity(sender)
        if self.inventory_journal.get_for_player(player_uuid, player_name) is not None:
            return False, "你已经有一个未完成的假人背包整理事务。"

        bot_player = self._online_player_named(fp.name)
        if bot_player is None or not hasattr(bot_player, "inventory"):
            return False, "假人当前不在线，无法安全读取背包。"

        session: dict[str, Any] | None = None
        remove_sent = False
        player_touched = False
        try:
            player_backup = snapshot_inventory(sender.inventory)
            bot_backup = snapshot_inventory(bot_player.inventory)
            if int(player_backup["size"]) != int(bot_backup["size"]):
                return False, "玩家与假人的背包槽位数量不一致，已拒绝整理。"

            session = {
                "bot_id": fp.id,
                "bot_name": fp.name,
                "player_uuid": player_uuid,
                "player_name": player_name,
                "state": "PREPARED",
                "created_at": time.time(),
                "updated_at": time.time(),
                "player_backup": player_backup,
                "player_digest": snapshot_digest(player_backup),
                "bot_backup": bot_backup,
                "bot_digest": snapshot_digest(bot_backup),
            }
            self.inventory_journal.put(fp.id, session)

            empty_inventory(bot_player.inventory)
            if not bot_player.inventory.is_empty:
                raise RuntimeError("failed to clear bot inventory")
            self._put_inventory_session(fp, session, "BOT_CLEARED")

            remove_sent = self._bridge.send_bridge("remove", {"n": fp.name})
            if not remove_sent:
                # Nothing has touched the player's inventory, so this rollback
                # cannot duplicate the bot's items.
                restore_inventory(bot_player.inventory, bot_backup)
                if inventory_digest(bot_player.inventory) != session["bot_digest"]:
                    raise RuntimeError("bot rollback verification failed after remove dispatch failure")
                self.inventory_journal.remove(fp.id)
                return False, "无法锁定假人，背包已原样恢复。"

            # The bot is already empty before its disconnect request is sent.
            # From this point the only live copy of the bot inventory is handed
            # to the player; the journal copy is recovery data, never gameplay state.
            player_touched = True
            restore_inventory(sender.inventory, bot_backup)
            if inventory_digest(sender.inventory) != session["bot_digest"]:
                raise RuntimeError("borrowed inventory verification failed")
            self._put_inventory_session(fp, session, "BORROWED")
            fp.sim_spawn_confirmed = False
            fp.sim_has_position = False
            fp.sim_last_seen_at = 0.0
            return True, (
                f"{fp.name} 已锁定并正在下线。现在你的背包就是假人背包；"
                "整理完成并选中希望假人手持的热栏槽后，点击“完成背包整理”。"
            )
        except Exception as exc:
            self._logger.error(f"开始背包整理失败 [{fp.name}]: {exc}")
            if session is None:
                return False, "无法创建背包事务，未修改任何背包。"

            rollback_kind = "ROLLBACK_FULL" if player_touched else "ROLLBACK_BOT_ONLY"
            # If the remove request was accepted, wait for the removal callback
            # before respawning; otherwise the callback could disconnect a bot
            # after its inventory had already been restored.
            bot_online = self._online_player_named(fp.name) is not None
            if remove_sent and bot_online:
                self._put_inventory_session(fp, session, rollback_kind + "_WAIT_REMOVE")
            else:
                self._put_inventory_session(fp, session, rollback_kind + "_WAIT_SPAWN")
                if self._bridge.active:
                    self.spawn(fp, force=True)
            return False, "进入背包整理模式失败；事务已锁定并正在安全回滚。"

    def on_bot_removed(self, name: str) -> None:
        fp = self.get_by_name(name)
        if fp is None:
            return
        fp.sim_spawn_confirmed = False
        fp.sim_has_position = False
        fp.sim_last_seen_at = 0.0

        session = self.inventory_session(fp)
        if session is None:
            return
        state = str(session.get("state", ""))
        if state == "ROLLBACK_BOT_ONLY_WAIT_REMOVE":
            self._put_inventory_session(fp, session, "ROLLBACK_BOT_ONLY_WAIT_SPAWN")
            if self._bridge.active:
                self.spawn(fp, force=True)
        elif state == "ROLLBACK_FULL_WAIT_REMOVE":
            self._put_inventory_session(fp, session, "ROLLBACK_FULL_WAIT_SPAWN")
            if self._bridge.active:
                self.spawn(fp, force=True)


    def finish_inventory_edit(self, sender: Any, fp: FakePlayer) -> tuple[bool, str]:
        session = self.inventory_session(fp)
        if session is None:
            return False, "该假人没有正在进行的背包整理事务。"
        if not self._session_matches_player(session, sender):
            return False, "只有当前整理事务的玩家可以完成归还。"
        if str(session.get("state")) != "BORROWED":
            return False, f"背包事务当前状态为 {session.get('state')}，暂时不能完成。"
        if not self._bridge.active:
            return False, "行为包桥接未就绪；背包仍由你保管，事务保持锁定。"
        if self._online_player_named(fp.name) is not None:
            return False, "假人异常处于在线状态；为防止物品重复，已拒绝归还。"

        self._put_inventory_session(fp, session, "WAIT_SPAWN_FOR_RETURN")
        if not self.spawn(fp, force=True):
            self._put_inventory_session(fp, session, "BORROWED")
            return False, "无法生成用于接收背包的空假人；你的背包未被修改。"
        return True, f"正在生成空的 {fp.name} 接收背包，完成后会自动恢复你原来的背包。"

    def _complete_spawned_inventory_session(self, fp: FakePlayer) -> None:
        session = self.inventory_session(fp)
        if session is None:
            return
        state = str(session.get("state", ""))
        bot_player = self._online_player_named(fp.name)
        if bot_player is None or not hasattr(bot_player, "inventory"):
            return

        if state == "ROLLBACK_BOT_ONLY_WAIT_SPAWN":
            try:
                if not bot_player.inventory.is_empty:
                    raise RuntimeError("rollback target bot inventory is not empty")
                restore_inventory(bot_player.inventory, session["bot_backup"])
                if inventory_digest(bot_player.inventory) != str(session.get("bot_digest")):
                    raise RuntimeError("rollback bot inventory verification failed")
                self.inventory_journal.remove(fp.id)
                self._notify_session_player(session, f"{fp.name} 的原背包已恢复；本次整理已取消。")
            except Exception as exc:
                self._logger.error(f"恢复假人原背包失败 [{fp.name}]，事务保持锁定: {exc}")
            return

        if state == "ROLLBACK_FULL_WAIT_SPAWN":
            player = self._online_player_named(str(session.get("player_name", "")))
            if player is None or not self._session_matches_player(session, player):
                self._bridge.send_bridge("remove", {"n": fp.name})
                self._put_inventory_session(fp, session, "ROLLBACK_FULL_WAIT_PLAYER")
                return
            try:
                if not bot_player.inventory.is_empty:
                    raise RuntimeError("full rollback target bot inventory is not empty")

                # Remove any partially delivered bot inventory from the player
                # before materializing the bot backup again.
                empty_inventory(player.inventory)
                if not player.inventory.is_empty:
                    raise RuntimeError("failed to clear player during full rollback")

                restore_inventory(bot_player.inventory, session["bot_backup"])
                if inventory_digest(bot_player.inventory) != str(session.get("bot_digest")):
                    raise RuntimeError("full rollback bot verification failed")

                restore_inventory(player.inventory, session["player_backup"])
                if inventory_digest(player.inventory) != str(session.get("player_digest")):
                    raise RuntimeError("full rollback player verification failed")

                self.inventory_journal.remove(fp.id)
                self._notify_session_player(session, f"{fp.name} 与你的原背包均已恢复；本次整理已取消。")
            except Exception as exc:
                self._logger.error(f"完整回滚背包事务失败 [{fp.name}]，事务保持锁定: {exc}")
            return

        if state == "PLAYER_CLEARED":
            try:
                if not bot_player.inventory.is_empty:
                    raise RuntimeError("recovery target bot inventory is not empty")
                edited_bot = session.get("edited_bot")
                if not isinstance(edited_bot, dict):
                    raise RuntimeError("missing edited bot snapshot")
                restore_inventory(bot_player.inventory, edited_bot)
                if inventory_digest(bot_player.inventory) != str(session.get("edited_bot_digest")):
                    raise RuntimeError("recovered bot inventory verification failed")
                self._put_inventory_session(fp, session, "BOT_RESTORED")

                player = self._online_player_named(str(session.get("player_name", "")))
                if player is not None and self._session_matches_player(session, player):
                    if not player.inventory.is_empty:
                        raise RuntimeError("player inventory is not empty during recovery")
                    restore_inventory(player.inventory, session["player_backup"])
                    if inventory_digest(player.inventory) != str(session.get("player_digest")):
                        raise RuntimeError("recovered player inventory verification failed")
                    self.inventory_journal.remove(fp.id)
                    self._notify_session_player(
                        session,
                        f"{fp.name} 的整理结果和你的原背包均已从事务日志恢复。",
                    )
            except Exception as exc:
                self._logger.error(f"恢复中断的背包归还失败 [{fp.name}]，事务保持锁定: {exc}")
            return

        if state != "WAIT_SPAWN_FOR_RETURN":
            return

        player = self._online_player_named(str(session.get("player_name", "")))
        if player is None or not self._session_matches_player(session, player):
            # Keep no live copy on the bot while the borrower is offline.
            self._bridge.send_bridge("remove", {"n": fp.name})
            self._put_inventory_session(fp, session, "BORROWED")
            return

        try:
            if not bot_player.inventory.is_empty:
                raise RuntimeError("return target bot inventory is not empty")

            edited_bot = snapshot_inventory(player.inventory)
            session["edited_bot"] = edited_bot
            session["edited_bot_digest"] = snapshot_digest(edited_bot)
            self._put_inventory_session(fp, session, "RETURNING_PREPARED")

            empty_inventory(player.inventory)
            if not player.inventory.is_empty:
                raise RuntimeError("failed to clear borrower inventory")
            self._put_inventory_session(fp, session, "PLAYER_CLEARED")

            restore_inventory(bot_player.inventory, edited_bot)
            if inventory_digest(bot_player.inventory) != str(session.get("edited_bot_digest")):
                raise RuntimeError("returned bot inventory verification failed")
            self._put_inventory_session(fp, session, "BOT_RESTORED")

            restore_inventory(player.inventory, session["player_backup"])
            if inventory_digest(player.inventory) != str(session.get("player_digest")):
                raise RuntimeError("player inventory restoration verification failed")

            self.inventory_journal.remove(fp.id)
            self._notify_session_player(
                session,
                f"{fp.name} 的背包已归还，你原来的背包也已完整恢复。当前热栏选择已同步为假人的主手槽。",
            )
        except Exception as exc:
            self._logger.error(f"完成背包整理失败 [{fp.name}]，事务保持锁定: {exc}")
            self._notify_session_player(
                session,
                "归还过程中发生异常；系统已保持事务锁，禁止继续操作该假人，请不要丢弃当前物品并联系管理员。",
                error=True,
            )

    def on_bot_spawned_for_inventory(self, name: str) -> None:
        fp = self.get_by_name(name)
        if fp is None:
            return
        self._complete_spawned_inventory_session(fp)

    def recover_inventory_for_player(self, player: Any) -> None:
        uuid = str(getattr(player, "unique_id", "") or "")
        name = str(getattr(player, "name", "") or "")
        found = self.inventory_journal.get_for_player(uuid, name)
        if found is None:
            return
        bot_id, session = found
        fp = self.bots.get(bot_id)
        if fp is None:
            self._logger.error(f"背包事务引用不存在的假人 id={bot_id}，保持 fail-close。")
            player.send_message("§c检测到损坏的假人背包事务，请联系管理员，不要移动当前背包物品。")
            return

        state = str(session.get("state", ""))
        try:
            current_digest = inventory_digest(player.inventory)
        except Exception as exc:
            self._logger.error(f"无法读取玩家背包以恢复事务 [{name}]: {exc}")
            return

        if state in {"PREPARED", "BOT_CLEARED"}:
            if current_digest == str(session.get("bot_digest")):
                self._put_inventory_session(fp, session, "BORROWED")
                player.send_message(f"§e恢复了 {fp.name} 的未完成背包整理事务；整理完成后请执行归还。")
                return
            if current_digest == str(session.get("player_digest")):
                self._put_inventory_session(fp, session, "ROLLBACK_BOT_ONLY_WAIT_SPAWN")
                if self._bridge.active:
                    self.spawn(fp, force=True)
                player.send_message(f"§e上次 {fp.name} 的整理尚未开始，正在恢复假人原背包。")
                return
            player.send_message("§c背包内容与事务快照均不匹配，已 fail-close；请联系管理员。")
            self._logger.error(f"背包事务恢复歧义 [{fp.name}/{name}] state={state}")
            return

        if state == "BORROWED":
            # The bot must stay offline while its inventory is borrowed.
            if self._online_player_named(fp.name) is not None and self._bridge.active:
                self._bridge.send_bridge("remove", {"n": fp.name})
            player.send_message(f"§e你仍在整理 {fp.name} 的背包；完成后请点击“完成背包整理”。")
            return

        if state in {"ROLLBACK_BOT_ONLY_WAIT_SPAWN", "ROLLBACK_FULL_WAIT_SPAWN"}:
            if self._bridge.active:
                self.spawn(fp, force=True)
            return

        if state == "ROLLBACK_FULL_WAIT_PLAYER":
            self._put_inventory_session(fp, session, "ROLLBACK_FULL_WAIT_SPAWN")
            if self._bridge.active:
                self.spawn(fp, force=True)
            return

        if state == "BOT_RESTORED":
            if not player.inventory.is_empty:
                player.send_message("§c恢复玩家原背包前检测到当前背包非空，已 fail-close，请联系管理员。")
                return
            try:
                restore_inventory(player.inventory, session["player_backup"])
                if inventory_digest(player.inventory) != str(session.get("player_digest")):
                    raise RuntimeError("player recovery verification failed")
                self.inventory_journal.remove(fp.id)
                player.send_message(f"§a已恢复你在整理 {fp.name} 前的原背包。")
            except Exception as exc:
                self._logger.error(f"恢复玩家原背包失败 [{name}]: {exc}")
            return

        if state in {"WAIT_SPAWN_FOR_RETURN", "RETURNING_PREPARED", "PLAYER_CLEARED"}:
            if state == "RETURNING_PREPARED":
                if current_digest == str(session.get("edited_bot_digest")):
                    self._put_inventory_session(fp, session, "WAIT_SPAWN_FOR_RETURN")
                elif player.inventory.is_empty:
                    self._put_inventory_session(fp, session, "PLAYER_CLEARED")
                else:
                    player.send_message("§c归还事务的玩家背包状态无法判定，已 fail-close。")
                    return
            elif state == "PLAYER_CLEARED" and not player.inventory.is_empty:
                player.send_message("§c事务记录要求玩家背包为空，但检测到物品，已 fail-close。")
                return
            if self._bridge.active:
                self.spawn(fp, force=True)

    def recover_online_inventory_sessions(self) -> None:
        for session in list(self.inventory_journal.sessions.values()):
            player = self._online_player_named(str(session.get("player_name", "")))
            if player is not None and self._session_matches_player(session, player):
                self.recover_inventory_for_player(player)


    @staticmethod
    def _view_direction(location: Any) -> tuple[float, float, float] | None:
        try:
            direction = location.direction
            x = float(direction.x)
            y = float(direction.y)
            z = float(direction.z)
            length = math.sqrt(x * x + y * y + z * z)
            if not math.isfinite(length) or length <= 1e-6:
                return None
            return (x / length, y / length, z / length)
        except Exception:
            return None

    @staticmethod
    def _pose_payload(fp: FakePlayer) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "n": fp.name,
            "x": fp.location_x,
            "y": fp.location_y,
            "z": fp.location_z,
            "d": fp.dimension,
            "pitch": fp.pitch,
            "yaw": fp.yaw,
        }
        length_sq = fp.view_x * fp.view_x + fp.view_y * fp.view_y + fp.view_z * fp.view_z
        if length_sq > 1e-8:
            payload["dx"] = fp.view_x
            payload["dy"] = fp.view_y
            payload["dz"] = fp.view_z
        return payload

    def _capture_player_pose(self, sender: Any, fp: FakePlayer) -> bool:
        loc = getattr(sender, "location", None)
        if loc is None:
            return False
        fp.location_x = round(float(loc.x), 2)
        fp.location_y = round(float(loc.y), 2)
        fp.location_z = round(float(loc.z), 2)
        fp.dimension = self._plugin.dimension_id(loc.dimension)
        fp.pitch = round(float(getattr(loc, "pitch", 0.0)), 2)
        fp.yaw = round(float(getattr(loc, "yaw", 0.0)), 2)
        direction = self._view_direction(loc)
        if direction is not None:
            fp.view_x, fp.view_y, fp.view_z = direction
        fp.sim_has_position = False
        return True

    def spawn_limit_error(self, sender: Any) -> str | None:
        if self._plugin.is_admin(sender):
            return None
        key, uuid, name = self._player_identity(sender)
        limits = self._settings.effective_for_key(key)
        owned = len(self.bots_for_owner(uuid, name))
        if not limits.unlimited and owned >= limits.max_bots:
            return f"你最多只能拥有 {limits.max_bots} 个假人。"
        if not limits.bypass_global_limit and len(self.bots) >= self._settings.max_total:
            return f"全服假人数量已达到上限 {self._settings.max_total}。"
        last = self._spawn_cooldowns.get(key, -999.0)
        remaining = limits.cooldown_seconds - (time.monotonic() - last)
        if remaining > 0:
            return f"创建过于频繁，请等待 {remaining:.1f} 秒。"
        return None

    def create_for_player(self, sender: Any, name: str) -> tuple[bool, str]:
        if not self._bridge.active:
            return False, "行为包桥接未就绪，暂时不能创建假人。"
        location = getattr(sender, "location", None)
        if location is None:
            return False, "该命令需要玩家位置。控制台请使用 createat。"
        limit_error = self.spawn_limit_error(sender)
        if limit_error:
            return False, limit_error
        online_names = {str(p.name) for p in self._plugin.server.online_players}
        error = validate_name(name, set(self.name_index.keys()), online_names)
        if error:
            return False, error
        owner_name = str(getattr(sender, "name", "") or "")
        owner_uuid = str(getattr(sender, "unique_id", "") or "")
        key = self._settings.remember_player(owner_uuid, owner_name)
        direction = self._view_direction(location)
        fp = FakePlayer(
            id=generate_id(),
            name=name.strip(),
            owner_name=owner_name,
            owner_uuid=owner_uuid,
            location_x=round(float(location.x), 2),
            location_y=round(float(location.y), 2),
            location_z=round(float(location.z), 2),
            dimension=self._plugin.dimension_id(location.dimension),
            pitch=round(float(getattr(location, "pitch", 0.0)), 2),
            yaw=round(float(getattr(location, "yaw", 0.0)), 2),
            created=format_date_time_beijing(),
            view_x=direction[0] if direction else 0.0,
            view_y=direction[1] if direction else 0.0,
            view_z=direction[2] if direction else 0.0,
        )
        self.bots[fp.id] = fp
        self.name_index[fp.name.lower()] = fp.id
        self._spawn_cooldowns[key] = time.monotonic()
        self.save()
        self.spawn(fp, force=True)
        return True, f"已创建假人 {fp.name}。"

    def create_at(
        self,
        name: str,
        owner_name: str,
        owner_uuid: str,
        x: float,
        y: float,
        z: float,
        dimension: str,
    ) -> tuple[bool, str]:
        if not self._bridge.active:
            return False, "行为包桥接未就绪。"
        online_names = {str(p.name) for p in self._plugin.server.online_players}
        error = validate_name(name, set(self.name_index.keys()), online_names)
        if error:
            return False, error
        fp = FakePlayer(
            id=generate_id(), name=name.strip(), owner_name=owner_name, owner_uuid=owner_uuid,
            location_x=round(float(x), 2), location_y=round(float(y), 2), location_z=round(float(z), 2),
            dimension=dimension, pitch=0.0, yaw=0.0, created=format_date_time_beijing(),
        )
        self.bots[fp.id] = fp
        self.name_index[fp.name.lower()] = fp.id
        self.save()
        self.spawn(fp, force=True)
        return True, f"已在 {dimension} ({x:.1f}, {y:.1f}, {z:.1f}) 创建假人 {fp.name}。"

    def spawn(self, fp: FakePlayer, force: bool = False) -> bool:
        if not self._bridge.active:
            return False
        now = time.monotonic()
        last = self._last_spawn_request.get(fp.id, -999.0)
        if not force and now - last < 10.0:
            return False
        self._last_spawn_request[fp.id] = now
        return self._bridge.send_bridge("spawn", self._pose_payload(fp))

    def ensure_all_spawned(self) -> None:
        if not self._bridge.active:
            return
        for fp in list(self.bots.values()):
            if self.inventory_locked(fp):
                continue
            if fp.sim_spawn_confirmed or fp.is_recently_seen():
                continue
            self.spawn(fp)

    def reconcile_names(self, names: set[str]) -> None:
        known = {fp.name.lower(): fp for fp in self.bots.values()}
        remote = {name.lower(): name for name in names}
        for lower, fp in known.items():
            if lower in remote:
                fp.sim_spawn_confirmed = True
            elif not fp.is_recently_seen():
                fp.sim_spawn_confirmed = False
        for lower, remote_name in remote.items():
            if lower not in known:
                self._logger.warning(f"移除行为包中的未登记假人: {remote_name}")
                self._bridge.send_bridge("remove", {"n": remote_name})

    def mark_spawned(self, name: str, ok: bool) -> None:
        fp = self.get_by_name(name)
        if fp is not None:
            fp.sim_spawn_confirmed = bool(ok)
            if ok and self.inventory_locked(fp):
                self.on_bot_spawned_for_inventory(name)

    def update_position(self, name: str, x: Any, y: Any, z: Any, dimension: str) -> None:
        fp = self.get_by_name(name)
        if fp is None:
            return
        try:
            nx, ny, nz = float(x), float(y), float(z)
        except (TypeError, ValueError):
            return
        dimension = str(dimension or fp.dimension).replace("minecraft:", "")
        fp.mark_seen(nx, ny, nz, dimension)

    def guard_positions(self) -> None:
        if not self._bridge.active or not self._settings.guard_enabled:
            return
        threshold_sq = self._settings.guard_distance ** 2
        for fp in list(self.bots.values()):
            if self.inventory_locked(fp):
                continue
            if not fp.sim_has_position or not fp.is_recently_seen():
                continue
            dx = fp.sim_actual_x - fp.location_x
            dy = fp.sim_actual_y - fp.location_y
            dz = fp.sim_actual_z - fp.location_z
            if dx * dx + dy * dy + dz * dz > threshold_sq:
                self._bridge.send_bridge("teleport", self._pose_payload(fp))

    def move_here(self, sender: Any, fp: FakePlayer) -> tuple[bool, str]:
        if not self.can_manage(sender, fp):
            return False, "你没有权限管理该假人。"
        if self.inventory_locked(fp):
            return False, "假人背包正在整理，当前位置和行为已锁定。"
        if not self._bridge.active:
            return False, "行为包桥接未就绪。"
        if not self._capture_player_pose(sender, fp):
            return False, "该操作需要玩家位置。"
        self.save()
        self._bridge.send_bridge("teleport", self._pose_payload(fp))
        return True, f"已将 {fp.name} 移到你的位置并同步视角。"

    def throw_trident_here(self, sender: Any, fp: FakePlayer) -> tuple[bool, str]:
        if not self.can_manage(sender, fp):
            return False, "你没有权限管理该假人。"
        if self.inventory_locked(fp):
            return False, "假人背包正在整理，不能执行三叉戟操作。"
        if not self._bridge.active:
            return False, "行为包桥接未就绪。"

        # Throwing must never move the bot onto the operator. The saved anchor
        # and view direction are updated only by "move here".
        payload = self._pose_payload(fp)
        payload["r"] = str(getattr(sender, "name", "") or "")
        ok = self._bridge.send_bridge("trident", payload)
        if not ok:
            return False, "三叉戟投掷指令发送失败。"
        return True, f"已请求 {fp.name} 按已保存的位置和视角投掷背包中的三叉戟。"

    def teleport_to(self, fp: FakePlayer, x: float, y: float, z: float, dimension: str) -> tuple[bool, str]:
        if self.inventory_locked(fp):
            return False, "假人背包正在整理，不能移动。"
        if not self._bridge.active:
            return False, "行为包桥接未就绪。"
        fp.location_x = round(float(x), 2)
        fp.location_y = round(float(y), 2)
        fp.location_z = round(float(z), 2)
        fp.dimension = dimension
        fp.sim_has_position = False
        self.save()
        self._bridge.send_bridge("teleport", self._pose_payload(fp))
        return True, f"已移动 {fp.name}。"

    def remove(self, sender: Any, fp: FakePlayer) -> tuple[bool, str]:
        if not self.can_manage(sender, fp):
            return False, "你没有权限管理该假人。"
        if self.inventory_locked(fp):
            return False, "假人背包正在整理，不能删除。"
        self._bridge.send_bridge("remove", {"n": fp.name})
        self.bots.pop(fp.id, None)
        self.name_index.pop(fp.name.lower(), None)
        self._last_spawn_request.pop(fp.id, None)
        self.save()
        return True, f"已删除假人 {fp.name}。"

    def remove_by_admin(self, fp: FakePlayer) -> None:
        if self.inventory_locked(fp):
            self._logger.warning(f"拒绝删除背包事务中的假人: {fp.name}")
            return
        self._bridge.send_bridge("remove", {"n": fp.name})
        self.bots.pop(fp.id, None)
        self.name_index.pop(fp.name.lower(), None)
        self._last_spawn_request.pop(fp.id, None)
        self.save()

    def clear_remote(self) -> None:
        if self._bridge.active:
            self._bridge.send_bridge("clear", {})
        for fp in self.bots.values():
            fp.sim_spawn_confirmed = False
            fp.sim_has_position = False
            fp.sim_last_seen_at = 0.0

    def status_text(self, fp: FakePlayer) -> str:
        session = self.inventory_session(fp)
        if session is not None:
            state = str(session.get("state", ""))
            if state == "BORROWED":
                return "背包整理中"
            return "背包事务处理中"
        if not self._bridge.active:
            return "桥接离线"
        if fp.is_recently_seen() or fp.sim_spawn_confirmed:
            return "在线"
        return "等待生成"
