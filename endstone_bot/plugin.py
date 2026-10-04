from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from endstone.command import Command, CommandSender
from endstone.event import PlayerJoinEvent, ScriptMessageEvent, event_handler
from endstone.plugin import Plugin

from endstone_bot.bridge import BRIDGE_PROTOCOL, BridgeManager
from endstone_bot.gui import BotGUI
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
                "/bot (spawn|remove|tp|trident)<action: BotNamedAction> <name: str>",
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
    BEHAVIOR_PACK_VERSION = [4, 1, 3]

    def on_enable(self) -> None:
        self.data_folder.mkdir(parents=True, exist_ok=True)
        self.settings = SettingsManager(self.data_folder, self.logger)
        self.bridge = BridgeManager(self.logger, self._dispatch)
        self.manager = FakeBotManager(
            self, self.data_folder, self.bridge, self.settings, self.logger
        )
        self.gui = BotGUI(self)
        self._tick_counter = 0
        self._list_names: set[str] = set()
        self._bridge_warning_sent = False

        pack_state = self._setup_behavior_pack()
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
                "行为包桥接尚未建立：请检查 Scripting/EndstoneBot 回调日志；首次安装或升级行为包后需要完整重启。"
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
            return
        if msg_id == "bot:trident_result":
            self._handle_trident_result(data)
            return
        if msg_id == "bot:lost":
            fp = self.manager.get_by_name(str(data.get("n", "")))
            if fp is not None:
                reason = str(data.get("reason", "") or "")
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
            "use_failed": f"假人 {name} 无法使用背包中的三叉戟。",
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

    def _dispatch(self, command: str) -> bool:
        try:
            return bool(self.server.dispatch_command(self.server.command_sender, command))
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
