from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from endstone.command import CommandSender

from endstone_bot.models import FakePlayer, format_date_time_beijing, generate_id, validate_name


class FakeBotManager:
    def __init__(self, plugin: Any, data_folder: Path, bridge: Any, settings: Any, logger: Any) -> None:
        self._plugin = plugin
        self._bridge = bridge
        self._settings = settings
        self._logger = logger
        self._db_path = data_folder / "bots.json"
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

    def inventory_session(self, fp: FakePlayer) -> dict[str, Any] | None:
        return self._plugin.inventory_sessions.for_bot_id(fp.id)

    def inventory_locked(self, fp: FakePlayer) -> bool:
        return self._plugin.inventory_sessions.is_bot_locked(fp.id)

    def toggle_inventory_edit(self, sender: Any, fp: FakePlayer) -> tuple[bool, str]:
        if not self.can_manage(sender, fp):
            return False, "你没有权限管理该假人。"
        if not hasattr(sender, "inventory"):
            return False, "背包整理只能由在线玩家执行。"
        if not self._bridge.active:
            return False, "行为包桥接未就绪。"

        current = self.inventory_session(fp)
        player_session = self._plugin.inventory_sessions.for_player(sender)
        if current is not None:
            if player_session is None or player_session.get("session_id") != current.get("session_id"):
                return False, "该假人的背包正在被其他玩家整理或恢复。"
            state = str(current.get("state", ""))
            if state == "editing":
                session_id = str(current["session_id"])
                self._plugin.inventory_sessions.set_state(session_id, "finishing")
                ok = self._bridge.send_bridge(
                    "inventory_finish",
                    {
                        "n": fp.name,
                        "r": str(getattr(sender, "name", "") or ""),
                        "s": session_id,
                    },
                )
                if not ok:
                    self._plugin.inventory_sessions.set_state(session_id, "editing")
                    return False, "结束背包整理指令发送失败；事务保持锁定。"
                return True, f"正在将整理后的背包归还 {fp.name}，并恢复你的原背包。"
            if state == "recovery_pending":
                return False, "检测到上次背包整理未正常结束，正在等待安全恢复。"
            if state in ("preparing", "prepared", "finishing", "recovery_storing", "recovery_restoring"):
                return False, "背包事务正在处理中，请稍后。"
            return False, f"背包事务处于保护状态：{state or 'unknown'}。"

        if player_session is not None:
            return False, "你已经在整理另一个假人的背包。"

        try:
            rec = self._plugin.inventory_sessions.create(sender, fp)
        except Exception as exc:
            return False, f"创建背包备份失败，未开始整理：{exc}"

        session_id = str(rec["session_id"])
        ok = self._bridge.send_bridge(
            "inventory_begin",
            {
                "n": fp.name,
                "r": str(getattr(sender, "name", "") or ""),
                "s": session_id,
            },
        )
        if not ok:
            self._plugin.inventory_sessions.abort_before_swap(session_id)
            return False, "背包交换指令发送失败；未改动双方背包。"
        return True, f"正在备份并接管 {fp.name} 的 36 格主背包。"

    def _player_identity(self, sender: Any) -> tuple[str, str, str]:
        name = str(getattr(sender, "name", "") or "")
        uuid = str(getattr(sender, "unique_id", "") or "")
        key = self._settings.remember_player(uuid, name)
        return key, uuid, name


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
            return False, "该假人的背包正在托管，暂时不能移动。"
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
            return False, "该假人的背包正在托管，暂时不能执行三叉戟动作。"
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
            return False, "该假人的背包正在托管，暂时不能移动。"
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
            return False, "该假人的背包正在托管；为避免物品丢失或复制，不能删除。"
        self._bridge.send_bridge("remove", {"n": fp.name})
        self.bots.pop(fp.id, None)
        self.name_index.pop(fp.name.lower(), None)
        self._last_spawn_request.pop(fp.id, None)
        self.save()
        return True, f"已删除假人 {fp.name}。"

    def remove_by_admin(self, fp: FakePlayer) -> None:
        if self.inventory_locked(fp):
            self._logger.warning(
                f"拒绝删除背包托管中的假人 {fp.name}；请先完成或恢复背包事务。"
            )
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
            if state == "editing":
                return "背包整理中"
            if state == "recovery_pending":
                return "背包待恢复"
            if state.startswith("recovery_"):
                return "背包恢复中"
            return "背包事务处理中"
        if not self._bridge.active:
            return "桥接离线"
        if fp.is_recently_seen() or fp.sim_spawn_confirmed:
            return "在线"
        return "等待生成"
