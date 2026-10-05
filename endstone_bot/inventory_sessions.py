from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from endstone.inventory import ItemStack
from endstone.nbt import (
    ByteArrayTag,
    ByteTag,
    CompoundTag,
    DoubleTag,
    FloatTag,
    IntArrayTag,
    IntTag,
    ListTag,
    LongTag,
    ShortTag,
    StringTag,
)

MAIN_INVENTORY_SLOTS = 36


class InventorySnapshotError(RuntimeError):
    pass


_TAG_TYPES = {
    "ByteTag": ByteTag,
    "ShortTag": ShortTag,
    "IntTag": IntTag,
    "LongTag": LongTag,
    "FloatTag": FloatTag,
    "DoubleTag": DoubleTag,
    "StringTag": StringTag,
    "ByteArrayTag": ByteArrayTag,
    "IntArrayTag": IntArrayTag,
    "ListTag": ListTag,
    "CompoundTag": CompoundTag,
}


def _encode_tag(tag: Any) -> dict[str, Any]:
    name = type(tag).__name__
    if name == "CompoundTag":
        return {"t": name, "v": {str(k): _encode_tag(v) for k, v in tag.items()}}
    if name == "ListTag":
        return {"t": name, "v": [_encode_tag(v) for v in tag]}
    if name in ("ByteArrayTag", "IntArrayTag"):
        return {"t": name, "v": [int(v) for v in tag]}
    if name in ("ByteTag", "ShortTag", "IntTag", "LongTag"):
        return {"t": name, "v": int(tag.value)}
    if name in ("FloatTag", "DoubleTag"):
        return {"t": name, "v": float(tag.value)}
    if name == "StringTag":
        return {"t": name, "v": str(tag.value)}
    raise InventorySnapshotError(f"unsupported NBT tag type: {name}")


def _decode_tag(raw: Any) -> Any:
    if not isinstance(raw, dict):
        raise InventorySnapshotError("invalid encoded NBT tag")
    name = str(raw.get("t", ""))
    value = raw.get("v")
    cls = _TAG_TYPES.get(name)
    if cls is None:
        raise InventorySnapshotError(f"unsupported encoded NBT tag type: {name}")
    if name == "CompoundTag":
        if not isinstance(value, dict):
            raise InventorySnapshotError("invalid compound payload")
        return CompoundTag({str(k): _decode_tag(v) for k, v in value.items()})
    if name == "ListTag":
        if not isinstance(value, list):
            raise InventorySnapshotError("invalid list payload")
        return ListTag([_decode_tag(v) for v in value])
    if name in ("ByteArrayTag", "IntArrayTag"):
        if not isinstance(value, list):
            raise InventorySnapshotError(f"invalid {name} payload")
        return cls([int(v) for v in value])
    return cls(value)


def _serialize_item(item: Any) -> dict[str, Any] | None:
    if item is None:
        return None
    try:
        type_id = str(item.type.id)
        amount = int(item.amount)
        data = int(item.data)
        nbt = _encode_tag(item.nbt)
    except Exception as exc:
        raise InventorySnapshotError(f"failed to serialize ItemStack: {exc}") from exc
    return {"type": type_id, "amount": amount, "data": data, "nbt": nbt}


def _deserialize_item(raw: Any) -> ItemStack | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise InventorySnapshotError("invalid ItemStack snapshot")
    try:
        item = ItemStack(str(raw["type"]), int(raw["amount"]), int(raw.get("data", 0)))
        item.nbt = _decode_tag(raw["nbt"])
        return item
    except Exception as exc:
        raise InventorySnapshotError(f"failed to deserialize ItemStack: {exc}") from exc


def _snapshot_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "held_slot": int(snapshot["held_slot"]),
        "slots": snapshot["slots"],
    }


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    raw = json.dumps(
        _snapshot_payload(snapshot),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def capture_player_inventory(player: Any, *, held_slot: int | None = None) -> dict[str, Any]:
    inventory = getattr(player, "inventory", None)
    if inventory is None:
        raise InventorySnapshotError("player inventory is unavailable")
    try:
        size = len(inventory)
    except Exception as exc:
        raise InventorySnapshotError(f"cannot read player inventory size: {exc}") from exc
    if size < MAIN_INVENTORY_SLOTS:
        raise InventorySnapshotError(
            f"player inventory has only {size} slots; expected at least {MAIN_INVENTORY_SLOTS}"
        )
    slots = [_serialize_item(inventory.get_item(i)) for i in range(MAIN_INVENTORY_SLOTS)]
    selected = int(inventory.held_item_slot if held_slot is None else held_slot)
    if selected < 0 or selected > 8:
        selected = 0
    snapshot = {"held_slot": selected, "slots": slots}
    snapshot["sha256"] = snapshot_digest(snapshot)
    return snapshot


def player_main_inventory_empty(player: Any) -> bool:
    inventory = getattr(player, "inventory", None)
    if inventory is None or len(inventory) < MAIN_INVENTORY_SLOTS:
        return False
    return all(inventory.get_item(i) is None for i in range(MAIN_INVENTORY_SLOTS))


def restore_player_inventory(player: Any, snapshot: dict[str, Any]) -> None:
    inventory = getattr(player, "inventory", None)
    if inventory is None or len(inventory) < MAIN_INVENTORY_SLOTS:
        raise InventorySnapshotError("player inventory is unavailable")
    if not player_main_inventory_empty(player):
        raise InventorySnapshotError("refusing to restore over a non-empty player inventory")

    raw_slots = snapshot.get("slots")
    if not isinstance(raw_slots, list) or len(raw_slots) != MAIN_INVENTORY_SLOTS:
        raise InventorySnapshotError("invalid player backup slot count")

    # Construct every stack before mutating the live inventory.
    items = [_deserialize_item(raw) for raw in raw_slots]
    try:
        for index, item in enumerate(items):
            inventory.set_item(index, item)
        inventory.held_item_slot = max(0, min(8, int(snapshot.get("held_slot", 0))))
    except Exception as exc:
        # The destination was proven empty before restore. If a partial write
        # fails, erase only this attempted reconstruction and retain the on-disk
        # backup for another recovery attempt. Never stack a second copy on top.
        try:
            for index in range(MAIN_INVENTORY_SLOTS):
                inventory.clear(index)
        except Exception:
            pass
        raise InventorySnapshotError(f"failed to restore player inventory: {exc}") from exc


class InventorySessionStore:
    """Persistent ownership journal for bot inventory editing.

    Normal editing never reconstructs ItemStacks from snapshots. The 36 live
    slots are physically swapped between the real player and SimulatedPlayer.
    Snapshots exist only for crash recovery and verification.
    """

    def __init__(self, data_folder: Path, logger: Any) -> None:
        self._logger = logger
        self._path = data_folder / "inventory_sessions.json"
        self._archive_dir = data_folder / "inventory_backups"
        self._sessions: dict[str, dict[str, Any]] = {}
        self._load()

    @staticmethod
    def _player_key(player: Any) -> str:
        uuid = str(getattr(player, "unique_id", "") or "").strip()
        if uuid:
            return f"uuid:{uuid}"
        return f"name:{str(getattr(player, 'name', '') or '').strip().lower()}"

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._logger.error(f"读取 inventory_sessions.json 失败: {exc}")
            return
        rows = data.get("sessions", {}) if isinstance(data, dict) else {}
        if not isinstance(rows, dict):
            return
        for bot_id, raw in rows.items():
            if not isinstance(raw, dict) or not raw.get("session_id"):
                continue
            rec = dict(raw)
            # A plugin/server restart destroys the behavior-pack side lease.
            # Only "editing" proves that the first physical swap completed and
            # the plugin persisted that fact. Transitional states are ambiguous:
            # the Script API may have swapped already while the callback was lost.
            # Never guess ownership in that case; fail closed for manual review.
            previous_state = str(rec.get("state", ""))
            if previous_state in ("prepared", "editing", "recovery_pending"):
                rec["state"] = "recovery_pending"
            else:
                rec["state"] = "manual_review"
                rec["restart_from_state"] = previous_state
            self._sessions[str(bot_id)] = rec
        if self._sessions:
            self._save()

    @staticmethod
    def _atomic_write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        with tmp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        # Best-effort directory fsync makes the rename durable on POSIX. Windows
        # does not expose O_DIRECTORY, so the file fsync + atomic replace is the
        # strongest portable path available here.
        flags = getattr(os, "O_DIRECTORY", 0)
        if flags:
            try:
                fd = os.open(str(path.parent), os.O_RDONLY | flags)
            except OSError:
                return
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _save(self) -> None:
        self._atomic_write_json(
            self._path,
            {"version": 1, "sessions": self._sessions},
        )

    def all(self) -> list[dict[str, Any]]:
        return [dict(v) for v in self._sessions.values()]

    def for_bot_id(self, bot_id: str) -> dict[str, Any] | None:
        rec = self._sessions.get(str(bot_id))
        return rec if rec is None else dict(rec)

    def for_bot_name(self, bot_name: str) -> dict[str, Any] | None:
        wanted = str(bot_name or "").lower()
        for rec in self._sessions.values():
            if str(rec.get("bot_name", "")).lower() == wanted:
                return dict(rec)
        return None

    def by_session(self, session_id: str) -> dict[str, Any] | None:
        wanted = str(session_id or "")
        for rec in self._sessions.values():
            if str(rec.get("session_id", "")) == wanted:
                return dict(rec)
        return None

    def for_player(self, player: Any) -> dict[str, Any] | None:
        key = self._player_key(player)
        for rec in self._sessions.values():
            if rec.get("player_key") == key:
                return dict(rec)
        return None

    def is_bot_locked(self, bot_id: str) -> bool:
        return str(bot_id) in self._sessions

    def create(self, player: Any, fp: Any) -> dict[str, Any]:
        if self.is_bot_locked(fp.id):
            raise InventorySnapshotError("该假人的背包已经处于托管状态。")
        if self.for_player(player) is not None:
            raise InventorySnapshotError("你已经在整理另一个假人的背包。")

        player_backup = capture_player_inventory(player)
        now = int(time.time())
        rec = {
            "session_id": secrets.token_hex(16),
            "bot_id": str(fp.id),
            "bot_name": str(fp.name),
            "player_key": self._player_key(player),
            "player_uuid": str(getattr(player, "unique_id", "") or ""),
            "player_name": str(getattr(player, "name", "") or ""),
            "state": "preparing",
            "created_at": now,
            "updated_at": now,
            "player_backup": player_backup,
            "bot_backup": None,
            "bot_held_slot": None,
            "bot_game_mode": "",
        }
        self._sessions[str(fp.id)] = rec
        self._save()
        return dict(rec)

    def abort_before_swap(self, session_id: str) -> None:
        for bot_id, rec in list(self._sessions.items()):
            if rec.get("session_id") == session_id and rec.get("state") == "preparing":
                self._sessions.pop(bot_id, None)
                self._save()
                return

    def mark_swapped(
        self,
        session_id: str,
        player: Any,
        *,
        bot_held_slot: int,
        bot_game_mode: str,
    ) -> dict[str, Any]:
        rec = self._find_mut(session_id)
        if rec.get("state") not in ("preparing", "rollback_pending"):
            raise InventorySnapshotError(f"unexpected inventory session state: {rec.get('state')}")
        bot_backup = capture_player_inventory(player, held_slot=bot_held_slot)
        rec["bot_backup"] = bot_backup
        rec["bot_held_slot"] = max(0, min(8, int(bot_held_slot)))
        rec["bot_game_mode"] = str(bot_game_mode or "survival")
        rec["state"] = "prepared"
        rec["updated_at"] = int(time.time())
        self._save()
        return dict(rec)

    def set_state(self, session_id: str, state: str) -> dict[str, Any]:
        rec = self._find_mut(session_id)
        rec["state"] = str(state)
        rec["updated_at"] = int(time.time())
        self._save()
        return dict(rec)

    def verify_player_restored(self, session_id: str, player: Any) -> bool:
        rec = self._find_mut(session_id)
        current = capture_player_inventory(player)
        expected = rec.get("player_backup")
        if not isinstance(expected, dict):
            return False
        return snapshot_digest(current) == snapshot_digest(expected)

    def restore_player_backup(self, session_id: str, player: Any) -> None:
        rec = self._find_mut(session_id)
        snapshot = rec.get("player_backup")
        if not isinstance(snapshot, dict):
            raise InventorySnapshotError("player backup is missing")
        restore_player_inventory(player, snapshot)

    def complete(self, session_id: str, *, result: str = "completed") -> None:
        bot_id = None
        rec = None
        for key, value in self._sessions.items():
            if value.get("session_id") == session_id:
                bot_id, rec = key, value
                break
        if bot_id is None or rec is None:
            return
        archived = dict(rec)
        archived["state"] = result
        archived["completed_at"] = int(time.time())
        try:
            self._archive_dir.mkdir(parents=True, exist_ok=True)
            path = self._archive_dir / f"{session_id}.json"
            self._atomic_write_json(path, archived)
            files = sorted(
                self._archive_dir.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for stale in files[20:]:
                stale.unlink(missing_ok=True)
        except Exception as exc:
            self._logger.warning(f"归档背包托管快照失败: {exc}")
        self._sessions.pop(bot_id, None)
        self._save()

    def _find_mut(self, session_id: str) -> dict[str, Any]:
        for rec in self._sessions.values():
            if rec.get("session_id") == session_id:
                return rec
        raise InventorySnapshotError("inventory session not found")
