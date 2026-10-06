from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from endstone_bot.inventory_journal import (
    item_from_record,
    item_to_record,
    restore_inventory,
    snapshot_digest,
    snapshot_inventory,
)


_EQUIPMENT_SLOTS = (
    "helmet",
    "chestplate",
    "leggings",
    "boots",
    "item_in_off_hand",
)


def snapshot_bot_inventory(inventory: Any) -> dict[str, Any]:
    snapshot = snapshot_inventory(inventory)
    snapshot["equipment"] = {
        name: item_to_record(getattr(inventory, name))
        for name in _EQUIPMENT_SLOTS
    }
    return snapshot


def restore_bot_inventory(inventory: Any, snapshot: dict[str, Any]) -> None:
    equipment = snapshot.get("equipment")
    if not isinstance(equipment, dict):
        raise ValueError("invalid bot inventory equipment snapshot")

    restore_inventory(inventory, snapshot, restore_held_slot=True)
    for name in _EQUIPMENT_SLOTS:
        setattr(inventory, name, item_from_record(equipment.get(name)))


def bot_inventory_digest(inventory: Any) -> str:
    return snapshot_digest(snapshot_bot_inventory(inventory))


def bot_inventory_is_empty(inventory: Any) -> bool:
    snapshot = snapshot_bot_inventory(inventory)
    slots = snapshot.get("slots")
    equipment = snapshot.get("equipment")
    return (
        isinstance(slots, list)
        and all(item is None for item in slots)
        and isinstance(equipment, dict)
        and all(equipment.get(name) is None for name in _EQUIPMENT_SLOTS)
    )


class BotInventoryStore:
    VERSION = 1

    def __init__(self, path: Path, logger: Any) -> None:
        self.path = path
        self.logger = logger
        self.available = True
        self.records: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or int(data.get("version", 0)) != self.VERSION:
                raise ValueError("unsupported bot inventory store schema")
            records = data.get("bots", {})
            if not isinstance(records, dict):
                raise ValueError("invalid bot inventory records object")
            self.records = {
                str(bot_id): dict(record)
                for bot_id, record in records.items()
                if isinstance(record, dict)
            }
        except Exception as exc:
            self.available = False
            self.logger.error(
                f"读取 bot_inventories.json 失败，假人背包持久化已 fail-close 禁用: {exc}"
            )

    def _write_records(self, records: dict[str, dict[str, Any]]) -> None:
        if not self.available:
            raise RuntimeError("bot inventory store is unavailable")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"version": self.VERSION, "bots": records}
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

    def put(self, bot_id: str, record: dict[str, Any]) -> None:
        updated = dict(self.records)
        updated[str(bot_id)] = dict(record)
        self._write_records(updated)
        self.records = updated

    def remove(self, bot_id: str) -> None:
        key = str(bot_id)
        if key not in self.records:
            return
        updated = dict(self.records)
        updated.pop(key, None)
        self._write_records(updated)
        self.records = updated

    def get(self, bot_id: str) -> dict[str, Any] | None:
        value = self.records.get(str(bot_id))
        return dict(value) if isinstance(value, dict) else None
