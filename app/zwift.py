"""Save .zwo files straight into the local Zwift workouts folder.

Only works when the app runs on the same computer as Zwift. Zwift reads custom workouts
from Documents/Zwift/Workouts/<Zwift user ID>/ when it starts.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

from . import config

ZWIFT_ROOT = Path.home() / "Documents" / "Zwift" / "Workouts"
# Files we wrote; "Workout Builder" was the app's name before VibeGrind.
OUR_AUTHORS = (b"<author>VibeGrind</author>", b"<author>Workout Builder</author>")


class ZwiftFolderError(RuntimeError):
    pass


def workouts_dir() -> Path | None:
    """The Zwift user folder: ZWIFT_WORKOUTS_DIR if set, else the single numeric folder found."""
    if config.hosted():
        return None  # a server can't reach the user's computer
    override = os.environ.get("ZWIFT_WORKOUTS_DIR")
    if override:
        path = Path(override).expanduser()
        return path if path.is_dir() else None
    if not ZWIFT_ROOT.is_dir():
        return None
    candidates = [p for p in ZWIFT_ROOT.iterdir() if p.is_dir() and p.name.isdigit()]
    if len(candidates) == 1:
        return candidates[0]
    return None


def status() -> dict:
    folder = workouts_dir()
    return {"available": folder is not None, "folder": str(folder) if folder else None}


def _require_dir() -> Path:
    folder = workouts_dir()
    if folder is None and config.hosted():
        raise ZwiftFolderError("The online version can't reach your Zwift folder. Download the .zwo file instead.")
    if folder is None:
        raise ZwiftFolderError(
            "Couldn't find your Zwift workouts folder. Open Zwift once on this computer, "
            "or set ZWIFT_WORKOUTS_DIR in .env."
        )
    return folder


def _file_in(folder: Path, filename: str) -> Path:
    """Resolve a bare .zwo file name inside the folder, refusing anything path-like."""
    if Path(filename).name != filename or not filename.lower().endswith(".zwo"):
        raise ZwiftFolderError("Invalid workout file name.")
    return folder / filename


def list_files() -> list[Path]:
    folder = _require_dir()
    return sorted((p for p in folder.glob("*.zwo") if p.is_file()), key=lambda p: p.name.lower())


def read(filename: str) -> bytes:
    path = _file_in(_require_dir(), filename)
    if not path.is_file():
        raise ZwiftFolderError(f"{filename} is no longer in your Zwift folder.")
    return path.read_bytes()


def replace(filename: str, data: bytes, backup_dir: Path) -> tuple[Path, Path | None]:
    """Overwrite a workout in the Zwift folder, copying the old version to backup_dir first."""
    path = _file_in(_require_dir(), filename)
    backup = None
    if path.is_file():
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = backup_dir / f"{path.stem}.{stamp}.zwo"
        shutil.copy2(path, backup)
    path.write_bytes(data)
    return path, backup


def save(data: bytes, filename: str) -> Path:
    """Write the file, replacing an earlier export of ours but never someone else's file."""
    folder = _require_dir()
    stem, ext = os.path.splitext(filename)
    target = folder / filename
    n = 1
    while target.exists() and not any(a in target.read_bytes()[:500] for a in OUR_AUTHORS):
        target = folder / f"{stem}_{n}{ext}"
        n += 1
    target.write_bytes(data)
    return target
