from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

MAX_NAME_LENGTH = 24
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_BEIJING_TZ = timezone(timedelta(hours=8))


def generate_id() -> str:
    return uuid.uuid4().hex


def format_date_time_beijing() -> str:
    return datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def validate_name(name: str, existing_names: set[str], online_player_names: set[str]) -> str | None:
    value = str(name or "").strip()
    if not value:
        return "假人名称不能为空"
    if len(value) > MAX_NAME_LENGTH:
        return f"假人名称不能超过 {MAX_NAME_LENGTH} 个字符"
    if not _NAME_PATTERN.fullmatch(value):
        return "假人名称只能包含字母、数字、下划线和短横线"
    lower = value.lower()
    if lower in {x.lower() for x in existing_names}:
        return "已有同名假人，请换一个名称"
    if lower in {x.lower() for x in online_player_names}:
        return "该名称已被在线玩家占用"
    return None


@dataclass
class FakePlayer:
    id: str
    name: str
    owner_name: str
    owner_uuid: str
    location_x: float
    location_y: float
    location_z: float
    dimension: str
    pitch: float
    yaw: float
    created: str
    desired_online: bool = True
    view_x: float = 0.0
    view_y: float = 0.0
    view_z: float = 0.0

    sim_spawn_confirmed: bool = False
    sim_has_position: bool = False
    sim_actual_x: float = 0.0
    sim_actual_y: float = 0.0
    sim_actual_z: float = 0.0
    sim_last_seen_at: float = 0.0

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "ownerName": self.owner_name,
            "ownerUuid": self.owner_uuid,
            "location": [round(self.location_x, 2), round(self.location_y, 2), round(self.location_z, 2)],
            "dimension": self.dimension,
            "rotation": [round(self.pitch, 2), round(self.yaw, 2)],
            "viewDirection": [round(self.view_x, 6), round(self.view_y, 6), round(self.view_z, 6)],
            "created": self.created,
            "desiredOnline": bool(self.desired_online),
            "type": "simulated",
        }

    @classmethod
    def from_record(cls, data: dict[str, Any]) -> "FakePlayer":
        loc = data.get("location", [0.5, 80.0, 0.5])
        if not isinstance(loc, list) or len(loc) < 3:
            loc = [0.5, 80.0, 0.5]
        rot = data.get("rotation", [0.0, 0.0])
        if not isinstance(rot, list) or len(rot) < 2:
            rot = [0.0, 0.0]
        view = data.get("viewDirection", [0.0, 0.0, 0.0])
        if not isinstance(view, list) or len(view) < 3:
            view = [0.0, 0.0, 0.0]
        return cls(
            id=str(data.get("id") or generate_id()),
            name=str(data.get("name", "")),
            owner_name=str(data.get("ownerName", "")),
            owner_uuid=str(data.get("ownerUuid", "")),
            location_x=float(loc[0]),
            location_y=float(loc[1]),
            location_z=float(loc[2]),
            dimension=str(data.get("dimension", "overworld")).replace("minecraft:", ""),
            pitch=float(rot[0]),
            yaw=float(rot[1]),
            created=str(data.get("created", "")) or format_date_time_beijing(),
            desired_online=bool(data.get("desiredOnline", True)),
            view_x=float(view[0]),
            view_y=float(view[1]),
            view_z=float(view[2]),
        )

    def mark_seen(self, x: float, y: float, z: float, dimension: str) -> bool:
        nx, ny, nz = round(float(x), 2), round(float(y), 2), round(float(z), 2)
        changed = (
            abs(self.location_x - nx) > 0.01
            or abs(self.location_y - ny) > 0.01
            or abs(self.location_z - nz) > 0.01
            or self.dimension != dimension
        )
        self.sim_actual_x = nx
        self.sim_actual_y = ny
        self.sim_actual_z = nz
        self.sim_has_position = True
        self.sim_last_seen_at = time.monotonic()
        self.sim_spawn_confirmed = True
        return changed

    def is_recently_seen(self, threshold: float = 15.0) -> bool:
        return self.sim_last_seen_at > 0 and time.monotonic() - self.sim_last_seen_at <= threshold
