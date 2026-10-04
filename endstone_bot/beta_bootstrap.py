"""Deferred Beta APIs patching.

Endstone plugins are loaded after BDS has already taken its LevelData snapshot.
Writing level.dat from on_load/on_enable is therefore unsafe: the current server
session ignores the change and the final world save can overwrite it.

Instead we remember an exact level.dat target and patch it from a Python atexit
handler. atexit only runs on normal process termination, after BDS has completed
its server-thread shutdown and final save. /reload does not trigger it.
"""

from __future__ import annotations

import atexit
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from endstone_bot.level_dat import PatchResult, enable_beta_apis_safely


@dataclass(frozen=True)
class ExitPatchRequest:
    level_dat: Path
    backup_root: Path
    world_name: str
    backup_keep: int
    status_path: Path


_pending: ExitPatchRequest | None = None
_registered = False


def schedule_exit_patch(
    level_dat: Path,
    *,
    backup_root: Path,
    world_name: str,
    backup_keep: int,
    status_path: Path,
) -> None:
    global _pending, _registered
    _pending = ExitPatchRequest(
        level_dat=Path(level_dat),
        backup_root=Path(backup_root),
        world_name=str(world_name),
        backup_keep=int(backup_keep),
        status_path=Path(status_path),
    )
    if not _registered:
        atexit.register(_run_atexit_patch)
        _registered = True


def cancel_exit_patch() -> None:
    global _pending
    _pending = None


def pending_exit_patch() -> ExitPatchRequest | None:
    return _pending


def _write_status(path: Path, result: PatchResult) -> None:
    data: dict[str, Any] = {
        "status": result.status,
        "changed": bool(result.changed),
        "message": str(result.message),
        "backup_path": str(result.backup_path) if result.backup_path else "",
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def run_pending_exit_patch() -> PatchResult | None:
    """Run and clear the currently scheduled request.

    Exposed for tests; normal production use goes through the registered atexit
    handler.
    """
    global _pending
    request = _pending
    _pending = None
    if request is None:
        return None

    result = enable_beta_apis_safely(
        request.level_dat,
        backup_root=request.backup_root,
        world_name=request.world_name,
        backup_keep=request.backup_keep,
    )
    _write_status(request.status_path, result)
    return result


def _run_atexit_patch() -> None:
    try:
        run_pending_exit_patch()
    except Exception:
        # Never turn process shutdown into a crash. The safe patcher itself is
        # fail-closed; this outer guard is only for interpreter teardown edge cases.
        pass


def consume_status(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        path.unlink(missing_ok=True)
        return data if isinstance(data, dict) else None
    except Exception:
        return None
