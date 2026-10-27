"""The app's own readiness estimate, built from Garmin data.

Garmin's Personal API doesn't expose Training Readiness, recovery time or training status, so this
module derives a transparent 0-100 score from what it does expose. Each component scores 0-100 and
explains itself; the total is their weighted average (missing components are left out and the
weights rescaled). It is an estimate, labelled as such in the UI - not Garmin's number.
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import median

WEIGHTS = {"hrv": 0.30, "sleep": 0.25, "resting_hr": 0.15, "body_battery": 0.15, "load": 0.15}

VERDICTS = [  # (minimum score, key, title, advice)
    (75, "ready", "Ready for intensity", "Good day for intervals or a hard session."),
    (55, "moderate", "Moderately ready", "Endurance or tempo is fine; keep the hardest work for a better day."),
    (40, "easy", "Take it easy", "An easy Zone 1–2 ride or rest."),
    (0, "recover", "Recovery day", "Rest, or a very easy spin."),
]


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


def score_day(row, history, loads: dict[str, float]) -> dict:
    """Readiness for one day. `history` = earlier days (for baselines)."""
    day = date.fromisoformat(row["day"])
    load = load_summary(loads, day)
    parts = {
        "hrv": _hrv(row),
        "sleep": _sleep(row),
        "resting_hr": _resting_hr(row, history[-30:]),
        "body_battery": _body_battery(row),
        "load": _load(load),
    }
    present = {k: v for k, v in parts.items() if v}
    total = None
    if len(present) >= 2:
        weight = sum(WEIGHTS[k] for k in present)
        total = round(sum(WEIGHTS[k] * v["score"] for k, v in present.items()) / weight)
    verdict = next((v for v in VERDICTS if total is not None and total >= v[0]), None)
    return {
        "day": row["day"],
        "score": total,
        "verdict": {"key": verdict[1], "title": verdict[2], "advice": verdict[3]} if verdict else None,
        "components": [
            {"key": k, "weight": WEIGHTS[k], **(v or {"score": None, "note": "no data"})} for k, v in parts.items()
        ],
        "load": load,
    }


def build(days, activities, today: date) -> dict:
    """Everything the dashboard needs: today's readiness plus 30 days of trends."""
    rows = list(days)
    loads = daily_loads(activities, today - timedelta(days=70), today)
    by_day = {r["day"]: r for r in rows}
    scored = []
    for i, r in enumerate(rows):
        scored.append(score_day(r, rows[:i], loads))
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
    return {"today": latest, "is_today": bool(latest and latest["day"] == today.isoformat()), "trend": trend}
