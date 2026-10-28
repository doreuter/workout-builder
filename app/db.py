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
    lthr_run INTEGER,   -- threshold heart rates (bpm)
    lthr_bike INTEGER,
    last_seen_at TEXT,  -- UTC, updated at most hourly
    training_rules TEXT,  -- the athlete's own rules for Claude, free text
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT 'New workout',
    sport TEXT,
    workout_json TEXT,
    messages_json TEXT NOT NULL DEFAULT '[]',
    planned_date TEXT,  -- YYYY-MM-DD when it's on the calendar
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_workouts_user ON workouts(user_id);
CREATE TABLE IF NOT EXISTS plan_chats (  -- one planning conversation per user
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    messages_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS invites (
    code TEXT PRIMARY KEY,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    note TEXT,
    used_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS garmin_days (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    hrv_last_night INTEGER,
    hrv_weekly_avg INTEGER,
    hrv_low_upper INTEGER,
    hrv_balanced_low INTEGER,
    hrv_balanced_upper INTEGER,
    hrv_status TEXT,
    resting_hr INTEGER,
    sleep_seconds INTEGER,
    deep_seconds INTEGER,
    light_seconds INTEGER,
    rem_seconds INTEGER,
    awake_seconds INTEGER,
    sleep_score INTEGER,
    sleep_qualifier TEXT,
    bb_max INTEGER,
    bb_min INTEGER,
    bb_charged INTEGER,
    bb_drained INTEGER,
    stress_avg INTEGER,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS garmin_activities (
    activity_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    start_local TEXT,
    name TEXT,
    type_key TEXT,
    duration_s REAL,
    avg_power REAL,
    norm_power REAL,
    tss REAL,
    intensity_factor REAL,
    training_load REAL,
    aerobic_te REAL,
    anaerobic_te REAL,
    te_label TEXT,
    vo2max REAL,
    avg_hr REAL,
    avg_speed REAL,  -- m/s
    gap_speed REAL,  -- grade-adjusted, m/s (runs)
    recovery_min INTEGER,  -- Garmin's recovery time from the activity file...
    recovery_from TEXT,    -- ...counted down from this moment (UTC)
    recovery_checked INTEGER
);
CREATE INDEX IF NOT EXISTS idx_garmin_activities_day ON garmin_activities(user_id, day);
CREATE TABLE IF NOT EXISTS garmin_sync (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    synced_at TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY,       -- SHA-256 of the emailed token; the token itself is never stored
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    name TEXT PRIMARY KEY,
    last_run_at TEXT,
    last_ok_at TEXT,
    last_error TEXT
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
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(garmin_activities)")}
        for name, kind in (("recovery_min", "INTEGER"), ("recovery_from", "TEXT"), ("recovery_checked", "INTEGER"),
                           ("avg_speed", "REAL"), ("gap_speed", "REAL")):
            if name not in columns:
                conn.execute(f"ALTER TABLE garmin_activities ADD COLUMN {name} {kind}")
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        for name, kind in (("lthr_run", "INTEGER"), ("lthr_bike", "INTEGER"), ("last_seen_at", "TEXT"),
                           ("training_rules", "TEXT")):
            if name not in columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {kind}")
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(workouts)")}
        if "planned_date" not in columns:
            conn.execute("ALTER TABLE workouts ADD COLUMN planned_date TEXT")


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


def update_profile(user_id: int, ftp, threshold_pace_s_per_km, lthr_run=None, lthr_bike=None,
                   training_rules=None) -> None:
    # max_hr is no longer edited (threshold heart rates replaced it); a stored value is kept as it was.
    with connect() as conn:
        conn.execute(
            """UPDATE users SET ftp = ?, threshold_pace_s_per_km = ?, lthr_run = ?, lthr_bike = ?, training_rules = ?
               WHERE id = ?""",
            (ftp, threshold_pace_s_per_km, lthr_run, lthr_bike, training_rules, user_id),
        )


def touch_user(user_id: int, now: str, stale_before: str) -> None:
    """Record activity, at most once an hour (cheap enough to call on every request)."""
    with connect() as conn:
        conn.execute(
            "UPDATE users SET last_seen_at = ? WHERE id = ? AND (last_seen_at IS NULL OR last_seen_at < ?)",
            (now, user_id, stale_before),
        )


def admin_users():
    """Every account with what the admin page shows about it."""
    with connect() as conn:
        return conn.execute(
            """SELECT u.id, u.email, u.created_at, u.last_seen_at, u.ftp,
                      (SELECT COUNT(*) FROM workouts w WHERE w.user_id = u.id AND w.workout_json IS NOT NULL) AS workouts,
                      (SELECT COUNT(*) FROM workouts w WHERE w.user_id = u.id AND w.planned_date IS NOT NULL) AS planned,
                      (SELECT COALESCE(SUM(messages), 0) FROM usage g WHERE g.user_id = u.id
                         AND g.day >= date('now', '-30 days')) AS messages_30d,
                      (SELECT COALESCE(SUM(cost_usd), 0) FROM usage g WHERE g.user_id = u.id
                         AND g.day >= date('now', '-30 days')) AS cost_30d,
                      (SELECT code FROM invites i WHERE i.used_by = u.id) AS invite_code
               FROM users u ORDER BY u.created_at DESC, u.id DESC"""
        ).fetchall()


def admin_stats(since: str) -> dict:
    """Totals for the admin page, plus per-day series from `since` (YYYY-MM-DD) on."""
    with connect() as conn:
        one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
        return {
            "users": one("SELECT COUNT(*) FROM users"),
            "users_new_30d": one("SELECT COUNT(*) FROM users WHERE created_at >= datetime('now', '-30 days')"),
            "active_7d": one("SELECT COUNT(*) FROM users WHERE last_seen_at >= datetime('now', '-7 days')"),
            "active_30d": one("SELECT COUNT(*) FROM users WHERE last_seen_at >= datetime('now', '-30 days')"),
            "workouts": one("SELECT COUNT(*) FROM workouts WHERE workout_json IS NOT NULL"),
            "workouts_30d": one("SELECT COUNT(*) FROM workouts WHERE workout_json IS NOT NULL "
                                "AND created_at >= datetime('now', '-30 days')"),
            "planned": one("SELECT COUNT(*) FROM workouts WHERE planned_date IS NOT NULL"),
            "messages_30d": one("SELECT COALESCE(SUM(messages), 0) FROM usage WHERE day >= date('now', '-30 days')"),
            "by_day": [dict(r) for r in conn.execute(
                """SELECT day, SUM(messages) AS messages, COUNT(DISTINCT user_id) AS users
                   FROM usage WHERE day >= ? GROUP BY day ORDER BY day""", (since,)).fetchall()],
            "workouts_by_day": [dict(r) for r in conn.execute(
                """SELECT date(created_at) AS day, COUNT(*) AS workouts FROM workouts
                   WHERE workout_json IS NOT NULL AND date(created_at) >= ? GROUP BY day""", (since,)).fetchall()],
        }


def set_password(user_id: int, password_hash: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))


def delete_user(user_id: int) -> None:
    """Delete an account. Foreign keys cascade to workouts, usage, Garmin data and reset tokens;
    invites the user created or used are kept but no longer point to them."""
    with connect() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def export_user(user_id: int) -> dict:
    """Everything stored about one user, for "Download my data" (no password hash)."""
    with connect() as conn:
        def rows(sql):
            return [dict(r) for r in conn.execute(sql, (user_id,)).fetchall()]
        user = dict(conn.execute(
            "SELECT email, ftp, threshold_pace_s_per_km, max_hr, lthr_run, lthr_bike, training_rules, created_at, last_seen_at "
            "FROM users WHERE id = ?", (user_id,)
        ).fetchone())
        return {
            "account": user,
            "invite_used": rows("SELECT code, used_at FROM invites WHERE used_by = ?"),
            "workouts": rows("SELECT id, name, sport, workout_json, messages_json, source_filename, planned_date, "
                             "created_at, updated_at FROM workouts WHERE user_id = ? ORDER BY id"),
            "plan_chat": rows("SELECT messages_json FROM plan_chats WHERE user_id = ?"),
            "usage": rows("SELECT day, messages, input_tokens, output_tokens, cost_usd FROM usage WHERE user_id = ? ORDER BY day"),
            "garmin_days": rows("SELECT * FROM garmin_days WHERE user_id = ? ORDER BY day"),
            "garmin_activities": rows("SELECT * FROM garmin_activities WHERE user_id = ? ORDER BY start_local"),
        }


# --- password reset --------------------------------------------------------

def create_password_reset(user_id: int, token_hash: str, expires_at: str) -> None:
    with connect() as conn:
        # Only the newest link works: older unused ones are dropped.
        conn.execute("DELETE FROM password_resets WHERE user_id = ? AND used_at IS NULL", (user_id,))
        conn.execute(
            "INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_hash, user_id, expires_at),
        )


def use_password_reset(token_hash: str, now: str, password_hash: str) -> bool:
    """Set the new password if the token is valid, unused and not expired; mark it used. Atomic."""
    with connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM password_resets WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
            (token_hash, now),
        ).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE password_resets SET used_at = ? WHERE token_hash = ?", (now, token_hash))
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, row["user_id"]))
        return True


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


# --- Garmin ----------------------------------------------------------------

GARMIN_DAY_FIELDS = (
    "hrv_last_night", "hrv_weekly_avg", "hrv_low_upper", "hrv_balanced_low", "hrv_balanced_upper", "hrv_status",
    "resting_hr", "sleep_seconds", "deep_seconds", "light_seconds", "rem_seconds", "awake_seconds",
    "sleep_score", "sleep_qualifier", "bb_max", "bb_min", "bb_charged", "bb_drained", "stress_avg",
)
GARMIN_ACTIVITY_FIELDS = (
    "day", "start_local", "name", "type_key", "duration_s", "avg_power", "norm_power", "tss",
    "intensity_factor", "training_load", "aerobic_te", "anaerobic_te", "te_label", "vo2max", "avg_hr",
    "avg_speed", "gap_speed",
)


def upsert_garmin_day(user_id: int, day: str, values: dict) -> None:
    """Merge one day's values; fields not given (or None) keep what's stored."""
    cols = [k for k in values if k in GARMIN_DAY_FIELDS]
    if not cols:
        return
    sets = ", ".join(f"{c} = COALESCE(excluded.{c}, {c})" for c in cols)
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO garmin_days (user_id, day, {", ".join(cols)})
                VALUES (?, ?, {", ".join("?" * len(cols))})
                ON CONFLICT (user_id, day) DO UPDATE SET {sets}""",
            (user_id, day, *(values[c] for c in cols)),
        )


def upsert_garmin_activity(user_id: int, activity_id: int, values: dict) -> None:
    cols = list(GARMIN_ACTIVITY_FIELDS)
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO garmin_activities (activity_id, user_id, {", ".join(cols)})
                VALUES (?, ?, {", ".join("?" * len(cols))})
                ON CONFLICT (activity_id) DO UPDATE SET {", ".join(f"{c} = excluded.{c}" for c in cols)}""",
            (activity_id, user_id, *(values.get(c) for c in cols)),
        )


def garmin_oldest_run_without_speed(user_id: int, since: str) -> str | None:
    """Day of the oldest run stored before pace was kept, so the next sync re-reads from there."""
    with connect() as conn:
        row = conn.execute(
            """SELECT MIN(day) AS day FROM garmin_activities
               WHERE user_id = ? AND day >= ? AND type_key LIKE '%running%' AND avg_speed IS NULL""",
            (user_id, since),
        ).fetchone()
    return row["day"]


def garmin_latest_ride_with_power(user_id: int) -> int | None:
    with connect() as conn:
        row = conn.execute(
            """SELECT activity_id FROM garmin_activities WHERE user_id = ? AND norm_power IS NOT NULL
               AND (type_key LIKE '%cycling%' OR type_key LIKE '%biking%' OR type_key LIKE '%ride%')
               ORDER BY start_local DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
    return row["activity_id"] if row else None


def garmin_activities_without_recovery(user_id: int, since: str) -> list[int]:
    """Activities whose file hasn't been checked for a recovery time yet."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT activity_id FROM garmin_activities WHERE user_id = ? AND day >= ? AND recovery_checked IS NULL",
            (user_id, since),
        ).fetchall()
    return [r["activity_id"] for r in rows]


def set_garmin_recovery(activity_id: int, minutes: int | None, since: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE garmin_activities SET recovery_min = ?, recovery_from = ?, recovery_checked = 1 WHERE activity_id = ?",
            (minutes, since, activity_id),
        )


def garmin_days(user_id: int, since: str):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM garmin_days WHERE user_id = ? AND day >= ? ORDER BY day", (user_id, since)
        ).fetchall()


def garmin_activities(user_id: int, since: str):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM garmin_activities WHERE user_id = ? AND day >= ? ORDER BY start_local", (user_id, since)
        ).fetchall()


def garmin_latest_day(user_id: int) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT MAX(day) AS d FROM garmin_days WHERE user_id = ?", (user_id,)).fetchone()
        return row["d"]


def garmin_sync_state(user_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM garmin_sync WHERE user_id = ?", (user_id,)).fetchone()


def set_garmin_sync_state(user_id: int, synced_at: str | None, error: str | None) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO garmin_sync (user_id, synced_at, error) VALUES (?, ?, ?)
               ON CONFLICT (user_id) DO UPDATE SET
                 synced_at = COALESCE(excluded.synced_at, synced_at), error = excluded.error""",
            (user_id, synced_at, error),
        )


# --- usage -----------------------------------------------------------------

def messages_today(user_id: int, day: str) -> int:
    with connect() as conn:
        row = conn.execute("SELECT messages FROM usage WHERE user_id = ? AND day = ?", (user_id, day)).fetchone()
        return row["messages"] if row else 0


def cost_this_month(month: str) -> float:
    with connect() as conn:
        row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM usage WHERE day LIKE ?", (f"{month}-%",)).fetchone()
        return row["c"]


def job_state(name: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE name = ?", (name,)).fetchone()


def set_job_state(name: str, run_at: str, ok: bool, error: str | None) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO jobs (name, last_run_at, last_ok_at, last_error) VALUES (?, ?, ?, ?)
               ON CONFLICT (name) DO UPDATE SET last_run_at = excluded.last_run_at,
                 last_ok_at = COALESCE(excluded.last_ok_at, last_ok_at), last_error = excluded.last_error""",
            (name, run_at, run_at if ok else None, error),
        )


def purge_old_data(usage_before: str, resets_before: str) -> None:
    """Retention clean-up: usage counters older than the cut-off day, and spent or expired reset links."""
    with connect() as conn:
        conn.execute("DELETE FROM usage WHERE day < ?", (usage_before,))
        conn.execute("DELETE FROM password_resets WHERE expires_at < ? OR used_at IS NOT NULL", (resets_before,))


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
            f"""SELECT id, name, sport, planned_date, updated_at FROM workouts
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


# --- calendar --------------------------------------------------------------

def set_planned_date(user_id: int, workout_id: int, day: str | None) -> None:
    with connect() as conn:
        conn.execute("UPDATE workouts SET planned_date = ? WHERE id = ? AND user_id = ?", (day, workout_id, user_id))


def planned_workouts(user_id: int, start: str, end: str):
    with connect() as conn:
        return conn.execute(
            """SELECT id, name, sport, planned_date, workout_json FROM workouts
               WHERE user_id = ? AND planned_date BETWEEN ? AND ? ORDER BY planned_date, id""",
            (user_id, start, end),
        ).fetchall()


def plan_chat(user_id: int) -> list:
    with connect() as conn:
        row = conn.execute("SELECT messages_json FROM plan_chats WHERE user_id = ?", (user_id,)).fetchone()
    return json.loads(row["messages_json"]) if row else []


def save_plan_chat(user_id: int, messages: list) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO plan_chats (user_id, messages_json) VALUES (?, ?)
               ON CONFLICT (user_id) DO UPDATE SET messages_json = excluded.messages_json""",
            (user_id, json.dumps(messages)),
        )
