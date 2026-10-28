"""The app's own readiness estimate, built from Garmin data.

Garmin's Personal API doesn't expose Training Readiness or training status, so this module derives a
transparent 0-100 score from what it does expose. Each component scores 0-100 and
explains itself; the total is their weighted average (missing components are left out and the
weights rescaled). It is an estimate, labelled as such in the UI - not Garmin's number.

Recovery time is Garmin's own: the device writes it into each activity file (see fitmeta.py) and it is
counted down from there. It is also one of the score's components, and while it's red (over 36 h left)
the score is capped at 54, so a good night can't override a very hard session.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from statistics import median

WEIGHTS = {"hrv": 0.25, "sleep": 0.20, "recovery": 0.20, "resting_hr": 0.10, "body_battery": 0.15, "load": 0.10}
RECOVERY_POINTS = [(0, 100), (15, 75), (36, 40), (72, 0)]  # hours left -> points, linear in between
RED_CAP = 54  # highest score while recovery is red: "Take it easy" at best
MORNING_UTC = 6  # past days are scored with the recovery time left at about 7:00 Swiss time
RECOVERY_WINDOW = timedelta(days=4)  # Garmin's recovery time never exceeds 4 days

VERDICTS = [  # (minimum score, key, title, advice)
    (75, "ready", "Ready for intensity", "Good day for intervals or a hard session."),
    (55, "moderate", "Moderately ready", "Endurance or tempo is fine; keep the hardest work for a better day."),
    (40, "easy", "Take it easy", "An easy Zone 1–2 ride or rest."),
    (0, "recover", "Recovery day", "Rest, or a very easy spin."),
]


RECOVERY_LIGHTS = {  # green under 15 h, yellow 15-36 h, red over 36 h
    "red": ("Still recovering", "A recovery ride or a rest day."),
    "yellow": ("Partly recovered", "Easy to moderate: Zone 1–2 or a short tempo block, no hard intervals."),
    "green": ("Recovered", "Good to go, hard sessions included."),
}


def recovery_light(hours_left: float) -> str:
    return "red" if hours_left > 36 else "yellow" if hours_left >= 15 else "green"


def _clamp(x: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, x))


def daily_loads(activities, since: date, until: date) -> dict[str, float]:
    """Training load per day: Garmin's activity training load, or TSS when that's missing."""
    loads = {(since + timedelta(days=i)).isoformat(): 0.0 for i in range((until - since).days + 1)}
    for a in activities:
        value = a["training_load"] if a["training_load"] is not None else a["tss"]
        if value and a["day"] in loads:
            loads[a["day"]] += float(value)
    return loads


def load_summary(loads: dict[str, float], day: date) -> dict:
    """Acute (7-day) vs chronic (28-day) average daily load, up to and including the day before."""
    def avg(n):
        days = [(day - timedelta(days=i)).isoformat() for i in range(1, n + 1)]
        return sum(loads.get(d, 0.0) for d in days) / n
    acute, chronic = avg(7), avg(28)
    last48 = sum(loads.get((day - timedelta(days=i)).isoformat(), 0.0) for i in (0, 1))
    return {
        "acute": round(acute, 1),
        "chronic": round(chronic, 1),
        "ratio": round(acute / chronic, 2) if chronic > 0 else None,
        "last_48h": round(last48),
    }


def _hrv(row) -> dict | None:
    x = row["hrv_last_night"]
    if not x:
        return None
    low, bl, bu = row["hrv_low_upper"], row["hrv_balanced_low"], row["hrv_balanced_upper"]
    if bl and bu:
        if x > bu:
            score, note = 80, f"{x} ms, above your usual range ({bl}–{bu})"
        elif x >= bl:
            score, note = 90 + 10 * (x - bl) / max(1, bu - bl), f"{x} ms, within your usual range ({bl}–{bu})"
        elif low and x > low:
            score, note = 50 + 35 * (x - low) / max(1, bl - low), f"{x} ms, a little below your usual range ({bl}–{bu})"
        else:
            score, note = _clamp(45 * x / max(1, low or bl)), f"{x} ms, well below your usual range ({bl}–{bu})"
    elif row["hrv_weekly_avg"]:
        ratio = x / row["hrv_weekly_avg"]
        score = _clamp(90 + (ratio - 1) * 250) if ratio < 1 else 90
        note = f"{x} ms vs. a 7-day average of {row['hrv_weekly_avg']} ms (no baseline yet)"
    else:
        return None
    return {"score": round(_clamp(score)), "note": note}


def _resting_hr(row, history) -> dict | None:
    x = row["resting_hr"]
    previous = [r["resting_hr"] for r in history if r["resting_hr"]]
    if not x or len(previous) < 5:
        return None
    base = median(previous)
    diff = x - base
    score = 100 if diff <= 0 else 100 - 10 * diff
    word = "at" if abs(diff) < 1 else ("above" if diff > 0 else "below")
    note = f"{x} bpm, {abs(diff):.0f} bpm {word} your 30-day median ({base:.0f})" if word != "at" else f"{x} bpm, at your 30-day median"
    return {"score": round(_clamp(score)), "note": note}


def _sleep(row) -> dict | None:
    secs = row["sleep_seconds"]
    if not secs:
        return None
    hours = secs / 3600
    if row["sleep_score"] is not None:
        return {"score": row["sleep_score"], "note": f"{hours:.1f} h, sleep score {row['sleep_score']}"}
    return {"score": round(_clamp((hours - 4) / 4 * 100)), "note": f"{hours:.1f} h (no sleep score)"}


def _body_battery(row) -> dict | None:
    x = row["bb_max"]
    if x is None:
        return None
    return {"score": x, "note": f"charged to {x} overnight"}


def _load(summary: dict) -> dict | None:
    ratio, chronic = summary["ratio"], summary["chronic"]
    if ratio is None:
        return None
    if ratio <= 1.3:
        score = 100 if ratio >= 0.8 else 90
    else:
        score = _clamp(100 - (ratio - 1.3) * 100)
    note = f"7-day load is {ratio:.2f}× your 28-day average"
    if chronic and summary["last_48h"] > 3 * chronic:
        score -= 20
        note += "; big load in the last 48 h"
    return {"score": round(_clamp(score)), "note": note}


def recovery_points(hours_left: float) -> int:
    for (h0, p0), (h1, p1) in zip(RECOVERY_POINTS, RECOVERY_POINTS[1:]):
        if hours_left <= h1:
            return round(p0 + (p1 - p0) * (hours_left - h0) / (h1 - h0))
    return 0


def _recovery(rec: dict | None) -> dict | None:
    if rec is None:
        return None
    a = rec["activity"]
    source = f"{(a['type_key'] or 'activity').replace('_', ' ')} on {a['day']}"
    if rec["hours_left"] <= 0:
        note = f"recovered (last set by {source})"
    else:
        note = f"{rec['hours_left']:.0f} h left from {source}"
    if rec["light"] == "red":
        note += f"; over 36 h caps readiness at {RED_CAP}"
    return {"score": recovery_points(rec["hours_left"]), "note": note}


def score_day(row, history, loads: dict[str, float], rec: dict | None = None) -> dict:
    """Readiness for one day. `history` = earlier days (for baselines); `rec` = recovery time then."""
    day = date.fromisoformat(row["day"])
    load = load_summary(loads, day)
    parts = {
        "hrv": _hrv(row),
        "sleep": _sleep(row),
        "recovery": _recovery(rec),
        "resting_hr": _resting_hr(row, history[-30:]),
        "body_battery": _body_battery(row),
        "load": _load(load),
    }
    present = {k: v for k, v in parts.items() if v}
    total, capped = None, False
    if len(present) >= 2:
        weight = sum(WEIGHTS[k] for k in present)
        total = round(sum(WEIGHTS[k] * v["score"] for k, v in present.items()) / weight)
        if rec and rec["light"] == "red" and total > RED_CAP:
            total, capped = RED_CAP, True
    verdict = next((v for v in VERDICTS if total is not None and total >= v[0]), None)
    return {
        "day": row["day"],
        "score": total,
        "capped": capped,
        "verdict": {"key": verdict[1], "title": verdict[2], "advice": verdict[3]} if verdict else None,
        "components": [
            {"key": k, "weight": WEIGHTS[k], **(v or {"score": None, "note": "no data"})} for k, v in parts.items()
        ],
        "load": load,
    }


def recovery(activities, now: datetime) -> dict | None:
    """Garmin's recovery time, counted down to `now`. Overlapping activities don't add up: like the
    watch, the one that ends last wins. None when no activity before `now` carried a recovery time."""
    best = None
    for a in activities:
        if not a["recovery_min"] or not a["recovery_from"]:
            continue
        start = datetime.fromisoformat(a["recovery_from"])
        if start > now:
            continue
        until = start + timedelta(minutes=a["recovery_min"])
        if best is None or until > best[0]:
            best = (until, a)
    if best is None:
        return None
    until, a = best
    hours_left = max(0.0, (until - now).total_seconds() / 3600)
    light = recovery_light(hours_left)
    title, advice = RECOVERY_LIGHTS[light]
    return {
        "hours_left": round(hours_left, 1),
        "until": until.isoformat(),
        "light": light,
        "title": title,
        "advice": advice,
        "activity": {"name": a["name"], "type_key": a["type_key"], "day": a["day"],
                     "hours": round(a["recovery_min"] / 60, 1)},
    }


def recovery_windows(activities, now: datetime, days: int = 14) -> list[dict]:
    """Each activity's recovery window (end of activity until its recovery time is up) for the timeline.
    The value in a file is the device's total, so an easy session during a longer window just repeats
    the time already left; windows that don't extend recovery by more than an hour are left out."""
    since = now - timedelta(days=days)
    windows, latest_end = [], None
    rows = [a for a in activities if a["recovery_min"] and a["recovery_from"]]
    for a in sorted(rows, key=lambda a: a["recovery_from"]):
        start = datetime.fromisoformat(a["recovery_from"])
        end = start + timedelta(minutes=a["recovery_min"])
        if start > now or end < since:
            continue
        if latest_end and start < latest_end and end <= latest_end + timedelta(hours=1):
            continue
        latest_end = max(latest_end or end, end)
        windows.append({"name": a["name"], "type_key": a["type_key"], "day": a["day"], "start": start.isoformat(),
                        "end": end.isoformat(), "hours": round(a["recovery_min"] / 60, 1)})
    return windows


def _recovery_at(activities, moment: datetime) -> dict | None:
    """Recovery time at `moment`, but only if every activity that could still count has had its file read;
    otherwise (older days, before the app read files) the part is left out rather than guessed."""
    lo = (moment - RECOVERY_WINDOW).date().isoformat()
    hi = moment.date().isoformat()
    if any(lo <= a["day"] <= hi and not a["recovery_checked"] for a in activities):
        return None
    return recovery(activities, moment)


PART_LABELS = {"hrv": "HRV", "sleep": "Sleep", "recovery": "Recovery time", "resting_hr": "Resting HR", "body_battery": "Body Battery", "load": "Training load"}


def chat_context(days, activities, today: date, now: datetime | None = None) -> str | None:
    """A compact plain-text summary of readiness and recent training, for Claude's context."""
    data = build(days, activities, today, now)
    t = data["today"]
    if t is None:
        return None
    lines = ["Readiness and training (the app's own estimate from the athlete's Garmin data):"]
    when = "today" if data["is_today"] else f"{t['day']} (no data for today yet)"
    capped = f" (capped at {RED_CAP}: recovery time is over 36 h)" if t["capped"] else ""
    lines.append(f"Readiness {when}: {t['score']}/100{capped}, {t['verdict']['title']}.")
    for c in t["components"]:
        score = "no data" if c["score"] is None else f"{c['score']}/100"
        lines.append(f"- {PART_LABELS[c['key']]} ({score}): {c['note']}")
    load = t["load"]
    ratio = f"{load['ratio']:.2f}" if load["ratio"] is not None else "n/a"
    lines.append(
        f"Training load (Garmin load, all sports): 7-day avg {load['acute']:.0f}/day, 28-day avg {load['chronic']:.0f}/day, "
        f"ratio {ratio}; load in the last 48 h: {load['last_48h']}."
    )
    rec = data["recovery"]
    if rec:
        act = rec["activity"]
        lines.append(
            f"Recovery time (Garmin's own, from the activity files, counted down): {rec['hours_left']:.0f} h left, "
            f"{rec['light']} ({rec['title'].lower()}; green under 15 h, yellow 15-36 h, red over 36 h). "
            f"From {act['type_key'] or 'activity'} on {act['day']}, which set {act['hours']:.0f} h."
        )
    lines.append("Last 7 days (date: readiness, HRV ms, sleep h, load):")
    for p in data["trend"][-7:]:
        sleep = f"{p['sleep_hours']:.1f}" if p["sleep_hours"] else "-"
        lines.append(f"  {p['day']}: {p['readiness'] or '-'}, {p['hrv'] or '-'}, {sleep}, {p['load']}")
    since = (today - timedelta(days=7)).isoformat()
    recent = [a for a in activities if a["day"] >= since]
    if recent:
        lines.append("Activities in the last 7 days:")
        for a in recent:
            parts = [a["day"], a["type_key"] or "activity"]
            if a["duration_s"]:
                parts.append(f"{round(a['duration_s'] / 60)} min")
            if a["training_load"] is not None:
                parts.append(f"load {round(a['training_load'])}")
            if a["tss"]:
                parts.append(f"TSS {round(a['tss'])}")
            if a["te_label"]:
                parts.append(a["te_label"].lower().replace("_", " "))
            lines.append("  " + ", ".join(parts))
    else:
        lines.append("No activities in the last 7 days.")
    return "\n".join(lines)


def build(days, activities, today: date, now: datetime | None = None) -> dict:
    """Everything the dashboard needs: today's readiness, recovery time and 30 days of trends."""
    rows = list(days)
    activities = list(activities)
    now = now or datetime.now(timezone.utc)
    loads = daily_loads(activities, today - timedelta(days=70), today)
    by_day = {r["day"]: r for r in rows}
    scored = []
    for i, r in enumerate(rows):
        day = date.fromisoformat(r["day"])
        moment = now if day == today else datetime(day.year, day.month, day.day, MORNING_UTC, tzinfo=timezone.utc)
        scored.append(score_day(r, rows[:i], loads, _recovery_at(activities, moment)))
    latest = next((s for s in reversed(scored) if s["score"] is not None), None)
    start = today - timedelta(days=29)
    trend = []
    for i in range(30):
        d = (start + timedelta(days=i)).isoformat()
        r = by_day.get(d)
        s = next((x for x in scored if x["day"] == d), None)
        trend.append({
            "day": d,
            "readiness": s["score"] if s else None,
            "hrv": r["hrv_last_night"] if r else None,
            "hrv_low": r["hrv_balanced_low"] if r else None,
            "hrv_high": r["hrv_balanced_upper"] if r else None,
            "resting_hr": r["resting_hr"] if r else None,
            "sleep_hours": round(r["sleep_seconds"] / 3600, 2) if r and r["sleep_seconds"] else None,
            "sleep_score": r["sleep_score"] if r else None,
            "body_battery": r["bb_max"] if r else None,
            "load": round(loads.get(d, 0.0)),
        })
    return {
        "today": latest,
        "is_today": bool(latest and latest["day"] == today.isoformat()),
        "recovery": recovery(activities, now),
        "recovery_windows": recovery_windows(activities, now),
        "trend": trend,
    }
