"""FastAPI app: accounts, workouts, chat and exports."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import anthropic
from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from . import backup, config, db, garmin, llm, mailer, maintenance, offsite, readiness, security, zwift
from .auth import current_user, hash_password, start_session, verify_password
from .exporters import ExportError, safe_filename, to_fit, to_zwo
from .importers import MAX_ZWO_BYTES, ImportError_, from_fit, from_zwo
from .workout import ENABLED_SPORTS, WorkoutError, flatten, stats, validate

logger = logging.getLogger("vibegrind")
ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
ZWIFT_BACKUPS = db.DB_PATH.parent / "zwift-backups"


def _session_secret() -> str:
    if os.environ.get("SESSION_SECRET"):
        return os.environ["SESSION_SECRET"]
    path = db.DB_PATH.parent / ".session_secret"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32))
    return path.read_text().strip()


db.init_db()
maintenance.purge()  # retention clean-up on every start; the thread repeats it
maintenance.start()
app = FastAPI(title="VibeGrind")
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret(),
    same_site="lax",
    # Online, the session cookie is only ever sent over HTTPS.
    https_only=os.environ.get("HTTPS_ONLY", "1" if config.hosted() else "0") == "1",
    max_age=30 * 24 * 3600,
)
app.add_middleware(security.SecurityMiddleware)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def landing():
    return FileResponse(STATIC / "landing.html")


@app.get("/app")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/admin")
def admin_page():
    return FileResponse(STATIC / "admin.html")  # the page itself is public; its data needs an admin session


# --- accounts ---------------------------------------------------------------

class Credentials(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)


class Registration(Credentials):
    invite_code: str | None = Field(default=None, max_length=64)


class Profile(BaseModel):
    ftp: int | None = Field(default=None, ge=50, le=700)
    threshold_pace_s_per_km: int | None = Field(default=None, ge=120, le=900)
    lthr_run: int | None = Field(default=None, ge=80, le=230)
    lthr_bike: int | None = Field(default=None, ge=80, le=230)
    training_rules: str | None = Field(default=None, max_length=1000)
    endurance_ceiling_pct: int | None = Field(default=None, ge=40, le=85)


def _is_admin(user) -> bool:
    return user["email"] in config.admin_emails()


def _user_json(user) -> dict:
    out = {
        "email": user["email"],
        "ftp": user["ftp"],
        "threshold_pace_s_per_km": user["threshold_pace_s_per_km"],
        "lthr_run": user["lthr_run"],
        "lthr_bike": user["lthr_bike"],
        "training_rules": user["training_rules"],
        "endurance_ceiling_pct": user["endurance_ceiling_pct"],
        "is_admin": _is_admin(user),
        "messages_left_today": _messages_left(user),
        "garmin": garmin.is_owner(user),
    }
    if out["is_admin"] or out["garmin"]:
        out["garmin_status"] = garmin.status(user)  # shown in Settings to help set it up
    return out


@app.get("/api/config")
def public_config():
    """What the sign-in page needs to know before anyone is signed in."""
    return {"hosted": config.hosted(), "invites_required": config.invites_required(),
            "password_reset": mailer.available(), "backup_keep_days": offsite.keep_days()}


@app.get("/healthz")
def healthz():
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


@app.post("/api/register")
def register(body: Registration, request: Request):
    security.check_rate(request, "register", limit=5, window_s=3600)
    email = body.email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "Enter a valid email address.")
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    # When invites are required, everyone needs an unused invite code, except the very first admin:
    # an address in ADMIN_EMAILS may sign up without one only while no admin account exists yet. Emails
    # aren't verified, so otherwise anyone who knows a newly added admin address could claim it first.
    admins = config.admin_emails()
    code = (body.invite_code or "").strip().upper() or None
    first_admin = email in admins and not db.any_user_with_email(admins)
    if not config.invites_required() or first_admin:
        code = None
    elif not code:
        raise HTTPException(400, "You need an invite code to create an account.")
    try:
        user_id = db.create_user_with_invite(email, hash_password(body.password), code)
    except LookupError:
        raise HTTPException(400, "That invite code is invalid or has already been used.")
    except sqlite3.IntegrityError:
        raise HTTPException(400, "An account with this email already exists.")
    user = db.get_user(user_id)
    start_session(request, user)
    return _user_json(user)


@app.post("/api/login")
def login(body: Credentials, request: Request):
    email = body.email.strip().lower()
    security.check_rate(request, "login", limit=10, window_s=900)
    # Per account too, which no faked IP address gets around. Only wrong passwords count, but once
    # there are too many, even the right one waits, so the answer can't confirm a guess.
    security.check_rate(request, "login-email", limit=10, window_s=900, key=email, count=False)
    user = db.get_user_by_email(email)
    if not user or not verify_password(body.password, user["password_hash"]):
        security.count_hit("login-email", email)
        raise HTTPException(401, "Wrong email or password.")
    start_session(request, user)
    return _user_json(user)


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return _user_json(user)


@app.put("/api/me/profile")
def update_profile(body: Profile, user=Depends(current_user)):
    db.update_profile(user["id"], body.ftp, body.threshold_pace_s_per_km, body.lthr_run, body.lthr_bike,
                      (body.training_rules or "").strip() or None, body.endurance_ceiling_pct)
    return _user_json(db.get_user(user["id"]))


@app.get("/api/me/garmin-thresholds")
def garmin_thresholds(user=Depends(current_user)):
    """Garmin's FTP and threshold heart rates, for the Settings form to fill in (not saved here)."""
    try:
        return garmin.thresholds(user)
    except garmin.GarminError as e:
        raise HTTPException(400, str(e))


# --- your data: export and delete -------------------------------------------------

class PasswordConfirm(BaseModel):
    password: str = Field(max_length=200)


@app.get("/api/me/export")
def export_my_data(user=Depends(current_user)):
    """Everything we store about the signed-in user, as a JSON download."""
    data = db.export_user(user["id"])
    for w in data["workouts"]:
        w["workout"] = json.loads(w.pop("workout_json")) if w["workout_json"] else None
        w["chat"] = llm.visible_transcript(json.loads(w.pop("messages_json")))
    data["exported_at"] = datetime.now(timezone.utc).isoformat()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(
        json.dumps(data, indent=2, ensure_ascii=False), media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="vibegrind-my-data-{stamp}.json"'},
    )


@app.post("/api/me/delete")
def delete_my_account(body: PasswordConfirm, request: Request, user=Depends(current_user)):
    """Delete the account and everything linked to it. Asks for the password as confirmation."""
    security.check_rate(request, "delete-account", limit=5, window_s=900)
    if not verify_password(body.password, user["password_hash"]):
        raise HTTPException(400, "That password isn't right.")
    if garmin.is_owner(user):
        (db.DB_PATH.parent / "garmin-bearer-token.json").unlink(missing_ok=True)
    db.delete_user(user["id"])
    request.session.clear()
    return {"ok": True}


# --- password reset by email ------------------------------------------------------

RESET_VALID_FOR = timedelta(hours=1)
ADMIN_RESET_VALID_FOR = timedelta(hours=24)  # links an admin shares by hand may take a while to be opened


class ResetRequestIn(BaseModel):
    email: str = Field(max_length=200)


class ResetConfirmIn(BaseModel):
    token: str = Field(max_length=200)
    password: str = Field(max_length=200)


def _public_base_url(request: Request) -> str:
    canonical = (os.environ.get("CANONICAL_HOST") or "").strip()
    return f"https://{canonical}" if canonical else str(request.base_url).rstrip("/")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@app.post("/api/password-reset/request")
def request_password_reset(body: ResetRequestIn, request: Request):
    """Email a one-hour reset link. The answer is the same whether or not the account exists."""
    security.check_rate(request, "password-reset", limit=5, window_s=3600)
    if not mailer.available():
        raise HTTPException(503, "Password reset by email isn't available. Ask the person who invited you.")
    email = body.email.strip().lower()
    security.check_rate(request, "password-reset-email", limit=3, window_s=3600, key=email)  # no inbox flooding
    user = db.get_user_by_email(email)
    if user is not None:
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + RESET_VALID_FOR).isoformat()
        db.create_password_reset(user["id"], _token_hash(token), expires)
        link = f"{_public_base_url(request)}/app#reset={token}"
        try:
            mailer.send(user["email"], "Reset your VibeGrind password", (
                "Hi,\n\nsomeone (hopefully you) asked to reset the password for your VibeGrind account.\n\n"
                f"Choose a new password here (the link works once, for one hour):\n{link}\n\n"
                "If you didn't ask for this, ignore this email; your password stays the same.\n\nVibeGrind\n"
            ))
        except mailer.MailError:
            logger.exception("Password reset email failed")  # don't reveal to the caller that the account exists
    return {"ok": True, "message": "If an account exists for that email, we've sent a reset link. It's valid for one hour."}


@app.post("/api/password-reset/confirm")
def confirm_password_reset(body: ResetConfirmIn, request: Request):
    security.check_rate(request, "password-reset-confirm", limit=10, window_s=900)
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    now = datetime.now(timezone.utc).isoformat()
    if not db.use_password_reset(_token_hash(body.token), now, hash_password(body.password)):
        raise HTTPException(400, "This reset link is invalid, already used or expired. Ask for a new one.")
    return {"ok": True}


# --- Garmin readiness ------------------------------------------------------------

@app.get("/api/readiness")
def get_readiness(refresh: bool = False, user=Depends(current_user)):
    """Readiness dashboard data. Syncs from Garmin first when the last sync is a few hours old."""
    if not garmin.is_owner(user):
        raise HTTPException(404, "Garmin isn't connected for this account.")
    sync_error = None
    try:
        garmin.sync(user, force=refresh)
    except garmin.GarminError as e:
        sync_error = str(e)  # still show what's stored
    today = date.today()
    since = (today - timedelta(days=70)).isoformat()
    data = readiness.build(db.garmin_days(user["id"], since), db.garmin_activities(user["id"], since), today)
    recent = [dict(a) for a in db.garmin_activities(user["id"], (today - timedelta(days=14)).isoformat())][::-1][:10]
    state = db.garmin_sync_state(user["id"])
    return {
        **data,
        "activities": recent,
        "synced_at": state["synced_at"] if state else None,
        "sync_error": sync_error,
    }


# --- admin: invites ------------------------------------------------------------

class InviteIn(BaseModel):
    note: str | None = Field(default=None, max_length=100)


def _admin(user=Depends(current_user)):
    if not _is_admin(user):
        raise HTTPException(403, "Only admins can do that.")
    return user


@app.get("/api/admin/invites")
def list_invites(user=Depends(_admin)):
    return [dict(r) for r in db.list_invites()]


@app.post("/api/admin/invites")
def create_invite(body: InviteIn, user=Depends(_admin)):
    # Easy to read out loud or type: no 0/O or 1/I.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2))
    db.create_invite(code, user["id"], (body.note or "").strip() or None)
    return {"code": code}


# --- admin: users and stats ----------------------------------------------------------

class AdminResetIn(BaseModel):
    send_email: bool = False


@app.get("/api/admin/users")
def admin_users(user=Depends(_admin)):
    admins = config.admin_emails()
    return {
        "mail": mailer.available(),
        "users": [{**dict(r), "is_admin": r["email"] in admins} for r in db.admin_users()],
    }


@app.post("/api/admin/users/{user_id}/reset")
def admin_reset_link(user_id: int, body: AdminResetIn, request: Request, user=Depends(_admin)):
    """A single-use link to choose a new password, valid 24 h: emailed to the user, or handed to the admin
    to share. The old password keeps working until the link is used; an earlier unused link stops working."""
    target = db.get_user(user_id)
    if target is None:
        raise HTTPException(404, "No such user.")
    if body.send_email and not mailer.available():  # before a new link replaces one already shared
        raise HTTPException(503, "Email isn't set up on this server yet. Copy the link and send it yourself.")
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + ADMIN_RESET_VALID_FOR
    db.create_password_reset(target["id"], _token_hash(token), expires.isoformat())
    link = f"{_public_base_url(request)}/app#reset={token}"
    emailed = False
    if body.send_email:
        try:
            mailer.send(target["email"], "Choose a new VibeGrind password", (
                "Hi,\n\nhere's a link to choose a new password for your VibeGrind account.\n\n"
                f"It works once, within 24 hours:\n{link}\n\n"
                "If you didn't expect this, ignore this email; your password stays the same.\n\nVibeGrind\n"
            ))
        except mailer.MailError as e:
            raise HTTPException(502, f"{e} Copy the link and send it yourself.")
        emailed = True
    return {"link": link, "expires_at": expires.isoformat(), "emailed": emailed}


@app.get("/api/admin/stats")
def admin_stats(user=Depends(_admin)):
    since = (date.today() - timedelta(days=29)).isoformat()
    stats = db.admin_stats(since)
    month = _today()[:7]
    return {**stats, "since": since, "cost_month": round(db.cost_this_month(month), 2),
            "budget": config.monthly_budget_usd()}


# --- admin: backup and restore ------------------------------------------------------

@app.get("/api/admin/backup")
def download_backup(user=Depends(_admin)):
    """A consistent copy of the whole database (accounts, workouts, Garmin data)."""
    path = backup.snapshot()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return FileResponse(
        path, media_type="application/vnd.sqlite3", filename=f"vibegrind-backup-{stamp}.sqlite3",
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@app.get("/api/admin/offsite")
def offsite_status(user=Depends(_admin)):
    state = db.job_state("offsite_backup")
    return {
        "configured": offsite.configured(),
        "keep_days": offsite.keep_days(),
        "last_ok_at": state["last_ok_at"] if state else None,
        "last_error": state["last_error"] if state else None,
    }


@app.get("/api/admin/restore")
def restore_status(user=Depends(_admin)):
    return {"allowed": backup.restore_allowed(), "target_empty": backup.target_is_empty()}


@app.post("/api/admin/restore")
def restore_backup(file: UploadFile, request: Request, user=Depends(_admin)):
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        size = 0
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > backup.MAX_BACKUP_BYTES:
                tmp_path.unlink(missing_ok=True)
                raise HTTPException(400, "That file is too large to be a VibeGrind backup.")
            tmp.write(chunk)
    try:
        summary = backup.restore(tmp_path)
    except backup.RestoreError as e:
        raise HTTPException(400, str(e))
    finally:
        tmp_path.unlink(missing_ok=True)
    request.session.clear()  # account IDs come from the backup now; sign in again
    return {"ok": True, **summary}


# --- workouts ---------------------------------------------------------------

class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


def _workout_view(user, row) -> dict:
    workout = json.loads(row["workout_json"]) if row["workout_json"] else None
    history = json.loads(row["messages_json"])
    pace = user["threshold_pace_s_per_km"]
    return {
        "id": row["id"],
        "workout": workout,
        "segments": flatten(workout, pace) if workout else [],
        "stats": stats(workout, pace) if workout else None,
        "transcript": llm.visible_transcript(history),
        "source_filename": row["source_filename"],
        "planned_date": row["planned_date"],
    }


def _get_row(user, workout_id: int):
    row = db.get_workout(user["id"], workout_id)
    if row is None:
        raise HTTPException(404, "Workout not found")
    return row


@app.get("/api/workouts")
def list_workouts(user=Depends(current_user)):
    return [dict(r) for r in db.list_workouts(user["id"], ENABLED_SPORTS)]


@app.post("/api/workouts")
def create_workout(user=Depends(current_user)):
    workout_id = db.create_workout(user["id"])
    return _workout_view(user, _get_row(user, workout_id))


@app.get("/api/workouts/{workout_id}")
def get_workout(workout_id: int, user=Depends(current_user)):
    return _workout_view(user, _get_row(user, workout_id))


@app.delete("/api/workouts/{workout_id}")
def delete_workout(workout_id: int, user=Depends(current_user)):
    _get_row(user, workout_id)
    db.delete_workout(user["id"], workout_id)
    return {"ok": True}


@app.post("/api/workouts/{workout_id}/chat")
def chat(workout_id: int, body: ChatIn, user=Depends(current_user)):
    row = _get_row(user, workout_id)
    _check_chat_allowed(user)
    current = json.loads(row["workout_json"]) if row["workout_json"] else None
    history = json.loads(row["messages_json"])
    with _claude_errors():
        reply, workout, history, usage = llm.chat(user, history, current, body.message,
                                                  readiness_text=_readiness_for_chat(user))
    db.save_workout(user["id"], workout_id, workout, history)
    db.record_usage(user["id"], _today(), usage.input_tokens, usage.output_tokens, usage.cost_usd)
    view = _workout_view(user, _get_row(user, workout_id))
    view["reply"] = reply
    view["messages_left_today"] = _messages_left(user)
    return view


@contextmanager
def _claude_errors():
    """Turn Claude API failures into answers the app can show."""
    try:
        yield
    except llm.MissingCredentials:
        raise HTTPException(500, "No Anthropic API key is configured on the server. Set ANTHROPIC_API_KEY and restart.")
    except anthropic.AuthenticationError:
        raise HTTPException(500, "The server's Anthropic API key is missing or invalid.")
    except anthropic.RateLimitError:
        raise HTTPException(429, "Too many requests to Claude right now. Try again in a minute.")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Claude API error: {e.message}")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Couldn't reach the Claude API.")


# --- calendar -------------------------------------------------------------------------

PLAN_AHEAD = timedelta(days=56)  # how far ahead the planning chat may put workouts
PLAN_CHAT_TURNS = 6  # earlier planning messages are dropped; the calendar itself is sent every turn


class PlanDateIn(BaseModel):
    date: str | None = None


class PlanChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    today: str | None = None  # the browser's local date; the server runs on UTC


def _parse_day(text: str | None, field: str = "date") -> date:
    try:
        return date.fromisoformat(text or "")
    except ValueError:
        raise HTTPException(400, f"Give the {field} as YYYY-MM-DD.")


def _local_today(text: str | None) -> date:
    """The user's today: the browser's date if it's within a day of the server's (time zones), else UTC's."""
    utc = datetime.now(timezone.utc).date()
    try:
        d = date.fromisoformat(text or "")
    except ValueError:
        return utc
    return d if abs((d - utc).days) <= 1 else utc


def _planned_view(user, row) -> dict:
    workout = json.loads(row["workout_json"]) if row["workout_json"] else None
    pace = user["threshold_pace_s_per_km"]
    st = stats(workout, pace) if workout else None
    return {"id": row["id"], "name": row["name"], "date": row["planned_date"], "sport": row["sport"],
            "duration_s": st["duration_s"] if st else None, "tss": st.get("tss") if st else None,
            "peak": _peak_intensity(flatten(workout, pace)) if workout else None}


def _peak_intensity(segments, min_seconds: int = 300) -> float | None:
    """The highest intensity a session holds for at least five minutes in total, so the calendar can
    colour it by its main zone (a short opener at 110 % doesn't make threshold work a VO2 session)."""
    spent = 0
    for s in sorted(segments, key=lambda s: -(s["intensity"] or 0)):
        if s["intensity"] is None:
            continue
        spent += s["duration_s"]
        if spent >= min_seconds:
            return s["intensity"]
    return None


@app.put("/api/workouts/{workout_id}/plan")
def plan_workout(workout_id: int, body: PlanDateIn, user=Depends(current_user)):
    """Put a workout on a day of the calendar, or take it off (date null)."""
    _get_row(user, workout_id)
    day = _parse_day(body.date).isoformat() if body.date else None
    db.set_planned_date(user["id"], workout_id, day)
    return _workout_view(user, _get_row(user, workout_id))


class PlanClearIn(BaseModel):
    start: str
    end: str | None = None  # none: everything from start on
    delete: bool = False  # delete the workouts instead of keeping them in the library


@app.post("/api/plan/clear")
def clear_plan(body: PlanClearIn, user=Depends(current_user)):
    first = _parse_day(body.start, "start")
    last = _parse_day(body.end, "end") if body.end else date(9999, 12, 31)
    if last < first:
        raise HTTPException(400, "The end date is before the start date.")
    count = db.clear_plan(user["id"], first.isoformat(), last.isoformat(), body.delete)
    return {"cleared": count, "deleted": body.delete}


@app.get("/api/plan")
def get_plan(start: str, days: int = 14, user=Depends(current_user)):
    first = _parse_day(start, "start")
    days = max(1, min(days, 42))
    end = first + timedelta(days=days - 1)
    rows = db.planned_workouts(user["id"], first.isoformat(), end.isoformat())
    return {
        "start": first.isoformat(),
        "end": end.isoformat(),
        "workouts": [_planned_view(user, r) for r in rows],
        "transcript": llm.visible_transcript(db.plan_chat(user["id"])),
    }


class _Planner:
    """Applies the planning chat's tool calls to the user's calendar and reports back to Claude."""

    def __init__(self, user, today: date):
        self.user, self.today = user, today
        self.changed = False

    def _date(self, text) -> str:
        d = date.fromisoformat(str(text))
        if not self.today <= d <= self.today + PLAN_AHEAD:
            raise ValueError(f"{text} is outside the planning window ({self.today} to {self.today + PLAN_AHEAD})")
        return d.isoformat()

    def plan(self, items: list) -> tuple[str, bool]:
        done, errors = [], []
        for i, item in enumerate(items):
            try:
                day = self._date(item.get("date"))
                workout = llm.check_workout({**(item.get("workout") or {}),
                                             "guideline_exception": item.get("guideline_exception")},
                                            self.user["threshold_pace_s_per_km"],
                                            self.user["endurance_ceiling_pct"])
                old = item.get("replaces_id")
                if old is not None:
                    row = db.get_workout(self.user["id"], int(old))
                    if row is None or row["planned_date"] is None:
                        raise ValueError(f"there's no planned workout with id {old}")
                    db.save_workout(self.user["id"], row["id"], workout, json.loads(row["messages_json"]))
                    db.set_planned_date(self.user["id"], row["id"], day)
                    done.append(f"replaced #{row['id']} with \"{workout['name']}\" on {day}")
                else:
                    wid = db.create_workout(self.user["id"], workout)
                    db.set_planned_date(self.user["id"], wid, day)
                    done.append(f"#{wid} \"{workout['name']}\" on {day}")
            except (ValueError, TypeError, WorkoutError) as e:
                errors.append(f"item {i + 1}: {e}")
        self.changed = self.changed or bool(done)
        text = ("Planned: " + "; ".join(done) + ".") if done else ""
        if errors:
            text += (" " if text else "") + "Not planned, fix and call again: " + "; ".join(errors) + "."
        return text, bool(errors) and not done

    def remove(self, ids: list) -> tuple[str, bool]:
        removed = []
        for wid in ids:
            row = db.get_workout(self.user["id"], int(wid))
            if row is not None and row["planned_date"]:
                db.set_planned_date(self.user["id"], row["id"], None)
                removed.append(f"#{row['id']}")
        self.changed = self.changed or bool(removed)
        return (f"Took {', '.join(removed)} off the calendar." if removed else "None of those ids were planned."), not removed


def _plan_context(user, today: date) -> str:
    rows = db.planned_workouts(user["id"], (today - timedelta(days=7)).isoformat(), (today + PLAN_AHEAD).isoformat())
    lines = [f"Today is {today.isoformat()} ({today.strftime('%A')}).", "Planned workouts (past week and ahead):"]
    for r in rows:
        v = _planned_view(user, r)
        d = date.fromisoformat(v["date"])
        dur = f"{round(v['duration_s'] / 60)} min" if v["duration_s"] else "no steps"
        tss = f", TSS {round(v['tss'])}" if v["tss"] else ""
        lines.append(f"- {v['date']} ({d.strftime('%a')}): #{v['id']} \"{v['name']}\", {dur}{tss}")
    if not rows:
        lines.append("- none")
    return "\n".join(lines)


@app.post("/api/plan/chat")
def plan_chat(body: PlanChatIn, user=Depends(current_user)):
    _check_chat_allowed(user)
    today = _local_today(body.today)
    planner = _Planner(user, today)
    with _claude_errors():
        reply, history, usage = llm.plan_chat(user, llm.trim_history(db.plan_chat(user["id"]), PLAN_CHAT_TURNS), body.message,
                                              _plan_context(user, today), planner,
                                              readiness_text=_readiness_for_chat(user))
    db.save_plan_chat(user["id"], history)
    db.record_usage(user["id"], _today(), usage.input_tokens, usage.output_tokens, usage.cost_usd)
    return {"reply": reply, "changed": planner.changed, "messages_left_today": _messages_left(user),
            "transcript": llm.visible_transcript(history)}


@app.delete("/api/plan/chat")
def clear_plan_chat(user=Depends(current_user)):
    """Start the planning conversation over; planned workouts stay on the calendar."""
    db.save_plan_chat(user["id"], [])
    return {"ok": True}


def _readiness_for_chat(user) -> str | None:
    """The Garmin owner's readiness summary for Claude; None for everyone else."""
    if not garmin.is_owner(user):
        return None
    try:
        garmin.sync(user)  # only fetches when the last sync is a few hours old
    except garmin.GarminError:
        pass  # use what's stored
    today = date.today()
    since = (today - timedelta(days=70)).isoformat()
    return readiness.chat_context(db.garmin_days(user["id"], since), db.garmin_activities(user["id"], since), today)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _messages_left(user) -> int | None:
    """None means unlimited (admins)."""
    if _is_admin(user):
        return None
    return max(0, config.daily_message_limit() - db.messages_today(user["id"], _today()))


def _check_chat_allowed(user) -> None:
    budget = config.monthly_budget_usd()
    if budget and db.cost_this_month(_today()[:7]) >= budget:
        raise HTTPException(503, "The chat is paused for the rest of the month because the app's AI budget is used up. "
                                 "You can still import, view and download workouts.")
    if _messages_left(user) == 0:
        raise HTTPException(429, f"You've used all {config.daily_message_limit()} chat messages for today. "
                                 "The limit resets at midnight (UTC).")


@app.get("/api/workouts/{workout_id}/export/{fmt}")
def export(workout_id: int, fmt: str, user=Depends(current_user)):
    row = _get_row(user, workout_id)
    if not row["workout_json"]:
        raise HTTPException(400, "This workout is empty.")
    workout = json.loads(row["workout_json"])
    pace = user["threshold_pace_s_per_km"]
    try:
        if fmt == "zwo":
            data, media = to_zwo(workout, pace), "application/xml"
        elif fmt == "fit":
            data, media = to_fit(workout, pace), "application/vnd.ant.fit"
        else:
            raise HTTPException(404, "Unknown format")
    except ExportError as e:
        raise HTTPException(400, str(e))
    filename = safe_filename(workout["name"], fmt)
    return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/integrations")
def integrations(user=Depends(current_user)):
    return {"zwift": zwift.status()}


@app.post("/api/workouts/{workout_id}/send/zwift")
def send_to_zwift(workout_id: int, user=Depends(current_user)):
    row = _get_row(user, workout_id)
    if not row["workout_json"]:
        raise HTTPException(400, "This workout is empty.")
    workout = json.loads(row["workout_json"])
    data = to_zwo(workout, user["threshold_pace_s_per_km"])
    try:
        if row["source_filename"]:
            # Imported from Zwift: update that file, keeping the previous version as a backup.
            path, backup = zwift.replace(row["source_filename"], data, ZWIFT_BACKUPS)
            return {"path": str(path), "filename": path.name, "replaced": True,
                    "backup": str(backup) if backup else None}
        path = zwift.save(data, safe_filename(workout["name"], "zwo"))
    except zwift.ZwiftFolderError as e:
        raise HTTPException(400, str(e))
    return {"path": str(path), "filename": path.name, "replaced": False}


# --- import -------------------------------------------------------------------

class ZwiftImportIn(BaseModel):
    filename: str = Field(max_length=255)


@app.get("/api/zwift/workouts")
def list_zwift_workouts(user=Depends(current_user)):
    try:
        files = zwift.list_files()
    except zwift.ZwiftFolderError as e:
        raise HTTPException(400, str(e))
    out = []
    for path in files:
        try:
            w = from_zwo(path.read_bytes(), path.stem)
            out.append({"filename": path.name, "name": w["name"], "sport": w["sport"],
                        "duration_s": stats(w)["duration_s"], "error": None})
        except ImportError_ as e:
            out.append({"filename": path.name, "name": path.stem, "sport": None, "duration_s": None, "error": str(e)})
    return out


@app.post("/api/import/zwift")
def import_from_zwift(body: ZwiftImportIn, user=Depends(current_user)):
    try:
        workout = from_zwo(zwift.read(body.filename), Path(body.filename).stem)
    except (zwift.ZwiftFolderError, ImportError_) as e:
        raise HTTPException(400, str(e))
    workout_id = db.create_workout(user["id"], workout, source_filename=body.filename)
    return _workout_view(user, _get_row(user, workout_id))


@app.post("/api/import/file")
def import_file(file: UploadFile, user=Depends(current_user)):
    """Upload a Zwift (.zwo) or Garmin (.fit) workout file."""
    filename = file.filename or "workout"
    data = file.file.read(MAX_ZWO_BYTES + 1)
    stem = Path(filename).stem
    is_fit = data[8:12] == b".FIT"  # FIT files carry this signature in their header
    try:
        if is_fit:
            workout = from_fit(data, user["ftp"], stem, user["threshold_pace_s_per_km"])
        elif filename.lower().endswith(".zwo") or data.lstrip().startswith(b"<"):
            workout = from_zwo(data, stem)
        else:
            raise ImportError_("Choose a Zwift workout (.zwo) or a Garmin workout (.fit).")
    except ImportError_ as e:
        raise HTTPException(400, str(e))
    workout_id = db.create_workout(user["id"], workout)
    return _workout_view(user, _get_row(user, workout_id))


# --- public pages ----------------------------------------------------------------
# Defined last: "/{page}" would otherwise shadow routes declared after it.

PUBLIC_PAGES = {"privacy": "privacy.html", "terms": "terms.html", "imprint": "imprint.html"}


@app.get("/{page}")
def public_page(page: str):
    if page not in PUBLIC_PAGES:
        raise HTTPException(404, "Not found")
    return FileResponse(STATIC / PUBLIC_PAGES[page])
