from __future__ import annotations

import base64
import hashlib
import json
import os
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
    Tag,
)


_VALUE_TAGS = {
    "byte": ByteTag,
    "short": ShortTag,
    "int": IntTag,
    "long": LongTag,
    "float": FloatTag,
    "double": DoubleTag,
    "string": StringTag,
}


def tag_to_record(tag: Tag) -> dict[str, Any]:
    if isinstance(tag, ByteTag):
        return {"t": "byte", "v": int(tag.value)}
    if isinstance(tag, ShortTag):
        return {"t": "short", "v": int(tag.value)}
    if isinstance(tag, IntTag):
        return {"t": "int", "v": int(tag.value)}
    if isinstance(tag, LongTag):
        return {"t": "long", "v": int(tag.value)}
    if isinstance(tag, FloatTag):
        return {"t": "float", "v": float(tag.value)}
    if isinstance(tag, DoubleTag):
        return {"t": "double", "v": float(tag.value)}
    if isinstance(tag, StringTag):
        return {"t": "string", "v": str(tag.value)}
    if isinstance(tag, ByteArrayTag):
        return {"t": "byte_array", "v": base64.b64encode(bytes(tag)).decode("ascii")}
    if isinstance(tag, IntArrayTag):
        return {"t": "int_array", "v": [int(x) for x in tag]}
    if isinstance(tag, ListTag):
        return {"t": "list", "v": [tag_to_record(x) for x in tag]}
    if isinstance(tag, CompoundTag):
        return {"t": "compound", "v": {str(k): tag_to_record(v) for k, v in tag.items()}}
    raise TypeError(f"unsupported NBT tag: {type(tag)!r}")


def tag_from_record(data: dict[str, Any]) -> Tag:
    kind = str(data.get("t", ""))
    value = data.get("v")
    cls = _VALUE_TAGS.get(kind)
    if cls is not None:
        return cls(value)
    if kind == "byte_array":
        return ByteArrayTag(base64.b64decode(str(value or "").encode("ascii")))
    if kind == "int_array":
        return IntArrayTag(int(x) for x in (value or []))
    if kind == "list":
        return ListTag(tag_from_record(x) for x in (value or []))
    if kind == "compound":
        mapping = value if isinstance(value, dict) else {}
        return CompoundTag({str(k): tag_from_record(v) for k, v in mapping.items()})
    raise ValueError(f"unsupported NBT record kind: {kind!r}")


def item_to_record(item: ItemStack | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "type": str(item.type),
        "amount": int(item.amount),
        "data": int(item.data),
        "nbt": tag_to_record(item.nbt),
    }


def item_from_record(data: dict[str, Any] | None) -> ItemStack | None:
    if data is None:
        return None
    item = ItemStack(str(data["type"]), int(data.get("amount", 1)), int(data.get("data", 0)))
    nbt = data.get("nbt")
    if isinstance(nbt, dict):
        restored = tag_from_record(nbt)
        if not isinstance(restored, CompoundTag):
            raise ValueError("ItemStack NBT root must be CompoundTag")
        item.nbt = restored
    return item


def snapshot_inventory(inventory: Any) -> dict[str, Any]:
    contents = list(inventory.contents)
    snapshot: dict[str, Any] = {
        "size": int(inventory.size),
        "slots": [item_to_record(item) for item in contents],
    }
    if hasattr(inventory, "held_item_slot"):
        snapshot["held_item_slot"] = int(inventory.held_item_slot)
    return snapshot


def restore_inventory(inventory: Any, snapshot: dict[str, Any], *, restore_held_slot: bool = True) -> None:
    expected_size = int(snapshot.get("size", -1))
    actual_size = int(inventory.size)
    if expected_size != actual_size:
        raise ValueError(f"inventory size mismatch: snapshot={expected_size}, actual={actual_size}")
    slots = snapshot.get("slots")
    if not isinstance(slots, list) or len(slots) != expected_size:
        raise ValueError("invalid inventory snapshot slots")
    inventory.contents = [item_from_record(x) for x in slots]
    if restore_held_slot and "held_item_slot" in snapshot and hasattr(inventory, "held_item_slot"):
        inventory.held_item_slot = max(0, min(8, int(snapshot["held_item_slot"])))


def empty_inventory(inventory: Any) -> None:
    inventory.clear()


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def inventory_digest(inventory: Any) -> str:
    return snapshot_digest(snapshot_inventory(inventory))

class PersistentInventoryStore:
    VERSION = 1

    def __init__(self, path: Path, logger: Any) -> None:
        self.path = path
        self.logger = logger
        self.available = True
        self.snapshots: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or int(data.get("version", 0)) != self.VERSION:
                raise ValueError("unsupported bot inventory store schema")
            snapshots = data.get("snapshots", {})
            if not isinstance(snapshots, dict):
                raise ValueError("invalid snapshots object")
            self.snapshots = {
                str(k): dict(v)
                for k, v in snapshots.items()
                if isinstance(v, dict)
            }
        except Exception as exc:
            self.available = False
            self.logger.error(f"读取 bot_inventories.json 失败，假人背包持久化已 fail-close 禁用: {exc}")

    def _write_snapshots(self, snapshots: dict[str, dict[str, Any]]) -> None:
        if not self.available:
            raise RuntimeError("bot inventory store is unavailable")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"version": self.VERSION, "snapshots": snapshots}
        try:
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2, sort_keys=True)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, self.path)
            try:
                dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                pass
        except Exception:
            self.available = False
            raise

    def put(self, bot_id: str, snapshot: dict[str, Any]) -> None:
        updated = dict(self.snapshots)
        updated[str(bot_id)] = dict(snapshot)
        self._write_snapshots(updated)
        self.snapshots = updated

    def get(self, bot_id: str) -> dict[str, Any] | None:
        value = self.snapshots.get(str(bot_id))
        return dict(value) if isinstance(value, dict) else None

    def remove(self, bot_id: str) -> None:
        updated = dict(self.snapshots)
        updated.pop(str(bot_id), None)
        self._write_snapshots(updated)
        self.snapshots = updated

    def clear(self) -> None:
        self._write_snapshots({})
        self.snapshots = {}


class InventoryJournal:
    VERSION = 1

    def __init__(self, path: Path, logger: Any) -> None:
        self.path = path
        self.logger = logger
        self.available = True
        self.sessions: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or int(data.get("version", 0)) != self.VERSION:
                raise ValueError("unsupported inventory journal schema")
            sessions = data.get("sessions", {})
            if not isinstance(sessions, dict):
                raise ValueError("invalid sessions object")
            self.sessions = {str(k): dict(v) for k, v in sessions.items() if isinstance(v, dict)}
        except Exception as exc:
            self.available = False
            self.logger.error(f"读取 inventory_journal.json 失败，背包整理功能已 fail-close 禁用: {exc}")

    def _write_sessions(self, sessions: dict[str, dict[str, Any]]) -> None:
        if not self.available:
            raise RuntimeError("inventory journal is unavailable")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"version": self.VERSION, "sessions": sessions}
        try:
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2, sort_keys=True)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, self.path)
            try:
                dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                pass
        except Exception:
            self.available = False
            raise

    def save(self) -> None:
        self._write_sessions(self.sessions)

    def put(self, bot_id: str, session: dict[str, Any]) -> None:
        updated = dict(self.sessions)
        updated[str(bot_id)] = dict(session)
        self._write_sessions(updated)
        self.sessions = updated

    def remove(self, bot_id: str) -> None:
        updated = dict(self.sessions)
        updated.pop(str(bot_id), None)
        self._write_sessions(updated)
        self.sessions = updated

    def get_for_bot(self, bot_id: str) -> dict[str, Any] | None:
        value = self.sessions.get(str(bot_id))
        return dict(value) if isinstance(value, dict) else None

    def get_for_player(self, player_uuid: str, player_name: str) -> tuple[str, dict[str, Any]] | None:
        wanted_uuid = str(player_uuid or "")
        wanted_name = str(player_name or "").lower()
        for bot_id, session in self.sessions.items():
            if wanted_uuid and str(session.get("player_uuid", "")) == wanted_uuid:
                return bot_id, dict(session)
            if not wanted_uuid and wanted_name and str(session.get("player_name", "")).lower() == wanted_name:
                return bot_id, dict(session)
        return None
