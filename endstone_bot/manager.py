from __future__ import annotations

import json
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
            payload = {"version": 5, "bots": [fp.to_record() for fp in self.bots.values()]}
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
        fp = FakePlayer(
            id=generate_id(),
            name=name.strip(),
            owner_name=owner_name,
            owner_uuid=owner_uuid,
            location_x=round(float(location.x), 2),
            location_y=round(float(location.y), 2),
            location_z=round(float(location.z), 2),
            dimension=self._plugin.dimension_id(location.dimension),
            created=format_date_time_beijing(),
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
            dimension=dimension, created=format_date_time_beijing(),
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
        return self._bridge.send_bridge("spawn", {
            "n": fp.name,
            "x": fp.location_x,
            "y": fp.location_y,
            "z": fp.location_z,
            "d": fp.dimension,
        })

    def ensure_all_spawned(self) -> None:
        if not self._bridge.active:
            return
        now = time.monotonic()
        for fp in list(self.bots.values()):
            if fp.sim_spawn_confirmed and fp.sim_last_seen_at > 0 and now - fp.sim_last_seen_at <= 15.0:
                continue
            fp.sim_spawn_confirmed = False
            self.spawn(fp)

    def reconcile_names(self, names: set[str]) -> None:
        known = {fp.name.lower(): fp for fp in self.bots.values()}
        remote = {name.lower(): name for name in names}
        for lower, fp in known.items():
            fp.sim_spawn_confirmed = lower in remote
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
        old_seen = fp.sim_last_seen_at
        fp.mark_seen(nx, ny, nz, dimension)
        if old_seen <= 0:
            self._logger.info(f"假人 {fp.name} 已上线并开始上报位置。")

    def guard_positions(self) -> None:
        if not self._bridge.active or not self._settings.guard_enabled:
            return
        threshold_sq = self._settings.guard_distance ** 2
        for fp in list(self.bots.values()):
            if not fp.sim_has_position or not fp.is_recently_seen():
                continue
            dx = fp.sim_actual_x - fp.location_x
            dy = fp.sim_actual_y - fp.location_y
            dz = fp.sim_actual_z - fp.location_z
            if dx * dx + dy * dy + dz * dz > threshold_sq:
                self._bridge.send_bridge("teleport", {
                    "n": fp.name,
                    "x": fp.location_x,
                    "y": fp.location_y,
                    "z": fp.location_z,
                    "d": fp.dimension,
                })

    def move_here(self, sender: Any, fp: FakePlayer) -> tuple[bool, str]:
        if not self.can_manage(sender, fp):
            return False, "你没有权限管理该假人。"
        if not self._bridge.active:
            return False, "行为包桥接未就绪。"
        loc = getattr(sender, "location", None)
        if loc is None:
            return False, "该操作需要玩家位置。"
        fp.location_x = round(float(loc.x), 2)
        fp.location_y = round(float(loc.y), 2)
        fp.location_z = round(float(loc.z), 2)
        fp.dimension = self._plugin.dimension_id(loc.dimension)
        fp.sim_has_position = False
        self.save()
        self._bridge.send_bridge("teleport", {
            "n": fp.name,
            "x": fp.location_x,
            "y": fp.location_y,
            "z": fp.location_z,
            "d": fp.dimension,
        })
        return True, f"已将 {fp.name} 移到你的位置。"

    def teleport_to(self, fp: FakePlayer, x: float, y: float, z: float, dimension: str) -> tuple[bool, str]:
        if not self._bridge.active:
            return False, "行为包桥接未就绪。"
        fp.location_x = round(float(x), 2)
        fp.location_y = round(float(y), 2)
        fp.location_z = round(float(z), 2)
        fp.dimension = dimension
        fp.sim_has_position = False
        self.save()
        self._bridge.send_bridge("teleport", {"n": fp.name, "x": fp.location_x, "y": fp.location_y, "z": fp.location_z, "d": dimension})
        return True, f"已移动 {fp.name}。"

    def remove(self, sender: Any, fp: FakePlayer) -> tuple[bool, str]:
        if not self.can_manage(sender, fp):
            return False, "你没有权限管理该假人。"
        self._bridge.send_bridge("remove", {"n": fp.name})
        self.bots.pop(fp.id, None)
        self.name_index.pop(fp.name.lower(), None)
        self._last_spawn_request.pop(fp.id, None)
        self.save()
        return True, f"已删除假人 {fp.name}。"

    def remove_by_admin(self, fp: FakePlayer) -> None:
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
        if not self._bridge.active:
            return "桥接离线"
        if fp.sim_spawn_confirmed and fp.is_recently_seen():
            return "在线"
        if fp.sim_spawn_confirmed:
            return "等待位置上报"
        return "等待生成"
