"""SQLite storage for users, profiles, workouts and their chat history."""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("WORKOUT_DB", Path(__file__).resolve().parent.parent / "data" / "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    ftp INTEGER,
    threshold_pace_s_per_km INTEGER,
    max_hr INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT 'New workout',
    sport TEXT,
    workout_json TEXT,
    messages_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_workouts_user ON workouts(user_id);
CREATE TABLE IF NOT EXISTS invites (
    code TEXT PRIMARY KEY,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    note TEXT,
    used_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    messages INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(workouts)")}
        if "source_filename" not in columns:
            # Name of the Zwift file a workout was imported from; Send to Zwift updates that file.
            conn.execute("ALTER TABLE workouts ADD COLUMN source_filename TEXT")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- users -----------------------------------------------------------------

def create_user(email: str, password_hash: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, password_hash)
        )
        return cur.lastrowid


def get_user_by_email(email: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def get_user(user_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def update_profile(user_id: int, ftp, threshold_pace_s_per_km, max_hr) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE users SET ftp = ?, threshold_pace_s_per_km = ?, max_hr = ? WHERE id = ?",
            (ftp, threshold_pace_s_per_km, max_hr, user_id),
        )


def set_password(user_id: int, password_hash: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))


# --- invites ---------------------------------------------------------------

def create_invite(code: str, created_by: int | None, note: str | None) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO invites (code, created_by, note) VALUES (?, ?, ?)", (code, created_by, note))


def list_invites():
    with connect() as conn:
        return conn.execute(
            """SELECT i.code, i.note, i.created_at, i.used_at, u.email AS used_by_email
               FROM invites i LEFT JOIN users u ON u.id = i.used_by
               ORDER BY i.created_at DESC, i.rowid DESC"""
        ).fetchall()


def create_user_with_invite(email: str, password_hash: str, code: str | None) -> int:
    """Create the user and, if a code is given, mark it used, in one transaction.

    Raises LookupError when the code doesn't exist or was already used.
    """
    with connect() as conn:
        if code is not None:
            cur = conn.execute(
                "UPDATE invites SET used_at = CURRENT_TIMESTAMP WHERE code = ? AND used_at IS NULL", (code,)
            )
            if cur.rowcount != 1:
                raise LookupError(code)
        user_id = conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, password_hash)
        ).lastrowid
        if code is not None:
            conn.execute("UPDATE invites SET used_by = ? WHERE code = ?", (user_id, code))
        return user_id


# --- usage -----------------------------------------------------------------

def messages_today(user_id: int, day: str) -> int:
    with connect() as conn:
        row = conn.execute("SELECT messages FROM usage WHERE user_id = ? AND day = ?", (user_id, day)).fetchone()
        return row["messages"] if row else 0


def cost_this_month(month: str) -> float:
    with connect() as conn:
        row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM usage WHERE day LIKE ?", (f"{month}-%",)).fetchone()
        return row["c"]


def record_usage(user_id: int, day: str, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO usage (user_id, day, messages, input_tokens, output_tokens, cost_usd)
               VALUES (?, ?, 1, ?, ?, ?)
               ON CONFLICT (user_id, day) DO UPDATE SET
                 messages = messages + 1,
                 input_tokens = input_tokens + excluded.input_tokens,
                 output_tokens = output_tokens + excluded.output_tokens,
                 cost_usd = cost_usd + excluded.cost_usd""",
            (user_id, day, input_tokens, output_tokens, cost_usd),
        )


# --- workouts --------------------------------------------------------------

def create_workout(user_id: int, workout: dict | None = None, source_filename: str | None = None) -> int:
    with connect() as conn:
        if workout is None:
            cur = conn.execute("INSERT INTO workouts (user_id) VALUES (?)", (user_id,))
        else:
            cur = conn.execute(
                "INSERT INTO workouts (user_id, name, sport, workout_json, source_filename) VALUES (?, ?, ?, ?, ?)",
                (user_id, workout["name"], workout["sport"], json.dumps(workout), source_filename),
            )
        return cur.lastrowid


def list_workouts(user_id: int, sports: tuple[str, ...]):
    """The user's workouts in the given sports, plus drafts that have no sport yet."""
    marks = ", ".join("?" * len(sports))
    with connect() as conn:
        return conn.execute(
            f"""SELECT id, name, sport, updated_at FROM workouts
                WHERE user_id = ? AND (sport IS NULL OR sport IN ({marks}))
                ORDER BY updated_at DESC, id DESC""",
            (user_id, *sports),
        ).fetchall()


def get_workout(user_id: int, workout_id: int):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM workouts WHERE id = ? AND user_id = ?", (workout_id, user_id)
        ).fetchone()


def save_workout(user_id: int, workout_id: int, workout: dict | None, messages: list) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE workouts
               SET name = COALESCE(?, name), sport = COALESCE(?, sport), workout_json = ?,
                   messages_json = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ?""",
            (
                workout.get("name") if workout else None,
                workout.get("sport") if workout else None,
                json.dumps(workout) if workout else None,
                json.dumps(messages),
                workout_id,
                user_id,
            ),
        )


def delete_workout(user_id: int, workout_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM workouts WHERE id = ? AND user_id = ?", (workout_id, user_id))
