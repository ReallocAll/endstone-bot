"""Out-of-process deferred Beta APIs patching.

BDS keeps LevelData in memory and can overwrite level.dat during final save.
Endstone's embedded Python shutdown path also does not reliably execute atexit
handlers.  The reliable boundary is therefore a small detached Python helper:
it waits for the current Endstone/BDS process to actually terminate, then runs
the strict offline level.dat patcher.

A JSON request file is both the hand-off payload and the cancellation token.
Deleting it cancels the pending patch.  Re-loading the plugin in the same parent
process reuses the existing worker instead of spawning another one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_request(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_request(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def schedule_exit_patch(
    level_dat: Path,
    *,
    backup_root: Path,
    world_name: str,
    backup_keep: int,
    status_path: Path,
    request_path: Path | None = None,
) -> int:
    """Start or reuse a detached helper that patches after this process exits.

    Returns the helper PID.  A request from the same live parent process is
    reused, which makes /reload idempotent.
    """

    parent_pid = os.getpid()
    request_path = Path(request_path or status_path.with_name("beta_patch_request.json"))

    existing = _read_request(request_path)
    if existing and int(existing.get("parent_pid", -1)) == parent_pid:
        worker_pid = int(existing.get("worker_pid", -1))
        if _pid_alive(worker_pid):
            return worker_pid

    payload: dict[str, Any] = {
        "version": 1,
        "parent_pid": parent_pid,
        "worker_pid": 0,
        "level_dat": str(Path(level_dat).resolve()),
        "backup_root": str(Path(backup_root).resolve()),
        "world_name": str(world_name),
        "backup_keep": int(backup_keep),
        "status_path": str(Path(status_path).resolve()),
    }
    _write_request(request_path, payload)

    cmd = [
        sys.executable,
        "-m",
        "endstone_bot.beta_helper",
        "--request",
        str(request_path.resolve()),
        "--parent-pid",
        str(parent_pid),
    ]

    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception:
        try:
            request_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    payload["worker_pid"] = int(proc.pid)
    _write_request(request_path, payload)
    return int(proc.pid)


def cancel_exit_patch(request_path: Path) -> None:
    """Cancel a pending helper by removing its request/cancellation token."""
    try:
        Path(request_path).unlink(missing_ok=True)
    except Exception:
        pass


def pending_exit_patch(request_path: Path) -> dict[str, Any] | None:
    return _read_request(Path(request_path))


def consume_status(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        path.unlink(missing_ok=True)
        return data if isinstance(data, dict) else None
    except Exception:
        return None
