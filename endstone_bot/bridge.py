from __future__ import annotations

import json
import secrets
import time
from typing import Any

from endstone.event import ScriptMessageEvent

BRIDGE_PROTOCOL = 2


class BridgeManager:
    """Authenticated scriptevent bridge to the bundled behavior pack."""

    def __init__(self, logger: Any, dispatch_fn: Any) -> None:
        self._logger = logger
        self._dispatch = dispatch_fn
        self._token = secrets.token_hex(16)
        self._last_seen_at = -999.0
        self._ready = False
        self._remote_protocol: int | None = None

    @property
    def active(self) -> bool:
        return self._ready and self._last_seen_at > 0 and time.monotonic() - self._last_seen_at < 15.0

    @property
    def protocol(self) -> int:
        return BRIDGE_PROTOCOL

    @property
    def remote_protocol(self) -> int | None:
        return self._remote_protocol

    def reset(self) -> None:
        self._ready = False
        self._last_seen_at = -999.0
        self._remote_protocol = None
        self._token = secrets.token_hex(16)

    def _send_raw(self, event_id: str, data: dict[str, Any]) -> bool:
        payload = {**data, "t": self._token, "p": BRIDGE_PROTOCOL}
        msg = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        try:
            return bool(self._dispatch(f"scriptevent {event_id} {msg}"))
        except Exception as exc:
            self._logger.debug(f"发送 scriptevent 失败: {exc}")
            return False

    def hello(self) -> bool:
        return self._send_raw("bot:hello", {})

    def ping(self) -> bool:
        if not self.active:
            return self.hello()
        return self._send_raw("bot:ping", {})

    def request_list(self) -> bool:
        if not self.active:
            return False
        return self._send_raw("bot:list", {})

    def shutdown(self) -> bool:
        """Gracefully clear remote bots and release the session token for /reload."""
        if not self._ready:
            return False
        ok = self._send_raw("bot:shutdown", {})
        self._ready = False
        return ok

    def send_bridge(self, action: str, data: dict[str, Any]) -> bool:
        if not self.active:
            return False
        event_id = {
            "spawn": "bot:spawn",
            "remove": "bot:remove",
            "teleport": "bot:teleport",
            "clear": "bot:clear",
        }.get(action)
        if event_id is None:
            return False
        return self._send_raw(event_id, data)

    def handle_script_message(self, event: ScriptMessageEvent) -> dict[str, Any] | None:
        msg_id = str(event.message_id or "")
        if not msg_id.startswith("bot:"):
            return None
        try:
            data = json.loads(event.message) if event.message else {}
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        if data.get("t") != self._token:
            self._logger.debug(f"忽略未经认证的 scriptevent: {msg_id}")
            return None

        try:
            remote_protocol = int(data.get("p", BRIDGE_PROTOCOL))
        except (TypeError, ValueError):
            remote_protocol = -1

        if msg_id == "bot:hello_ack":
            self._remote_protocol = remote_protocol
            if remote_protocol != BRIDGE_PROTOCOL:
                self._ready = False
                self._logger.error(
                    f"行为包协议不兼容: plugin={BRIDGE_PROTOCOL}, pack={remote_protocol}"
                )
                return None
            first_ready = not self._ready
            self._ready = True
            self._last_seen_at = time.monotonic()
            if first_ready:
                self._logger.info("行为包桥接已认证，SimulatedPlayer 功能可用。")
            return {"id": msg_id, "data": data}

        if remote_protocol != BRIDGE_PROTOCOL:
            return None
        if not self._ready:
            return None
        self._last_seen_at = time.monotonic()
        return {"id": msg_id, "data": data}

    def mark_stale_if_needed(self) -> bool:
        if self._ready and not self.active:
            self._ready = False
            self._logger.warning("行为包桥接已超时，进入断开状态。")
            return True
        return False
