"""Pull the owner's data from the Garmin Connect Personal API and store it per day.

The Personal API is for one person's own data (Garmin: "not intended for commercial applications
serving multiple end users"), so the app connects exactly one account: GARMIN_OWNER_EMAIL, or the
first admin. The key lives in GARMIN_PERSONAL_API_KEY and is exchanged for a 24-hour bearer token.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

from . import config, db, fitmeta

BACKFILL_DAYS = 60
RESYNC_DAYS = 3  # re-read the last few days: sleep and HRV can arrive after the day itself
MIN_SYNC_INTERVAL = timedelta(hours=3)
CHUNK_DAYS = 28  # keep each range request modest
RECOVERY_DAYS = 14  # files read for recovery time: 4 days would do for today, 14 give past scores theirs


class GarminError(RuntimeError):
    pass


def api_key() -> str | None:
    return (os.environ.get("GARMIN_PERSONAL_API_KEY") or "").strip() or None


def owner_email() -> str | None:
    """GARMIN_OWNER_EMAIL, or the admin when there's exactly one (with several, it must be explicit)."""
    explicit = (os.environ.get("GARMIN_OWNER_EMAIL") or "").strip().lower()
    if explicit:
        return explicit
    admins = config.admin_emails()
    return next(iter(admins)) if len(admins) == 1 else None


def is_owner(user) -> bool:
    return api_key() is not None and user["email"] == owner_email()


def status(user) -> str:
    """Why the Readiness dashboard is or isn't shown, in words an admin can act on."""
    if api_key() is None:
        return "Off: GARMIN_PERSONAL_API_KEY isn't set on the server."
    owner = owner_email()
    if owner is None:
        if len(config.admin_emails()) > 1:
            return "Off: ADMIN_EMAILS lists several addresses, so set GARMIN_OWNER_EMAIL to the account that should see it."
        return "Off: no owner. Set ADMIN_EMAILS or GARMIN_OWNER_EMAIL to your sign-in email."
    if user["email"] != owner:
        return f"Off for this account: the dashboard belongs to {owner}, but you're signed in as {user['email']}."
    return "On: the Readiness tab uses your Garmin key."


def _client():
    """A Garmin client with a fresh-enough token. Separate so tests can replace it."""
    from garmin_personal_api import GarminPersonalApiClient, GarminPersonalApiError, PersonalApiAuthenticator

    cache = db.DB_PATH.parent / "garmin-bearer-token.json"
    try:
        with PersonalApiAuthenticator(bearer_token_cache_file=cache) as auth:
            token = auth.obtain_token(api_key())
    except GarminPersonalApiError as e:
        status = getattr(e, "status_code", None)
        if status in (401, 403, 404):  # 404: the key was deleted in Garmin Connect
            raise GarminError("Garmin rejected the API key. It may have expired: create a new one in Garmin Connect.")
        raise GarminError(f"Couldn't get a Garmin token ({status or e}).")
    return GarminPersonalApiClient(token.access_token)


def sync(user, *, force: bool = False, today: date | None = None, client=None) -> dict:
    """Fetch new data if the last sync is old enough. Returns the sync state."""
    if not is_owner(user):
        raise GarminError("Garmin isn't connected for this account.")
    state = db.garmin_sync_state(user["id"])
    now = datetime.now(timezone.utc)
    if not force and state and state["synced_at"] and not state["error"]:
        if now - datetime.fromisoformat(state["synced_at"]) < MIN_SYNC_INTERVAL:
            return {"synced_at": state["synced_at"], "error": None, "fetched": False}

    today = today or date.today()
    latest = db.garmin_latest_day(user["id"])
    start = date.fromisoformat(latest) - timedelta(days=RESYNC_DAYS) if latest else today - timedelta(days=BACKFILL_DAYS)
    try:
        client = client or _client()
        for lo, hi in _chunks(start, today):
            _sync_hrv(client, user["id"], lo, hi)
            _sync_sleep(client, user["id"], lo, hi)
            _sync_daily(client, user["id"], lo, hi)
            _sync_activities(client, user["id"], lo, hi)
        # Runs stored before pace and GAP were kept: read their summaries once more.
        stale = db.garmin_oldest_run_without_speed(user["id"], (today - timedelta(days=BACKFILL_DAYS)).isoformat())
        if stale and date.fromisoformat(stale) < start:
            for lo, hi in _chunks(date.fromisoformat(stale), start - timedelta(days=1)):
                _sync_activities(client, user["id"], lo, hi)
        _sync_recovery(client, user["id"], today - timedelta(days=RECOVERY_DAYS))
    except GarminError as e:
        db.set_garmin_sync_state(user["id"], None, str(e))
        raise
    except Exception as e:  # network trouble, unexpected responses
        msg = f"Garmin sync failed: {getattr(e, 'status_code', '') or type(e).__name__}"
        db.set_garmin_sync_state(user["id"], None, msg)
        raise GarminError(msg) from e
    stamp = now.isoformat()
    db.set_garmin_sync_state(user["id"], stamp, None)
    return {"synced_at": stamp, "error": None, "fetched": True}


def _chunks(start: date, end: date):
    lo = start
    while lo <= end:
        hi = min(lo + timedelta(days=CHUNK_DAYS - 1), end)
        yield lo, hi
        lo = hi + timedelta(days=1)


def _get(obj, *path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _sync_hrv(client, user_id: int, lo: date, hi: date) -> None:
    data = client.get_daily_hrv_summaries(lo, hi) or {}
    for s in data.get("hrvSummaries") or []:
        if not s.get("calendarDate"):
            continue
        db.upsert_garmin_day(user_id, s["calendarDate"], {
            "hrv_last_night": s.get("lastNightAvg"),
            "hrv_weekly_avg": s.get("weeklyAvg"),
            "hrv_low_upper": _get(s, "baseline", "lowUpper"),
            "hrv_balanced_low": _get(s, "baseline", "balancedLow"),
            "hrv_balanced_upper": _get(s, "baseline", "balancedUpper"),
            "hrv_status": s.get("status"),
        })


def _sync_sleep(client, user_id: int, lo: date, hi: date) -> None:
    for s in client.get_daily_sleep_summaries(lo, hi) or []:
        if not s.get("calendarDate") or not s.get("sleepTimeSeconds"):
            continue
        overall = (s.get("sleepScores") or {}).get("overall") or {}
        db.upsert_garmin_day(user_id, s["calendarDate"], {
            "sleep_seconds": s.get("sleepTimeSeconds"),
            "deep_seconds": s.get("deepSleepSeconds"),
            "light_seconds": s.get("lightSleepSeconds"),
            "rem_seconds": s.get("remSleepSeconds"),
            "awake_seconds": s.get("awakeSleepSeconds"),
            "sleep_score": overall.get("value"),
            "sleep_qualifier": overall.get("qualifierKey"),
        })


def _sync_daily(client, user_id: int, lo: date, hi: date) -> None:
    data = client.get_daily_summaries(lo, hi) or {}
    for s in data.get("data") or []:
        if not s.get("calendarDate"):
            continue
        stress = _get(s, "stress", "avgLevel")
        db.upsert_garmin_day(user_id, s["calendarDate"], {
            "resting_hr": _get(s, "heartRate", "restingValue"),
            "bb_max": _get(s, "bodyBattery", "maxValue"),
            "bb_min": _get(s, "bodyBattery", "minValue"),
            "bb_charged": _get(s, "bodyBattery", "chargedValue"),
            "bb_drained": _get(s, "bodyBattery", "drainedValue"),
            "stress_avg": stress if stress is not None and stress >= 0 else None,  # Garmin uses -1/-2 for "no data"
        })


def _sync_activities(client, user_id: int, lo: date, hi: date) -> None:
    start, limit = 0, 100
    while True:
        page = client.search_activities(start=start, limit=limit, start_date=lo, end_date=hi) or []
        for a in page:
            if not a.get("activityId") or not a.get("startTimeLocal"):
                continue
            atype = a.get("activityType")
            type_key = (atype.get("typeKey") if isinstance(atype, dict) else None) or ""
            db.upsert_garmin_activity(user_id, a["activityId"], {
                "day": a["startTimeLocal"][:10],
                "start_local": a["startTimeLocal"],
                "name": a.get("activityName"),
                "type_key": type_key,
                "duration_s": a.get("duration"),
                "avg_power": a.get("avgPower"),
                "norm_power": a.get("normPower"),
                "tss": a.get("trainingStressScore"),
                "intensity_factor": a.get("intensityFactor"),
                "training_load": a.get("activityTrainingLoad"),
                "aerobic_te": a.get("aerobicTrainingEffect"),
                "anaerobic_te": a.get("anaerobicTrainingEffect"),
                "te_label": a.get("trainingEffectLabel"),
                "vo2max": a.get("vO2MaxValue"),
                "avg_hr": a.get("averageHR"),
                "avg_speed": a.get("averageSpeed"),
                "gap_speed": a.get("avgGradeAdjustedSpeed"),
            })
        if len(page) < limit:
            break
        start += limit


def _sync_recovery(client, user_id: int, since: date) -> None:
    """Read the recovery time from each recent activity's original file, once per activity.

    The Personal API's activity summaries don't include it; the device writes it into the file.
    A file that can't be downloaded is tried again on the next sync; one without a value is not.
    """
    for activity_id in db.garmin_activities_without_recovery(user_id, since.isoformat()):
        try:
            data = client.download_activity(activity_id)
        except Exception:
            continue
        found = fitmeta.recovery(data)
        minutes, start = found if found else (None, None)
        db.set_garmin_recovery(activity_id, minutes, start.isoformat() if start else None)


def thresholds(user, client=None) -> dict:
    """FTP and threshold heart rates as Garmin has them, for Settings to offer. Threshold pace isn't
    in the Personal API (its activity field stays empty), so it's not here."""
    if not is_owner(user):
        raise GarminError("Garmin isn't connected for this account.")
    try:
        client = client or _client()
        out = {"ftp": None, "lthr_run": None, "lthr_bike": None}
        zones = client.get_heart_rate_zones() or []
        by_sport = {z.get("sport"): z.get("lactateThresholdHeartRateUsed") for z in zones if isinstance(z, dict)}
        out["lthr_run"] = by_sport.get("RUNNING") or by_sport.get("DEFAULT")
        out["lthr_bike"] = by_sport.get("CYCLING") or by_sport.get("DEFAULT")
        ride = db.garmin_latest_ride_with_power(user["id"])
        if ride:
            ftp = _get(client.get_activity(ride), "summaryDTO", "functionalThresholdPower")
            out["ftp"] = round(ftp) if ftp else None
    except GarminError:
        raise
    except Exception as e:
        raise GarminError(f"Couldn't read your thresholds from Garmin ({getattr(e, 'status_code', '') or type(e).__name__}).") from e
    return {k: (round(v) if v else None) for k, v in out.items()}
