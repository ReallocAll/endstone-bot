"""Safe, minimal Beta APIs patching for Bedrock level.dat.

This module is intentionally conservative:
- resolves a single world from server.properties; never scans worlds/ for guesses
- validates the complete little-endian NBT before and after patching
- rejects symlinks, duplicate tags, wrong tag types and header-length mismatches
- creates and verifies a standalone backup before touching level.dat
- writes through a same-directory temp file + fsync + os.replace
- rolls back automatically if post-write verification fails
"""

from __future__ import annotations

import copy
import hashlib
import os
import shutil
import stat
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from endstone_bot.nbt import (
    TAG_BYTE,
    TAG_COMPOUND,
    get_root_body_offset,
    make_byte_tag_bytes,
    make_compound_tag_bytes,
    parse_level_dat_header,
    read_bedrock_nbt,
    scan_compound_fields,
)

EXPERIMENTS_FIELDS: dict[str, int] = {
    "gametest": 1,
    "experiments_ever_used": 1,
    "saved_with_toggled_experiments": 1,
}

MAX_LEVEL_DAT_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class WorldResolution:
    level_dat: Path | None
    world_dir: Path | None
    server_root: Path | None
    level_name: str
    error: str = ""


@dataclass(frozen=True)
class PatchResult:
    status: str
    changed: bool
    message: str
    backup_path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"already-enabled", "patched"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_server_properties(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    text = path.read_text(encoding="utf-8-sig")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def resolve_level_dat_for_startup(cwd: Path) -> WorldResolution:
    """Resolve exactly one level.dat from fixed BDS roots.

    Supported layouts:
      <cwd>/server.properties + <cwd>/worlds/<level-name>/level.dat
      <cwd>/bedrock_server/server.properties + .../worlds/<level-name>/level.dat

    We deliberately do not scan worlds/ and do not choose a directory by recency.
    """

    roots: list[Path] = []
    for raw in (cwd, cwd / "bedrock_server"):
        resolved = raw.resolve()
        if resolved not in roots:
            roots.append(resolved)

    matches: list[tuple[Path, Path, Path, str]] = []
    errors: list[str] = []

    for root in roots:
        props_path = root / "server.properties"
        if not props_path.is_file():
            continue
        try:
            props = _read_server_properties(props_path)
        except Exception as exc:
            errors.append(f"{props_path}: 无法读取 ({exc})")
            continue

        level_name = str(props.get("level-name", "")).strip()
        if not level_name:
            errors.append(f"{props_path}: 缺少 level-name")
            continue

        worlds_root = (root / "worlds").resolve()
        world_dir = (worlds_root / level_name).resolve()
        try:
            world_dir.relative_to(worlds_root)
        except ValueError:
            errors.append(f"{props_path}: level-name 越出 worlds 目录")
            continue

        level_dat = world_dir / "level.dat"
        if level_dat.is_file():
            matches.append((level_dat.resolve(), world_dir, root, level_name))
        else:
            errors.append(f"{level_dat}: 不存在")

    unique: dict[Path, tuple[Path, Path, Path, str]] = {}
    for item in matches:
        unique[item[0]] = item

    if len(unique) == 1:
        level_dat, world_dir, root, level_name = next(iter(unique.values()))
        return WorldResolution(level_dat, world_dir, root, level_name)

    if len(unique) > 1:
        joined = ", ".join(str(path) for path in unique)
        return WorldResolution(
            None, None, None, "",
            f"检测到多个可能的 BDS 世界，拒绝自动修改: {joined}",
        )

    detail = "; ".join(errors) if errors else "固定位置中没有找到 server.properties"
    return WorldResolution(None, None, None, "", detail)


def _validate_level_dat_bytes(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_LEVEL_DAT_BYTES:
        raise ValueError(f"level.dat 过大，拒绝修改: {len(raw)} bytes")
    parse_level_dat_header(raw)
    return read_bedrock_nbt(raw, has_header=True, strict=True)


def _build_patch(raw: bytes) -> bytes | None:
    before = _validate_level_dat_bytes(raw)
    root_body = get_root_body_offset(raw, has_header=True)
    root_fields, root_end = scan_compound_fields(raw, root_body)

    patches: list[tuple[int, int, bytes]] = []
    experiments_info = root_fields.get("experiments")

    if experiments_info is None:
        body = b"".join(make_byte_tag_bytes(name, value) for name, value in EXPERIMENTS_FIELDS.items())
        patches.append((root_end, 0, make_compound_tag_bytes("experiments", body)))
    else:
        if experiments_info.tag_type != TAG_COMPOUND:
            raise ValueError("level.dat 的 experiments 字段存在但不是 Compound，拒绝修改")

        exp_fields, exp_end = scan_compound_fields(raw, experiments_info.payload_offset)
        for name, wanted in EXPERIMENTS_FIELDS.items():
            info = exp_fields.get(name)
            if info is None:
                patches.append((exp_end, 0, make_byte_tag_bytes(name, wanted)))
                continue
            if info.tag_type != TAG_BYTE:
                raise ValueError(f"experiments.{name} 存在但不是 Byte，拒绝修改")
            current = struct.unpack("<b", raw[info.payload_offset:info.payload_offset + 1])[0]
            if current != wanted:
                patches.append((info.payload_offset, 1, struct.pack("<b", wanted)))

    if not patches:
        experiments = before.get("experiments")
        if not isinstance(experiments, dict):
            raise ValueError("experiments 解析结果异常")
        if all(experiments.get(name) == value for name, value in EXPERIMENTS_FIELDS.items()):
            return None
        raise ValueError("Beta APIs 状态异常，拒绝无补丁写入")

    result = bytearray(raw)
    for offset, delete_len, insert_bytes in sorted(patches, key=lambda item: item[0], reverse=True):
        result[offset:offset + delete_len] = insert_bytes

    struct.pack_into("<I", result, 4, len(result) - 8)
    candidate = bytes(result)
    after = _validate_level_dat_bytes(candidate)

    expected = copy.deepcopy(before)
    experiments = expected.get("experiments")
    if experiments is None:
        experiments = {}
        expected["experiments"] = experiments
    if not isinstance(experiments, dict):
        raise ValueError("experiments 语义结构异常")
    experiments.update(EXPERIMENTS_FIELDS)

    if after != expected:
        raise ValueError("补丁后的 NBT 除实验字段外发生了语义变化，拒绝写入")

    return candidate


def is_beta_apis_enabled(level_dat_path: Path) -> bool:
    try:
        raw = level_dat_path.read_bytes()
        data = _validate_level_dat_bytes(raw)
        experiments = data.get("experiments", {})
        return (
            isinstance(experiments, dict)
            and all(experiments.get(name) == value for name, value in EXPERIMENTS_FIELDS.items())
        )
    except Exception:
        return False


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    tmp = path.with_name(path.name + ".endstone-bot.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, stat.S_IMODE(mode))
        except OSError:
            pass
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _create_verified_backup(
    level_dat_path: Path,
    original: bytes,
    backup_root: Path,
    world_name: str,
    mode: int,
) -> Path:
    safe_world = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in world_name) or "world"
    target_dir = backup_root / safe_world
    target_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = _sha256(original)
    backup = target_dir / f"level.dat.{stamp}.{digest[:12]}.bak"
    counter = 1
    while backup.exists():
        backup = target_dir / f"level.dat.{stamp}.{digest[:12]}.{counter}.bak"
        counter += 1

    tmp = backup.with_suffix(backup.suffix + ".tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, stat.S_IMODE(mode))
        except OSError:
            pass
        os.replace(tmp, backup)
        _fsync_dir(target_dir)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

    if _sha256(backup.read_bytes()) != digest:
        backup.unlink(missing_ok=True)
        raise IOError("level.dat 备份校验失败")
    return backup


def _prune_backups(backup_root: Path, world_name: str, keep: int) -> None:
    keep = max(1, min(20, int(keep)))
    safe_world = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in world_name) or "world"
    folder = backup_root / safe_world
    if not folder.is_dir():
        return
    backups = sorted(
        folder.glob("level.dat.*.bak"),
        key=lambda p: p.stat().st_mtime_ns,
        reverse=True,
    )
    for old in backups[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def enable_beta_apis_safely(
    level_dat_path: Path,
    *,
    backup_root: Path,
    world_name: str,
    backup_keep: int = 5,
) -> PatchResult:
    """Enable Beta APIs with strict validation, atomic replacement and rollback."""

    try:
        if level_dat_path.is_symlink():
            return PatchResult("rejected", False, "level.dat 是符号链接，拒绝自动修改")

        st = level_dat_path.stat()
        if not stat.S_ISREG(st.st_mode):
            return PatchResult("rejected", False, "level.dat 不是普通文件，拒绝自动修改")

        original = level_dat_path.read_bytes()
        original_hash = _sha256(original)
        candidate = _build_patch(original)
        if candidate is None:
            return PatchResult("already-enabled", False, "Beta APIs 已启用")

        # TOCTOU guard: if anything changed the file after our initial read, abort.
        current = level_dat_path.read_bytes()
        if _sha256(current) != original_hash:
            return PatchResult("rejected", False, "level.dat 在校验期间发生变化，拒绝写入")

        backup = _create_verified_backup(
            level_dat_path,
            original,
            backup_root,
            world_name,
            st.st_mode,
        )

        try:
            _atomic_write(level_dat_path, candidate, st.st_mode)
            written = level_dat_path.read_bytes()
            if _sha256(written) != _sha256(candidate):
                raise IOError("原子替换后 SHA-256 不匹配")
            if not is_beta_apis_enabled(level_dat_path):
                raise IOError("原子替换后 Beta APIs 验证失败")
        except Exception as write_error:
            rollback_error: Exception | None = None
            try:
                _atomic_write(level_dat_path, original, st.st_mode)
                if _sha256(level_dat_path.read_bytes()) != original_hash:
                    raise IOError("回滚后的 level.dat 与原文件 SHA-256 不一致")
            except Exception as exc:
                rollback_error = exc

            if rollback_error is not None:
                return PatchResult(
                    "rollback-failed",
                    False,
                    f"写入失败且自动回滚失败: write={write_error}; rollback={rollback_error}; 备份={backup}",
                    backup,
                )
            return PatchResult(
                "rolled-back",
                False,
                f"写入后验证失败，已恢复原文件: {write_error}",
                backup,
            )

        _prune_backups(backup_root, world_name, backup_keep)
        return PatchResult(
            "patched",
            True,
            "已安全启用 Beta APIs",
            backup,
        )

    except FileNotFoundError:
        return PatchResult("rejected", False, f"level.dat 不存在: {level_dat_path}")
    except Exception as exc:
        return PatchResult("rejected", False, f"安全校验未通过，未修改 level.dat: {exc}")
