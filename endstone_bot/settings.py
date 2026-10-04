from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "limits": {
        "max_total": 6,
        "max_per_player": 1,
        "spawn_cooldown_seconds": 10,
    },
    "position_guard": {
        "enabled": True,
        "interval_ticks": 10,
        "distance": 1.0,
    },
    "beta_api": {
        "auto_enable": True,
        "backup_keep": 5,
    },
}


@dataclass(frozen=True)
class EffectiveLimits:
    max_bots: int
    cooldown_seconds: int
    bypass_global_limit: bool

    @property
    def unlimited(self) -> bool:
        return self.max_bots < 0


class SettingsManager:
    """Global limits and per-player overrides.

    Per-player values use the following semantics:
      * max_bots=None: inherit global default
      * max_bots=-1: unlimited
      * cooldown_seconds=None: inherit global default
      * cooldown_seconds=0: no cooldown
      * bypass_global_limit=True: this player may create bots after the global cap
    """

    def __init__(self, data_folder: Path, logger: Any) -> None:
        self._logger = logger
        self._config_path = data_folder / "config.json"
        self._players_path = data_folder / "player_limits.json"
        self._config: dict[str, Any] = {}
        self._players: dict[str, dict[str, Any]] = {}
        self.reload()

    @property
    def config(self) -> dict[str, Any]:
        return self._config

    @property
    def max_total(self) -> int:
        return max(1, int(self._config["limits"]["max_total"]))

    @property
    def max_per_player(self) -> int:
        return max(0, int(self._config["limits"]["max_per_player"]))

    @property
    def spawn_cooldown_seconds(self) -> int:
        return max(0, int(self._config["limits"]["spawn_cooldown_seconds"]))

    @property
    def guard_enabled(self) -> bool:
        return bool(self._config["position_guard"]["enabled"])

    @property
    def guard_interval_ticks(self) -> int:
        return max(1, int(self._config["position_guard"]["interval_ticks"]))

    @property
    def guard_distance(self) -> float:
        return max(0.0, float(self._config["position_guard"]["distance"]))

    @property
    def beta_auto_enable(self) -> bool:
        return bool(self._config["beta_api"]["auto_enable"])

    @property
    def beta_backup_keep(self) -> int:
        return max(1, int(self._config["beta_api"]["backup_keep"]))

    def reload(self) -> None:
        self._config = self._load_config()
        self._players = self._load_players()

    def _load_config(self) -> dict[str, Any]:
        data = self._read_json(self._config_path)
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if isinstance(data, dict):
            limits = data.get("limits")
            if isinstance(limits, dict):
                for key in ("max_total", "max_per_player", "spawn_cooldown_seconds"):
                    if key in limits:
                        cfg["limits"][key] = limits[key]
            guard = data.get("position_guard")
            if isinstance(guard, dict):
                for key in ("enabled", "interval_ticks", "distance"):
                    if key in guard:
                        cfg["position_guard"][key] = guard[key]
            beta_api = data.get("beta_api")
            if isinstance(beta_api, dict):
                for key in ("auto_enable", "backup_keep"):
                    if key in beta_api:
                        cfg["beta_api"][key] = beta_api[key]
        self._sanitize_config(cfg)
        self._write_json(self._config_path, cfg)
        return cfg

    def _load_players(self) -> dict[str, dict[str, Any]]:
        data = self._read_json(self._players_path)
        if not isinstance(data, dict):
            return {}
        players = data.get("players", {})
        if not isinstance(players, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for key, raw in players.items():
            if not isinstance(raw, dict):
                continue
            result[str(key)] = {
                "name": str(raw.get("name", "")),
                "max_bots": self._optional_int(raw.get("max_bots")),
                "cooldown_seconds": self._optional_int(raw.get("cooldown_seconds")),
                "bypass_global_limit": bool(raw.get("bypass_global_limit", False)),
            }
        return result

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _sanitize_config(self, cfg: dict[str, Any]) -> None:
        limits = cfg["limits"]
        limits["max_total"] = max(1, min(128, int(limits.get("max_total", 6))))
        limits["max_per_player"] = max(0, min(64, int(limits.get("max_per_player", 1))))
        limits["spawn_cooldown_seconds"] = max(
            0, min(3600, int(limits.get("spawn_cooldown_seconds", 10)))
        )
        guard = cfg["position_guard"]
        guard["enabled"] = bool(guard.get("enabled", True))
        guard["interval_ticks"] = max(1, min(1200, int(guard.get("interval_ticks", 10))))
        guard["distance"] = max(0.0, min(32.0, float(guard.get("distance", 1.0))))
        beta_api = cfg["beta_api"]
        beta_api["auto_enable"] = bool(beta_api.get("auto_enable", True))
        beta_api["backup_keep"] = max(1, min(20, int(beta_api.get("backup_keep", 5))))

    def save_config(self) -> None:
        self._sanitize_config(self._config)
        self._write_json(self._config_path, self._config)

    def save_players(self) -> None:
        self._write_json(
            self._players_path,
            {"version": 1, "players": self._players},
        )

    def set_global(self, key: str, value: int) -> None:
        if key == "maxtotal":
            self._config["limits"]["max_total"] = value
        elif key == "maxperplayer":
            self._config["limits"]["max_per_player"] = value
        elif key == "cooldown":
            self._config["limits"]["spawn_cooldown_seconds"] = value
        else:
            raise ValueError(f"unknown global key: {key}")
        self.save_config()

    def set_guard(self, *, enabled: bool | None = None, interval_ticks: int | None = None, distance: float | None = None) -> None:
        guard = self._config["position_guard"]
        if enabled is not None:
            guard["enabled"] = bool(enabled)
        if interval_ticks is not None:
            guard["interval_ticks"] = int(interval_ticks)
        if distance is not None:
            guard["distance"] = float(distance)
        self.save_config()

    def set_beta_auto_enable(self, enabled: bool) -> None:
        self._config["beta_api"]["auto_enable"] = bool(enabled)
        self.save_config()

    @staticmethod
    def identity_key(uuid: str, name: str) -> str:
        uid = str(uuid or "").strip()
        if uid:
            return f"uuid:{uid}"
        return f"name:{str(name or '').strip().lower()}"

    def remember_player(self, uuid: str, name: str) -> str:
        name = str(name or "").strip()
        uuid = str(uuid or "").strip()
        key = self.identity_key(uuid, name)
        provisional = f"name:{name.lower()}" if name else ""
        if uuid and provisional and provisional in self._players and provisional != key:
            old = self._players.pop(provisional)
            current = self._players.get(key, {})
            self._players[key] = {
                "name": name,
                "max_bots": current.get("max_bots", old.get("max_bots")),
                "cooldown_seconds": current.get("cooldown_seconds", old.get("cooldown_seconds")),
                "bypass_global_limit": bool(
                    current.get("bypass_global_limit", old.get("bypass_global_limit", False))
                ),
            }
            self.save_players()
            return key
        rec = self._players.setdefault(
            key,
            {
                "name": name,
                "max_bots": None,
                "cooldown_seconds": None,
                "bypass_global_limit": False,
            },
        )
        if name and rec.get("name") != name:
            rec["name"] = name
            self.save_players()
        return key

    def resolve_target_key(self, name: str, online_players: Any) -> tuple[str, str]:
        wanted = str(name or "").strip()
        for player in online_players:
            try:
                if str(player.name).lower() == wanted.lower():
                    uid = str(getattr(player, "unique_id", "") or "")
                    key = self.remember_player(uid, str(player.name))
                    return key, str(player.name)
            except Exception:
                continue
        for key, rec in self._players.items():
            if str(rec.get("name", "")).lower() == wanted.lower():
                return key, str(rec.get("name", wanted) or wanted)
        key = f"name:{wanted.lower()}"
        self._players.setdefault(
            key,
            {
                "name": wanted,
                "max_bots": None,
                "cooldown_seconds": None,
                "bypass_global_limit": False,
            },
        )
        self.save_players()
        return key, wanted

    def effective_for_key(self, key: str) -> EffectiveLimits:
        rec = self._players.get(key, {})
        max_bots = rec.get("max_bots")
        cooldown = rec.get("cooldown_seconds")
        return EffectiveLimits(
            max_bots=self.max_per_player if max_bots is None else int(max_bots),
            cooldown_seconds=self.spawn_cooldown_seconds if cooldown is None else max(0, int(cooldown)),
            bypass_global_limit=bool(rec.get("bypass_global_limit", False)),
        )

    def effective(self, uuid: str, name: str) -> tuple[str, EffectiveLimits]:
        key = self.remember_player(uuid, name)
        return key, self.effective_for_key(key)

    def override(self, key: str) -> dict[str, Any]:
        rec = self._players.setdefault(
            key,
            {"name": "", "max_bots": None, "cooldown_seconds": None, "bypass_global_limit": False},
        )
        return dict(rec)

    def set_override(
        self,
        key: str,
        *,
        name: str | None = None,
        max_bots: int | None | object = ...,
        cooldown_seconds: int | None | object = ...,
        bypass_global_limit: bool | object = ...,
    ) -> None:
        rec = self._players.setdefault(
            key,
            {"name": name or "", "max_bots": None, "cooldown_seconds": None, "bypass_global_limit": False},
        )
        if name is not None:
            rec["name"] = name
        if max_bots is not ...:
            rec["max_bots"] = None if max_bots is None else max(-1, min(64, int(max_bots)))
        if cooldown_seconds is not ...:
            rec["cooldown_seconds"] = None if cooldown_seconds is None else max(0, min(3600, int(cooldown_seconds)))
        if bypass_global_limit is not ...:
            rec["bypass_global_limit"] = bool(bypass_global_limit)
        self.save_players()

    def set_unlimited(self, key: str, name: str) -> None:
        self.set_override(
            key,
            name=name,
            max_bots=-1,
            cooldown_seconds=0,
            bypass_global_limit=True,
        )

    def reset_override(self, key: str, name: str) -> None:
        self._players[key] = {
            "name": name,
            "max_bots": None,
            "cooldown_seconds": None,
            "bypass_global_limit": False,
        }
        self.save_players()

    def known_players(self) -> list[tuple[str, dict[str, Any]]]:
        rows = list(self._players.items())
        rows.sort(key=lambda item: str(item[1].get("name", "")).lower())
        return rows

    def configured_overrides(self) -> list[tuple[str, dict[str, Any]]]:
        result = []
        for key, rec in self.known_players():
            if rec.get("max_bots") is not None or rec.get("cooldown_seconds") is not None or rec.get("bypass_global_limit"):
                result.append((key, rec))
        return result

    @staticmethod
    def _read_json(path: Path) -> Any:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_json(self, path: Path, data: Any) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            self._logger.warning(f"保存 {path.name} 失败: {exc}")
