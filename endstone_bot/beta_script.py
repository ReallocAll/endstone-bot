"""Generate the explicit, self-contained offline Beta APIs patch script."""

from __future__ import annotations

from pathlib import Path


PATCH_SCRIPT_NAME = "enable_beta.py"
_TEMPLATE_PATH = Path(__file__).with_name("enable_beta_template.py")


def render_patch_script(level_dat: Path, world_name: str, *, backup_keep: int = 5) -> str:
    level_dat = Path(level_dat).resolve()
    world_name = str(world_name)
    backup_keep = max(1, min(20, int(backup_keep)))

    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template
        .replace("__LEVEL_DAT_LITERAL__", repr(str(level_dat)))
        .replace("__WORLD_NAME_LITERAL__", repr(world_name))
        .replace("__BACKUP_KEEP_LITERAL__", str(backup_keep))
    )


def write_patch_script(data_folder: Path, world_dir: Path, *, backup_keep: int = 5) -> Path:
    data_folder = Path(data_folder)
    world_dir = Path(world_dir).resolve()
    level_dat = world_dir / "level.dat"
    script_path = data_folder / PATCH_SCRIPT_NAME

    data_folder.mkdir(parents=True, exist_ok=True)
    content = render_patch_script(level_dat, world_dir.name, backup_keep=backup_keep)
    tmp = script_path.with_suffix(".py.tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    tmp.replace(script_path)
    return script_path
