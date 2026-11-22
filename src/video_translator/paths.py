"""Windows-safe path helpers (pathlib only)."""

from __future__ import annotations

import re
from pathlib import Path

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_MARKER = Path("config") / "default.yaml"


def _has_marker(directory: Path) -> bool:
    return (directory / _MARKER).is_file()


def project_root() -> Path:
    """Find the project root: walk up from the CWD, else the package's repo folder, else the CWD."""
    cwd = Path.cwd().resolve()
    for directory in (cwd, *cwd.parents):
        if _has_marker(directory):
            return directory
    package_root = Path(__file__).resolve().parents[2]
    if _has_marker(package_root):
        return package_root
    return cwd


def resolve_path(cfg_path: str | Path) -> Path:
    """Make a config path absolute (relative paths start at the project root)."""
    path = Path(cfg_path).expanduser()
    return path if path.is_absolute() else project_root() / path


def safe_name(name: str, replacement: str = "_") -> str:
    """Make ``name`` a valid single file or folder name on Windows, keeping Unicode characters."""
    cleaned = _ILLEGAL.sub(replacement, name).strip(" .")
    if not cleaned:
        return "unnamed"
    if cleaned.split(".")[0].strip().upper() in _RESERVED:
        cleaned = f"{replacement}{cleaned}"
    return cleaned


def job_dir(work_root: str | Path, job_id: str) -> Path:
    """Folder for one job's intermediate files, inside ``work_root``."""
    return resolve_path(work_root) / safe_name(job_id)
