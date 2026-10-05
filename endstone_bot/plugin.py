import json
import shutil
from pathlib import Path
from typing import Any

from endstone.command import Command, CommandSender

try:
    from endstone.command import CommandSenderWrapper
except ImportError:  # Compatibility with older Endstone 0.11 builds.
    CommandSenderWrapper = None
from endstone.event import PlayerJoinEvent, ScriptMessageEvent, event_handler
from endstone.plugin import Plugin

from endstone_bot.beta_script import write_patch_script
from endstone_bot.bridge import BRIDGE_PROTOCOL, BridgeManager
from endstone_bot.gui import BotGUI
from endstone_bot.inventory_sessions import InventorySessionStore
from endstone_bot.manager import FakeBotManager
from endstone_bot.settings import SettingsManager


class BotPlugin(Plugin):
    api_version = "0.11"
    description = "轻量 SimulatedPlayer 假人：挂机、定点三叉戟、玩家限额、GUI 与控制台管理。"

    commands = {
        "botbridge": {
            "description": "EndstoneBot internal behavior-pack callback.",
            "usages": ["/botbridge <event: str> <payload: str>"],
        },
        "bot": {
            "description": "管理 SimulatedPlayer 假人。",
            "usages": [
                "/bot",
                "/bot (gui|list|status|admin|removeall)<action: BotSimpleAction>",
                "/bot (spawn|remove|tp|trident|inventory)<action: BotNamedAction> <name: str>",
                "/bot (createat)<action: BotCreateAtAction> <name: str> <owner: str> <x: float> <y: float> <z: float> (overworld|nether|the_end)<dimension: BotDimension>",
                "/bot (moveat)<action: BotMoveAtAction> <name: str> <x: float> <y: float> <z: float> (overworld|nether|the_end)<dimension: BotMoveDimension>",
                "/bot (limit)<action: BotLimitAction> <player: str> (show|unlimited|default)<mode: BotLimitSimpleMode>",
                "/bot (limit)<action: BotLimitNumberAction> <player: str> (max|cooldown)<mode: BotLimitNumberMode> <value: int>",
                "/bot (limit)<action: BotLimitBoolAction> <player: str> (bypassglobal)<mode: BotLimitBoolMode> <enabled: bool>",
                "/bot (config)<action: BotConfigAction> (show|reload)<mode: BotConfigSimpleMode>",
                "/bot (config)<action: BotConfigNumberAction> (maxtotal|maxperplayer|cooldown)<key: BotConfigNumberKey> <value: int>",
            ],
            "permissions": ["endstone_bot.command"],
        },
    }

    permissions = {
        "endstone_bot.command": {
            "description": "允许使用假人系统；管理员子命令仍会额外检查 OP。",
            "default": True,
        }
    }

    BEHAVIOR_PACK_UUID = "a3f7c2e1-8b4d-4f6a-9c3e-1d2b3c4d5e6f"
    BEHAVIOR_PACK_VERSION = [4, 3, 0]

    def on_load(self) -> None:
        self.data_folder.mkdir(parents=True, exist_ok=True)
        self.settings = SettingsManager(self.data_folder, self.logger)

    def on_enable(self) -> None:
        self.data_folder.mkdir(parents=True, exist_ok=True)
        if not hasattr(self, "settings"):
            self.settings = SettingsManager(self.data_folder, self.logger)
        self._bridge_command_sender = self.server.command_sender
        if CommandSenderWrapper is not None:
            self._bridge_command_sender = CommandSenderWrapper(
                self.server.command_sender,
                on_message=lambda _message: None,
                on_error=self._on_bridge_command_error,
            )
        self.bridge = BridgeManager(self.logger, self._dispatch)
        self.inventory_sessions = InventorySessionStore(self.data_folder, self.logger)
        self.manager = FakeBotManager(
            self, self.data_folder, self.bridge, self.settings, self.logger
        )
        self.gui = BotGUI(self)
        self._tick_counter = 0
        self._list_names: set[str] = set()
        self._bridge_warning_sent = False
        self.register_events(self)

        pack_state = self._setup_behavior_pack()
        self._write_beta_patch_script()
        self.manager.restore()

        scheduler = self.server.scheduler
        scheduler.run_task(self, self._tick, delay=1, period=1)
        scheduler.run_task(self, self._bridge_poll, delay=20, period=100)
        scheduler.run_task(self, self.manager.ensure_all_spawned, delay=60, period=100)

        self.bridge.hello()
        self.logger.info(
            f"Bot 已启用：SimulatedPlayer only，bridge protocol={BRIDGE_PROTOCOL}，"
            f"默认每人 {self.settings.max_per_player} 个，全服 {self.settings.max_total} 个。"
        )
        if pack_state in {"installed", "updated"}:
            self.logger.warning("行为包已写入当前世界；首次安装或更新后需要重启服务器。")

    def _write_beta_patch_script(self) -> None:
        world_dir = self._find_world_dir()
        if world_dir is None:
            self.logger.warning(
                "无法精确定位当前世界；未生成 Beta APIs 离线补丁脚本。"
            )
            return
        try:
            self._beta_patch_script_path = write_patch_script(
                self.data_folder,
                world_dir,
                backup_keep=5,
            )
        except Exception as exc:
            self.logger.warning(f"生成 Beta APIs 离线补丁脚本失败: {exc}")

    def on_disable(self) -> None:
        if hasattr(self, "manager"):
            self.manager.save()
        if hasattr(self, "bridge"):
            self.bridge.shutdown()

    def _tick(self) -> None:
        self._tick_counter += 1
        interval = self.settings.guard_interval_ticks
        if self._tick_counter % interval == 0:
            self.manager.guard_positions()

    def _bridge_poll(self) -> None:
        self.bridge.mark_stale_if_needed()
        if self.bridge.active:
            self._bridge_warning_sent = False
            return

        # While disconnected, retry the authenticated handshake. Once connected,
        # the behavior pack's heartbeat is the sole liveness signal so the server
        # log is not flooded by /scriptevent bot:ping every five seconds.
        self.bridge.hello()
        if not self._bridge_warning_sent and self._tick_counter >= 100:
            self.logger.warning(
                "行为包桥接尚未建立：若世界未启用 Beta APIs，请停服后执行 plugins/bot/enable_beta.py，"
                "再完整启动服务器。"
            )
            self._bridge_warning_sent = True

    @event_handler
    def on_player_join(self, event: PlayerJoinEvent) -> None:
        player = event.player
        name = str(player.name)
        uuid = str(getattr(player, "unique_id", "") or "")
        self.settings.remember_player(uuid, name)
        changed = False
        for fp in self.manager.bots.values():
            if not fp.owner_uuid and fp.owner_name.lower() == name.lower() and uuid:
                fp.owner_uuid = uuid
                fp.owner_name = name
                changed = True
        if changed:
            self.manager.save()
        self._maybe_recover_inventory_for_player(player)

    @event_handler
    def on_script_message(self, event: ScriptMessageEvent) -> None:
        parsed = self.bridge.handle_script_message(event)
        if parsed is not None:
            self._handle_bridge_message(parsed)

    def _handle_bridge_message(self, parsed: dict[str, Any]) -> None:
        msg_id = parsed["id"]
        data = parsed["data"]

        if msg_id == "bot:hello_ack":
            self._list_names.clear()
            self.bridge.request_list()
            return
        if msg_id in ("bot:pong", "bot:heartbeat"):
            return
        if msg_id == "bot:spawned":
            name = str(data.get("n", ""))
            ok = bool(data.get("ok", False))
            self.manager.mark_spawned(name, ok)
            if ok and not data.get("existed") and not data.get("respawned"):
                self.logger.info(f"假人 {name} 已生成。")
            if ok:
                self._maybe_recover_inventory_for_bot(name)
            return
        if msg_id == "bot:inventory_swapped":
            self._handle_inventory_swapped(data)
            return
        if msg_id == "bot:inventory_committed":
            self._handle_inventory_committed(data)
            return
        if msg_id == "bot:inventory_recovery_stored":
            self._handle_inventory_recovery_stored(data)
            return
        if msg_id == "bot:inventory_recovery_finalized":
            self._handle_inventory_recovery_finalized(data)
            return
        if msg_id == "bot:inventory_error":
            self._handle_inventory_error(data)
            return
        if msg_id == "bot:trident_result":
            self._handle_trident_result(data)
            return
        if msg_id == "bot:lost":
            fp = self.manager.get_by_name(str(data.get("n", "")))
            if fp is not None:
                reason = str(data.get("reason", "") or "")
                session = self.inventory_sessions.for_bot_id(fp.id)
                if session is not None:
                    session_id = str(session.get("session_id", ""))
                    if reason == "dead":
                        self.inventory_sessions.set_state(session_id, "manual_review")
                        self.logger.error(
                            f"假人 {fp.name} 在背包托管期间死亡；为避免掉落物与备份同时恢复造成刷物，"
                            "事务已 fail-close 锁定，需要人工检查。"
                        )
                        self._message_inventory_player(
                            session,
                            "§c假人在背包托管期间异常死亡。为防止刷物，事务已锁定，请联系管理员处理。",
                        )
                    elif str(session.get("state", "")) == "editing":
                        self.inventory_sessions.set_state(session_id, "recovery_pending")
                        self._message_inventory_player(
                            session,
                            "§e假人连接中断，背包事务已进入安全恢复状态；请不要移动当前背包物品。",
                        )
                if reason != "dead":
                    fp.sim_spawn_confirmed = False
                fp.sim_has_position = False
                fp.sim_last_seen_at = 0.0
            return
        if msg_id == "bot:positions":
            entries = data.get("p", [])
            if not isinstance(entries, list):
                return
            for item in entries:
                if not isinstance(item, dict):
                    continue
                self.manager.update_position(
                    str(item.get("n", "")),
                    item.get("x"), item.get("y"), item.get("z"),
                    str(item.get("d", "overworld")),
                )
            return
        if msg_id == "bot:list_result":
            if data.get("reset"):
                self._list_names.clear()
            names = data.get("names", [])
            if isinstance(names, list):
                self._list_names.update(str(x) for x in names if isinstance(x, str))
            if data.get("done"):
                self.manager.reconcile_names(set(self._list_names))
                self.manager.ensure_all_spawned()
            return
        if msg_id == "bot:error":
            self.logger.warning(
                f"行为包错误 [{data.get('n', '')}]: {data.get('e', 'unknown error')}"
            )

    def _find_inventory_player(self, session: dict[str, Any]) -> Any | None:
        wanted_uuid = str(session.get("player_uuid", "") or "")
        wanted_name = str(session.get("player_name", "") or "")
        for player in self.server.online_players:
            try:
                uuid = str(getattr(player, "unique_id", "") or "")
                name = str(getattr(player, "name", "") or "")
                if wanted_uuid and uuid == wanted_uuid:
                    return player
                if not wanted_uuid and wanted_name and name.lower() == wanted_name.lower():
                    return player
            except Exception:
                continue
        return None

    def _message_inventory_player(self, session: dict[str, Any], message: str) -> None:
        player = self._find_inventory_player(session)
        if player is not None:
            try:
                player.send_message(message)
            except Exception:
                pass

    def _maybe_recover_inventory_for_player(self, player: Any) -> None:
        session = self.inventory_sessions.for_player(player)
        if session is None or str(session.get("state", "")) != "recovery_pending":
            return
        self._try_inventory_recovery(session)

    def _maybe_recover_inventory_for_bot(self, bot_name: str) -> None:
        session = self.inventory_sessions.for_bot_name(bot_name)
        if session is None or str(session.get("state", "")) != "recovery_pending":
            return
        self._try_inventory_recovery(session)

    def _try_inventory_recovery(self, session: dict[str, Any]) -> None:
        if not self.bridge.active or str(session.get("state", "")) != "recovery_pending":
            return
        player = self._find_inventory_player(session)
        fp = self.manager.get_by_name(str(session.get("bot_name", "")))
        if player is None or fp is None or not fp.sim_spawn_confirmed:
            return
        session_id = str(session.get("session_id", ""))
        self.inventory_sessions.set_state(session_id, "recovery_storing")
        ok = self.bridge.send_bridge(
            "inventory_recover",
            {
                "n": fp.name,
                "r": str(getattr(player, "name", "") or ""),
                "s": session_id,
            },
        )
        if not ok:
            self.inventory_sessions.set_state(session_id, "recovery_pending")

    def _handle_inventory_swapped(self, data: dict[str, Any]) -> None:
        session_id = str(data.get("s", "") or "")
        session = self.inventory_sessions.by_session(session_id)
        if session is None:
            self.logger.error("收到未知背包事务的 inventory_swapped；保持远端锁定。")
            return
        player = self._find_inventory_player(session)
        if player is None:
            self.inventory_sessions.set_state(session_id, "manual_review")
            self.logger.error("背包已交换但玩家已离线；事务 fail-close 锁定。")
            return
        try:
            self.inventory_sessions.mark_swapped(
                session_id,
                player,
                bot_held_slot=int(data.get("bot_slot", 0)),
                bot_game_mode=str(data.get("game_mode", "survival")),
            )
        except Exception as exc:
            # The physical swap succeeded but the second durable snapshot failed.
            # Immediately request the inverse swap. Do not allow editing to start.
            self.logger.error(f"备份假人背包失败，正在回滚物理交换: {exc}")
            self.inventory_sessions.set_state(session_id, "rollback_pending")
            ok = self.bridge.send_bridge(
                "inventory_finish",
                {
                    "n": str(session.get("bot_name", "")),
                    "r": str(session.get("player_name", "")),
                    "s": session_id,
                },
            )
            if not ok:
                self.inventory_sessions.set_state(session_id, "manual_review")
            return

        player.send_message(
            "§a已进入假人背包整理模式。你的原 36 格主背包现在由假人托管；"
            "当前背包就是假人的真实背包。整理完成后再次点击「完成背包整理」或执行同一条 /bot inventory 命令。"
        )

    def _handle_inventory_committed(self, data: dict[str, Any]) -> None:
        session_id = str(data.get("s", "") or "")
        session = self.inventory_sessions.by_session(session_id)
        if session is None:
            return
        player = self._find_inventory_player(session)
        previous_state = str(session.get("state", ""))
        if player is None:
            self.inventory_sessions.set_state(session_id, "manual_review")
            self.logger.error("背包交换已提交但无法验证玩家背包；事务保持锁定。")
            return
        try:
            restored = self.inventory_sessions.verify_player_restored(session_id, player)
        except Exception as exc:
            restored = False
            self.logger.error(f"验证玩家背包恢复失败: {exc}")
        if not restored:
            self.inventory_sessions.set_state(session_id, "manual_review")
            player.send_message(
                "§c背包物理交换已结束，但玩家原背包校验失败。为防止刷物，假人保持锁定，请联系管理员。"
            )
            return

        result = "rolled_back" if previous_state == "rollback_pending" else "completed"
        self.inventory_sessions.complete(session_id, result=result)
        if previous_state == "rollback_pending":
            player.send_message("§e假人背包备份失败，但双方背包已安全回滚；未进入整理模式。")
        else:
            player.send_message(
                "§a背包整理完成：假人已取得整理后的背包，你的原背包已恢复。"
            )

    def _handle_inventory_recovery_stored(self, data: dict[str, Any]) -> None:
        session_id = str(data.get("s", "") or "")
        session = self.inventory_sessions.by_session(session_id)
        if session is None:
            return
        player = self._find_inventory_player(session)
        if player is None:
            self.inventory_sessions.set_state(session_id, "manual_review")
            return
        self.inventory_sessions.set_state(session_id, "recovery_restoring")
        try:
            self.inventory_sessions.restore_player_backup(session_id, player)
        except Exception as exc:
            self.inventory_sessions.set_state(session_id, "manual_review")
            self.logger.error(f"恢复玩家背包失败；事务已锁定: {exc}")
            try:
                player.send_message("§c自动恢复玩家原背包失败；为防止刷物，事务已锁定，请联系管理员。")
            except Exception:
                pass
            return

        ok = self.bridge.send_bridge(
            "inventory_recovery_finalize",
            {
                "n": str(session.get("bot_name", "")),
                "r": str(session.get("player_name", "")),
                "s": session_id,
            },
        )
        if not ok:
            self.inventory_sessions.set_state(session_id, "manual_review")
            self.logger.error("玩家背包已恢复，但远端恢复 finalize 发送失败；保持 fail-close 锁定。")

    def _handle_inventory_recovery_finalized(self, data: dict[str, Any]) -> None:
        session_id = str(data.get("s", "") or "")
        session = self.inventory_sessions.by_session(session_id)
        if session is None:
            return
        player = self._find_inventory_player(session)
        if player is None:
            self.inventory_sessions.set_state(session_id, "manual_review")
            return
        try:
            restored = self.inventory_sessions.verify_player_restored(session_id, player)
        except Exception:
            restored = False
        if not restored:
            self.inventory_sessions.set_state(session_id, "manual_review")
            return
        self.inventory_sessions.complete(session_id, result="recovered")
        player.send_message(
            "§a检测到上次未正常结束的背包整理事务，已安全恢复："
            "假人保留当时玩家持有的假人背包，你的原背包已还原。"
        )

    def _handle_inventory_error(self, data: dict[str, Any]) -> None:
        session_id = str(data.get("s", "") or "")
        reason = str(data.get("reason", "") or "unknown")
        session = self.inventory_sessions.by_session(session_id)
        if session is None:
            self.logger.warning(f"未知背包事务错误: {reason}")
            return
        state = str(session.get("state", ""))
        if state == "preparing":
            # Behavior pack rejected before ownership transfer.
            self.inventory_sessions.abort_before_swap(session_id)
            self._message_inventory_player(session, f"§c无法开始整理假人背包：{reason}")
            return
        if state == "finishing" and reason == "finish_failed":
            self.inventory_sessions.set_state(session_id, "editing")
            self._message_inventory_player(session, "§e归还背包失败但已成功回滚交换；仍处于整理模式，可重试。")
            return
        if state == "recovery_storing" and reason in (
            "recovery_unavailable",
            "recovery_store_failed",
        ):
            self.inventory_sessions.set_state(session_id, "recovery_pending")
            return

        # rollback failure, non-empty recovery bot, poisoned lease, or any
        # unexpected state is ownership-ambiguous. Never auto-reconstruct.
        self.inventory_sessions.set_state(session_id, "manual_review")
        self.logger.error(
            f"背包事务进入 fail-close: session={session_id} state={state} reason={reason}"
        )
        self._message_inventory_player(
            session,
            "§c背包事务遇到不确定状态，已锁定以防止刷物。请联系管理员处理。",
        )

    def _handle_trident_result(self, data: dict[str, Any]) -> None:
        name = str(data.get("n", "") or "假人")
        requester = str(data.get("r", "") or "")
        ok = bool(data.get("ok", False))
        reason = str(data.get("reason", "") or "")

        messages = {
            "no_trident": f"假人 {name} 的背包里没有三叉戟。",
            "busy": f"假人 {name} 正在执行三叉戟动作。",
            "respawning": f"假人 {name} 正在重生，请稍后再试。",
            "respawn_failed": f"假人 {name} 重生失败，请稍后再试。",
            "not_found": f"假人 {name} 当前不在线。",
            "use_failed": f"假人 {name} 无法开始使用三叉戟。",
            "prepare_failed": f"假人 {name} 无法准备三叉戟投掷。",
            "release_failed": f"假人 {name} 未能成功松手投掷三叉戟。",
            "player_too_close": f"假人 {name} 附近 1 格内有其他玩家，请先离开后再投掷。",
        }
        message = (
            f"假人 {name} 已按你的视角投掷三叉戟。"
            if ok
            else messages.get(reason.split(":", 1)[0], f"假人 {name} 投掷三叉戟失败。")
        )

        if requester:
            for player in self.server.online_players:
                try:
                    if str(player.name).lower() == requester.lower():
                        player.send_message(("§a" if ok else "§c") + message)
                        return
                except Exception:
                    continue
        if not ok:
            self.logger.warning(f"三叉戟操作失败 [{name}]: {reason or 'unknown'}")

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        command_name = command.name.lower()
        if command_name == "botbridge":
            if len(args) < 2:
                return True
            parsed = self.bridge.handle_command_callback(str(args[0]), str(args[1]))
            if parsed is not None:
                self._handle_bridge_message(parsed)
            return True
        if command_name != "bot":
            return False
        if not args:
            return self._open_gui_or_usage(sender)

        action = str(args[0]).lower()
        if action == "gui":
            return self._open_gui_or_usage(sender)
        if action == "status":
            self._send_status(sender)
            return True
        if action == "list":
            self._send_list(sender)
            return True
        if action == "admin":
            if not self._require_admin(sender):
                return True
            if not hasattr(sender, "send_form"):
                self._send_error(sender, "控制台不能打开 GUI，请使用 /bot limit、/bot config、/bot list 等命令。")
                return True
            self.gui.open_admin(sender)
            return True
        if action == "spawn":
            if len(args) < 2:
                return True
            ok, message = self.manager.create_for_player(sender, str(args[1]))
            self._send_result(sender, ok, message)
            return True
        if action == "remove":
            if len(args) < 2:
                return True
            fp = self.manager.get_by_name(str(args[1]))
            if fp is None:
                self._send_error(sender, "假人不存在。")
                return True
            ok, message = self.manager.remove(sender, fp)
            self._send_result(sender, ok, message)
            return True
        if action == "tp":
            if len(args) < 2:
                return True
            fp = self.manager.get_by_name(str(args[1]))
            if fp is None:
                self._send_error(sender, "假人不存在。")
                return True
            ok, message = self.manager.move_here(sender, fp)
            self._send_result(sender, ok, message)
            return True
        if action == "trident":
            if len(args) < 2:
                return True
            fp = self.manager.get_by_name(str(args[1]))
            if fp is None:
                self._send_error(sender, "假人不存在。")
                return True
            ok, message = self.manager.throw_trident_here(sender, fp)
            self._send_result(sender, ok, message)
            return True
        if action == "inventory":
            if len(args) < 2:
                return True
            fp = self.manager.get_by_name(str(args[1]))
            if fp is None:
                self._send_error(sender, "假人不存在。")
                return True
            ok, message = self.manager.toggle_inventory_edit(sender, fp)
            self._send_result(sender, ok, message)
            return True
        if action == "createat":
            return self._cmd_create_at(sender, args)
        if action == "moveat":
            return self._cmd_move_at(sender, args)
        if action == "removeall":
            return self._cmd_remove_all(sender)
        if action == "limit":
            return self._cmd_limit(sender, args)
        if action == "config":
            return self._cmd_config(sender, args)
        return True

    def _open_gui_or_usage(self, sender: CommandSender) -> bool:
        if hasattr(sender, "send_form"):
            self.gui.open_main(sender)
        else:
            sender.send_message(
                "控制台管理：/bot status | list | createat | moveat | remove | removeall | limit | config"
            )
        return True

    def _send_status(self, sender: CommandSender) -> None:
        bridge = "在线" if self.bridge.active else "离线"
        sender.send_message(
            "§b===== 假人系统状态 =====\n"
            f"桥接：{bridge} (protocol {BRIDGE_PROTOCOL})\n"
            f"假人：{len(self.manager.bots)} / {self.settings.max_total}\n"
            f"普通玩家默认：{self.settings.max_per_player} 个\n"
            f"创建冷却：{self.settings.spawn_cooldown_seconds}s\n"
            f"位置守护：{'开启' if self.settings.guard_enabled else '关闭'}"
        )

    def _send_list(self, sender: CommandSender) -> None:
        if self.is_admin(sender) or not hasattr(sender, "unique_id"):
            bots = sorted(self.manager.bots.values(), key=lambda x: (x.owner_name.lower(), x.name.lower()))
        else:
            bots = self.manager.bots_for_owner(
                str(getattr(sender, "unique_id", "") or ""),
                str(getattr(sender, "name", "") or ""),
            )
        if not bots:
            sender.send_message("当前没有可显示的假人。")
            return
        lines = ["§b===== 假人列表 ====="]
        for fp in bots:
            lines.append(
                f"§f{fp.name} §7| {fp.owner_name} | {self.manager.status_text(fp)} | "
                f"{fp.dimension} {fp.location_x:.1f} {fp.location_y:.1f} {fp.location_z:.1f}"
            )
        sender.send_message("\n".join(lines))

    def _cmd_create_at(self, sender: CommandSender, args: list[str]) -> bool:
        if not self._require_admin(sender) or len(args) < 7:
            return True
        name, owner = str(args[1]), str(args[2])
        try:
            x, y, z = float(args[3]), float(args[4]), float(args[5])
        except ValueError:
            self._send_error(sender, "坐标必须是数字。")
            return True
        dimension = str(args[6])
        owner_uuid = self._online_uuid(owner)
        ok, message = self.manager.create_at(name, owner, owner_uuid, x, y, z, dimension)
        self._send_result(sender, ok, message)
        return True

    def _cmd_move_at(self, sender: CommandSender, args: list[str]) -> bool:
        if not self._require_admin(sender) or len(args) < 6:
            return True
        fp = self.manager.get_by_name(str(args[1]))
        if fp is None:
            self._send_error(sender, "假人不存在。")
            return True
        try:
            x, y, z = float(args[2]), float(args[3]), float(args[4])
        except ValueError:
            self._send_error(sender, "坐标必须是数字。")
            return True
        ok, message = self.manager.teleport_to(fp, x, y, z, str(args[5]))
        self._send_result(sender, ok, message)
        return True

    def _cmd_remove_all(self, sender: CommandSender) -> bool:
        if not self._require_admin(sender):
            return True
        if self.inventory_sessions.all():
            self._send_error(
                sender,
                "存在背包托管事务；为避免物品丢失或复制，不能执行 removeall。",
            )
            return True
        count = len(self.manager.bots)
        self.manager.clear_remote()
        self.manager.bots.clear()
        self.manager.name_index.clear()
        self.manager.save()
        sender.send_message(f"§a已清除全部 {count} 个假人。")
        return True

    def _cmd_limit(self, sender: CommandSender, args: list[str]) -> bool:
        if not self._require_admin(sender) or len(args) < 3:
            return True
        target = str(args[1])
        mode = str(args[2]).lower()
        key, name = self.settings.resolve_target_key(target, self.server.online_players)
        if mode == "show":
            self._send_limit(sender, key, name)
            return True
        if mode == "unlimited":
            self.settings.set_unlimited(key, name)
            sender.send_message(f"§a已解除 {name} 的全部假人限制。")
            return True
        if mode == "default":
            self.settings.reset_override(key, name)
            sender.send_message(f"§a{name} 已恢复全局默认限制。")
            return True
        if mode in ("max", "cooldown"):
            if len(args) < 4:
                return True
            try:
                value = int(args[3])
            except ValueError:
                self._send_error(sender, "值必须是整数。")
                return True
            if mode == "max":
                if value < -1:
                    self._send_error(sender, "max 最小为 -1（-1 表示不限）。")
                    return True
                self.settings.set_override(key, name=name, max_bots=value)
            else:
                if value < 0:
                    self._send_error(sender, "cooldown 不能小于 0。")
                    return True
                self.settings.set_override(key, name=name, cooldown_seconds=value)
            self._send_limit(sender, key, name)
            return True
        if mode == "bypassglobal":
            if len(args) < 4:
                return True
            enabled = str(args[3]).lower() in ("true", "1", "on", "yes")
            self.settings.set_override(key, name=name, bypass_global_limit=enabled)
            self._send_limit(sender, key, name)
            return True
        return True

    def _send_limit(self, sender: CommandSender, key: str, name: str) -> None:
        limits = self.settings.effective_for_key(key)
        max_text = "不限" if limits.unlimited else str(limits.max_bots)
        sender.send_message(
            f"§b{name} 的假人限制：§r上限={max_text}，冷却={limits.cooldown_seconds}s，"
            f"绕过全服上限={'是' if limits.bypass_global_limit else '否'}"
        )

    def _cmd_config(self, sender: CommandSender, args: list[str]) -> bool:
        if not self._require_admin(sender) or len(args) < 2:
            return True
        mode = str(args[1]).lower()
        if mode == "show":
            self._send_status(sender)
            return True
        if mode == "reload":
            self.settings.reload()
            sender.send_message("§a假人配置已重新加载。")
            return True
        if mode in ("maxtotal", "maxperplayer", "cooldown"):
            if len(args) < 3:
                return True
            try:
                value = int(args[2])
                self.settings.set_global(mode, value)
            except (ValueError, TypeError) as exc:
                self._send_error(sender, f"配置值无效: {exc}")
                return True
            self._send_status(sender)
        return True

    def is_admin(self, sender: Any) -> bool:
        try:
            value = getattr(sender, "is_op", None)
            if value is not None:
                return bool(value)
        except Exception:
            pass
        return not hasattr(sender, "unique_id") and not hasattr(sender, "location")

    def _require_admin(self, sender: Any) -> bool:
        if self.is_admin(sender):
            return True
        self._send_error(sender, "只有管理员可以执行该操作。")
        return False

    def _online_uuid(self, name: str) -> str:
        for player in self.server.online_players:
            try:
                if str(player.name).lower() == name.lower():
                    return str(getattr(player, "unique_id", "") or "")
            except Exception:
                pass
        return ""

    @staticmethod
    def dimension_id(dimension: Any) -> str:
        try:
            name = str(getattr(dimension, "name", "")).lower()
            dtype = str(getattr(dimension, "type", "")).lower()
        except Exception:
            name = dtype = ""
        text = f"{name} {dtype}"
        if "nether" in text:
            return "nether"
        if "end" in text and "stone" not in text:
            return "the_end"
        return "overworld"

    def _on_bridge_command_error(self, message: Any) -> None:
        self.logger.warning(f"bridge 命令执行失败: {message}")

    def _dispatch(self, command: str) -> bool:
        try:
            sender = getattr(self, "_bridge_command_sender", self.server.command_sender)
            return bool(self.server.dispatch_command(sender, command))
        except Exception as exc:
            self.logger.debug(f"命令执行失败 /{command}: {exc}")
            return False

    @staticmethod
    def _send_error(sender: Any, message: str) -> None:
        try:
            sender.send_error_message(message)
        except Exception:
            sender.send_message("§c" + message)

    @staticmethod
    def _send_result(sender: Any, ok: bool, message: str) -> None:
        sender.send_message(("§a" if ok else "§c") + message)

    def _find_world_dir(self) -> Path | None:
        try:
            name = str(self.server.level.name)
        except Exception:
            return None
        candidates = [
            Path.cwd() / "worlds" / name,
            Path.cwd() / "bedrock_server" / "worlds" / name,
            Path("bedrock_server") / "worlds" / name,
        ]
        for path in candidates:
            try:
                if (path / "level.dat").is_file():
                    return path.resolve()
            except Exception:
                continue
        return None

    def _setup_behavior_pack(self) -> str:
        world_dir = self._find_world_dir()
        if world_dir is None:
            self.logger.error("无法精确定位当前世界目录；为避免修改错误世界，已停止自动安装行为包。")
            return "missing-world"

        source = Path(__file__).parent / "behavior_pack"
        target = world_dir / "behavior_packs" / "endstone_bot_bridge"
        state = "unchanged"
        try:
            src_manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
            dst_manifest = None
            if (target / "manifest.json").exists():
                try:
                    dst_manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
                except Exception:
                    dst_manifest = None
            src_version = src_manifest.get("header", {}).get("version")
            dst_version = (dst_manifest or {}).get("header", {}).get("version")
            if not target.exists() or dst_version != src_version:
                state = "installed" if not target.exists() else "updated"
                staging = target.with_name(target.name + ".new")
                if staging.exists():
                    shutil.rmtree(staging)
                shutil.copytree(source, staging, ignore=shutil.ignore_patterns("*.mcpack", "__pycache__"))
                if target.exists():
                    shutil.rmtree(target)
                staging.replace(target)
        except Exception as exc:
            self.logger.error(f"安装行为包失败: {exc}")
            return "error"

        self._register_world_pack(world_dir)
        return state

    def _register_world_pack(self, world_dir: Path) -> None:
        path = world_dir / "world_behavior_packs.json"
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    raise ValueError("根节点不是数组")
            else:
                data = []
            found = False
            for entry in data:
                if isinstance(entry, dict) and entry.get("pack_id") == self.BEHAVIOR_PACK_UUID:
                    entry["version"] = list(self.BEHAVIOR_PACK_VERSION)
                    found = True
                    break
            if not found:
                data.append({"pack_id": self.BEHAVIOR_PACK_UUID, "version": list(self.BEHAVIOR_PACK_VERSION)})
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            self.logger.error(f"注册 world_behavior_packs.json 失败，未覆盖原文件: {exc}")
