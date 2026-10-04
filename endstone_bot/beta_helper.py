"""Detached helper for post-process Beta APIs patching."""

from __future__ import annotations

import argparse
import json
import os
import select
import time
from pathlib import Path
from typing import Any

from endstone_bot.level_dat import PatchResult, enable_beta_apis_safely


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
        os.replace(tmp, path)
    except Exception:
        pass


def wait_for_process_exit(pid: int) -> None:
    """Wait for one exact process to terminate using only the stdlib."""
    if pid <= 0:
        return

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        SYNCHRONIZE = 0x00100000
        INFINITE = 0xFFFFFFFF

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return
        try:
            kernel32.WaitForSingleObject(handle, INFINITE)
        finally:
            kernel32.CloseHandle(handle)
        return

    if hasattr(os, "pidfd_open"):
        try:
            fd = os.pidfd_open(pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            fd = -1
        if fd >= 0:
            try:
                select.select([fd], [], [])
            finally:
                os.close(fd)
            return

    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        except OSError:
            return
        time.sleep(0.5)


def _load_request(path: Path, expected_parent_pid: int) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if int(data.get("parent_pid", -1)) != int(expected_parent_pid):
            return None
        return data
    except Exception:
        return None


def run_request(request_path: Path, parent_pid: int) -> PatchResult | None:
    """Wait for parent exit, then execute the request if it still exists."""
    wait_for_process_exit(parent_pid)

    # Give the OS a small grace period to finish closing file handles inherited
    # from the BDS shutdown path before opening level.dat for replacement.
    time.sleep(0.25)

    request = _load_request(request_path, parent_pid)
    if request is None:
        return None

    try:
        level_dat = Path(str(request["level_dat"]))
        backup_root = Path(str(request["backup_root"]))
        world_name = str(request["world_name"])
        backup_keep = int(request.get("backup_keep", 5))
        status_path = Path(str(request["status_path"]))
    except Exception:
        return None

    result = enable_beta_apis_safely(
        level_dat,
        backup_root=backup_root,
        world_name=world_name,
        backup_keep=backup_keep,
    )
    _write_status(status_path, result)

    try:
        request_path.unlink(missing_ok=True)
    except Exception:
        pass
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="EndstoneBot deferred Beta APIs patch helper")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()

    result = run_request(args.request, args.parent_pid)
    if result is None:
        return 0
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
