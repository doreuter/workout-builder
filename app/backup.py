"""Database backup and one-time restore, for moving the app to a new server.

Backup: a consistent snapshot of the live SQLite database (SQLite's online backup API, safe while
the app is running).

Restore: deliberately hard to trigger. It only works when the server has ALLOW_RESTORE=1, only for an
admin, and only into a database that has no real data yet (at most the one admin account created to
run the restore, and no workouts). The uploaded file must be a healthy SQLite database with the
app's tables. Turn ALLOW_RESTORE off again right after the move.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from . import db

REQUIRED_TABLES = {"users", "workouts", "invites"}
MAX_BACKUP_BYTES = 200 * 1024 * 1024
SQLITE_HEADER = b"SQLite format 3\x00"


class RestoreError(ValueError):
    pass


def restore_allowed() -> bool:
    return os.environ.get("ALLOW_RESTORE") == "1"


def snapshot() -> Path:
    """Write a consistent copy of the live database to a temporary file and return its path."""
    fd, name = tempfile.mkstemp(prefix="vibegrind-backup-", suffix=".sqlite3")
    os.close(fd)
    src = sqlite3.connect(db.DB_PATH)
    dest = sqlite3.connect(name)
    try:
        src.backup(dest)
    finally:
        dest.close()
        src.close()
    return Path(name)


def target_is_empty() -> bool:
    """True when the live database holds no real data yet (at most one account, no workouts)."""
    with db.connect() as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        workouts = conn.execute("SELECT COUNT(*) FROM workouts WHERE workout_json IS NOT NULL").fetchone()[0]
    return users <= 1 and workouts == 0


def validate_backup(path: Path) -> dict:
    """Check the uploaded file is a healthy app database. Returns a short summary of its contents."""
    with open(path, "rb") as f:
        if f.read(16) != SQLITE_HEADER:
            raise RestoreError("That file isn't a VibeGrind database backup.")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RestoreError("The backup file is damaged (integrity check failed).")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = REQUIRED_TABLES - tables
        if missing:
            raise RestoreError(f"The backup is missing the app's tables ({', '.join(sorted(missing))}).")
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        workouts = conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0]
    except sqlite3.DatabaseError:
        raise RestoreError("That file isn't a readable SQLite database.")
    finally:
        conn.close()
    if users == 0:
        raise RestoreError("The backup contains no accounts, so there's nothing to restore.")
    return {"users": users, "workouts": workouts}


def restore(path: Path) -> dict:
    """Replace the live database with the backup, after all checks pass."""
    if not restore_allowed():
        raise RestoreError("Restoring is switched off on this server (set ALLOW_RESTORE=1 to allow it).")
    if not target_is_empty():
        raise RestoreError("This server already has data. Restoring would overwrite it, so it's blocked.")
    summary = validate_backup(path)
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    dest = sqlite3.connect(db.DB_PATH)
    try:
        src.backup(dest)  # atomic from SQLite's point of view
    finally:
        dest.close()
        src.close()
    db.init_db()  # apply any schema additions the backup's app version didn't have yet
    return summary
