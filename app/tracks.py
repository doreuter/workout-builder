"""Recommend running tracks near a start point for a run's intervals, and say how to run them there.

The tracks come from climbdata/switzerland-tracks.json.gz (oval athletics tracks from OpenStreetMap,
built by scripts/build_tracks.py). The plan turns each effort into laps of that track, from the
runner's threshold pace. All computed here, nothing leaves the server.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from . import hills
from .climbdb import haversine
from .howto import minutes, structure, target

DATA = Path(__file__).resolve().parent.parent / "climbdata" / "switzerland-tracks.json.gz"
SEARCH_KM = 25
WALK_FACTOR = 1.25      # straight line -> rough distance on foot
JOG_THERE_KM = 4        # closer than this, jogging there is the warm-up
DEFAULT_PACE = 300      # threshold pace (s/km) to assume when it isn't set: 5:00 /km
JOG = 0.7               # jogging the recoveries, as a fraction of threshold speed

NO_EFFORTS = "This run has no hard efforts. Track suggestions are for interval sessions."

_db: list | None = None


def available() -> bool:
    return DATA.exists()


def _load() -> list:
    global _db
    if _db is None:
        _db = json.load(gzip.open(DATA, "rt"))["tracks"] if DATA.exists() else []
    return _db


def _metres(d: float, i: float, m: float | None, pace: int) -> float:
    """An effort's distance: as set, or from its time at that fraction of threshold pace."""
    return m if m else d * i * 1000 / pace


def laps(m: float, lap: int) -> str:
    """'2½ laps', '1 lap', '¾ lap' (to the quarter); '3 laps + 50 m' on odd tracks."""
    n = m / lap
    q = round(n * 4) / 4
    if lap == 400 or abs(q - n) * lap < 30:
        whole, frac = int(q), q - int(q)
        part = {0.25: "¼", 0.5: "½", 0.75: "¾"}.get(frac, "")
        if not whole:
            return f"{part} lap"
        return f"{whole}{part} lap" + ("" if q == 1 else "s")
    whole = int(n)
    rest = round((m - whole * lap) / 10) * 10
    return f"{whole} lap{'s' if whole != 1 else ''}" + (f" + {rest} m" if rest else "")


def _lap_time(lap: int, i: float, pace: int) -> str:
    s = round(lap / 1000 * pace / i)
    return f"{s // 60}:{s % 60:02d}" if s >= 60 else f"{s} s"


def doing_outside(workout: dict, pace: int | None) -> dict:
    """Running this workout outside, before picking a start place: {"howto": [...], "assumed": {...}},
    or {"message"} when it has no efforts to put on a track."""
    shape = structure(workout, pace)
    efs = shape["efforts"]
    if not efs:
        return {"howto": [], "message": NO_EFFORTS}
    p = pace or DEFAULT_PACE
    i = max(efs, key=lambda e: e[0])[1]
    lines = []
    if shape["warm_s"]:
        lines.append("Warm up with an easy jog to the track, or on it" +
                     (", then the strides or openers on a straight." if shape["openers"] else "."))
    lines.append(f"Run the efforts on a 400 m track: it's flat, measured and has no crossings. At "
                 f"{target('running', i, None, p)} a lap takes about {_lap_time(400, i, p)}.")
    lines.append("Run anti-clockwise. Lane 1 is for the fastest runners: if others are training, use lane 2 "
                 "or further out, and jog the recoveries in the outer lanes.")
    if hills.suits(workout, pace):
        lines.append("Short efforts like these also work as hill repeats: up a steady 5–10 % hill by effort, "
                     "jogging back down as the recovery.")
    steady = [(d, x) for kind, d, x in shape["after"] if kind == "steady"]
    if steady:
        d, x = max(steady)
        lines.append(f"Run the steady {minutes(d)} at {target('running', x, None, p)} off the track, on a "
                     "flat path, then cool down with an easy jog.")
    else:
        lines.append("Cool down with an easy jog, on the track or home.")
    return {"howto": lines, "assumed": {"pace": pace is None, "pace_s": p}}


def recommend(workout: dict, lat: float, lon: float, pace: int | None, limit: int = 4) -> dict:
    """The nearest tracks to (lat, lon), each with this workout's efforts as laps."""
    outside = doing_outside(workout, pace)
    if "message" in outside:
        return {"tracks": [], "message": outside["message"]}
    near = []
    for t in _load():
        km = haversine((lat, lon), t["at"]) / 1000 * WALK_FACTOR
        if km <= SEARCH_KM:
            near.append((km - (0.5 if t["lap_m"] == 400 else 0), km, t))  # a full-size track is worth a bit more
    near.sort(key=lambda x: x[0])
    out = [_describe(t, km, workout, pace or DEFAULT_PACE) for _, km, t in near[:limit]]
    res = {"howto": outside["howto"], "assumed": outside["assumed"], "tracks": out}
    if not out:
        res["message"] = "No running tracks in our database near there. It covers Switzerland."
    return res


def _name(t: dict) -> str:
    return f"{t['name']}, {t['village']}" if t["name"] and t["village"] not in t["name"] else (
        t["name"] or f"Track in {t['village']}")


def _describe(t: dict, km: float, workout: dict, pace: int) -> dict:
    shape = structure(workout, pace)
    efs, lap = shape["efforts"], t["lap_m"]
    # Close by, jogging there is the warm-up; further away the general tips already cover it.
    jog = (f"Jog there as your warm-up: about {km:.1f} km, {minutes(km * pace / 0.65)} easy."
           if km <= JOG_THERE_KM else None)
    lines = [jog] if jog else []
    groups: list = []  # [count, metres, intensity, recovery s, set as a distance?]
    for (d, i, rec), m in zip(efs, shape["effort_m"]):
        dist = _metres(d, i, m, pace)
        if groups and abs(groups[-1][1] - dist) < 20 and abs(groups[-1][2] - i) < 0.005:
            groups[-1][0] += 1
            groups[-1][3] = groups[-1][3] or rec
        else:
            groups.append([1, dist, i, rec, bool(m)])
    for n, dist, i, rec, exact in groups:
        what = f"{n} × " if n > 1 else ""
        size = f"{dist:,.0f} m".replace(",", "'") if exact else f"about {round(dist / 50) * 50:.0f} m"
        line = f"{what}{size}: {laps(dist, lap)} each" if n > 1 else f"{size}: {laps(dist, lap)}"
        line += f", at {target('running', i, None, pace)} ({_lap_time(lap, i, pace)} a lap)"
        if rec:
            line += f"; jog {minutes(rec)} between, about {laps(rec * JOG * 1000 / pace, lap)}"
        lines.append(line + ".")
    main = max(groups, key=lambda g: g[0] * g[1])
    if 0.3 < (main[1] / lap) % 1 < 0.7:  # half laps: start opposite the finish line
        lines.append("Start the efforts on the far side of the track, so each one finishes on the home straight.")
    facts = []
    if t["surface"] == "synthetic":
        facts.append("synthetic surface")
    elif t["surface"]:
        facts.append(f"{t['surface'].replace('_', ' ')} surface")
    if t["lit"]:
        facts.append("floodlit")
    if facts:
        lines.append("The track: " + ", ".join(facts) + ".")
    lines.append("School tracks are usually open outside school hours only; check it's open to the public."
                 if t["school"] else "Check it's open to the public and not booked by a club when you go.")
    return {
        "id": t["id"], "name": _name(t), "distance_km": round(km, 1), "lap_m": lap,
        "surface": t["surface"], "lit": t["lit"], "school": t["school"],
        "summary": jog or "", "plan": lines,
        "at": t["at"], "ring": t["ring"],
    }
