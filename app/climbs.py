"""Recommend climbs near a start point for a workout's intervals, and say how to ride them.

The climbs come from our database (climbdata/, built by scripts/build_climbs.py). For each climb we
work out where the efforts go, from the rider's FTP and weight and the climb's profile, and pick one of
two ways to ride it:

- repeats: ride the effort uphill, turn, roll back down as the recovery, again from the same spot;
- up the climb: on a climb long enough for the whole block, do the efforts back-to-back going up.

The efforts go on the stretch that suits them best: a steady gradient right for the intensity, with no
traffic lights or stop signs to break them. All computed here, nothing leaves the server.
"""

from __future__ import annotations

import bisect
import gzip
import heapq
import json
import math
from pathlib import Path

from .climbdb import haversine, path_points
from .howto import minutes as _minutes
from .howto import structure

DATA = Path(__file__).resolve().parent.parent / "climbdata" / "central-ch.json.gz"
ROADS = DATA.with_name("central-ch-roads.json.gz")  # bigger roads, for distances by road
PLACES = DATA.with_name("central-ch-places.json.gz")  # towns, villages, hamlets: start points by name
SEARCH_KM = 30          # straight-line radius searched around the start
MAX_RIDE_KM = 45        # by road
ROAD_FACTOR = 1.3       # straight line -> rough road distance, where the road graph can't help
APPROACH_KMH = 25       # easy riding to the climb
EASY = 0.55             # % FTP while recovering on the way up
BIKE_KG = 8
DEFAULT_KG = 75
DEFAULT_WKG = 3.3       # FTP to assume per kg when it isn't set
BREAKS = ("signal", "stop", "give_way", "rail", "roundabout")

_db: list | None = None
_roads: dict | None = None
_places: list | None = None


def available() -> bool:
    return DATA.exists()


def _load() -> list:
    global _db
    if _db is None:
        _db = json.load(gzip.open(DATA, "rt"))["climbs"] if DATA.exists() else []
    return _db


def find_places(query: str, limit: int = 8) -> list:
    """Start points by name: names starting with the query first, towns and villages before hamlets."""
    global _places
    if _places is None:
        _places = json.load(gzip.open(PLACES, "rt")) if PLACES.exists() else []
    q = query.strip().lower()
    if len(q) < 2:
        return []
    rank = {"city": 0, "town": 0, "village": 1, "suburb": 2, "hamlet": 3}
    hits = [p for p in _places if q in p[0].lower()]
    hits.sort(key=lambda p: (not p[0].lower().startswith(q), rank.get(p[3], 4), len(p[0]), p[0]))
    out, seen = [], set()
    for name, lat, lon, kind, near in hits:
        label = f"{name} (near {near})" if near else name
        if label in seen:
            continue
        seen.add(label)
        out.append({"label": label, "name": name, "lat": lat, "lon": lon})
        if len(out) == limit:
            break
    return out


def _road_graph() -> dict | None:
    global _roads
    if _roads is None and ROADS.exists():
        g = json.load(gzip.open(ROADS, "rt"))
        adj = [[] for _ in g["nodes"]]
        for u, v, length in g["edges"]:
            adj[u].append((v, length))
            adj[v].append((u, length))
        grid = {}
        for i, (la, lo) in enumerate(g["nodes"]):
            grid.setdefault((int(la * 100), int(lo * 100)), []).append(i)
        _roads = {"nodes": g["nodes"], "adj": adj, "grid": grid}
    return _roads


def road_km(lat: float, lon: float) -> dict:
    """Road distance (km) from (lat, lon) to every junction of the road graph within MAX_RIDE_KM, keyed
    by node; empty if there's no graph. Rough: bigger roads only, plus straight hops to get onto them."""
    g = _road_graph()
    if not g:
        return {}
    near = []
    for r in range(6):
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                for i in g["grid"].get((int(lat * 100) + dx, int(lon * 100) + dy), ()):
                    near.append((haversine((lat, lon), g["nodes"][i]) * ROAD_FACTOR, i))
        if len(near) >= 4:
            break
    if not near:
        return {}
    dist, q = {}, []
    for d, i in sorted(set(near))[:4]:  # a few nearby junctions, in case the nearest is a dead end
        dist[i] = min(dist.get(i, 1e18), d)
        q.append((d, i))
    heapq.heapify(q)
    while q:
        d, n = heapq.heappop(q)
        if d > dist.get(n, 1e18) or d > MAX_RIDE_KM * 1000:
            continue
        for m, length in g["adj"][n]:
            nd = d + length
            if nd < dist.get(m, 1e18):
                dist[m] = nd
                heapq.heappush(q, (nd, m))
    return {n: d / 1000 for n, d in dist.items()}


# --- the workout ------------------------------------------------------------------------

def efforts(workout: dict, pace: int | None = None) -> tuple[int, list]:
    """(warm-up seconds before the main set, [(seconds, intensity, recovery seconds after)]). Openers in the
    warm-up aren't efforts here: they're done on the way (see app/howto.structure)."""
    shape = structure(workout, pace)
    return shape["warm_s"], shape["efforts"]


# --- riding physics ---------------------------------------------------------------------------

def speed(grade: float, watts: float, mass: float) -> float:
    """m/s at steady power on a gradient (rolling resistance and air drag of an upright road bike)."""
    th = math.atan(grade)
    lo, hi = 0.1, 25.0
    for _ in range(40):
        v = (lo + hi) / 2
        p = v * mass * 9.81 * (math.sin(th) + 0.004 * math.cos(th)) + 0.5 * 1.15 * 0.32 * v ** 3
        lo, hi = (v, hi) if p < watts else (lo, v)
    return v


def _down_speed(grade: float) -> float:
    """m/s rolling back down a stretch that climbs at `grade` (cautious: ~20 km/h flat, 50 max)."""
    return min(50, 20 + 400 * max(grade, 0)) / 3.6


class _Profile:
    """A climb's points with cumulative riding times, for finding where an effort of N seconds ends."""

    def __init__(self, climb: dict, mass: float):
        self.c = climb
        self.pts = path_points(climb)
        self.d = [p[3] for p in self.pts]
        self.z = [p[2] for p in self.pts]
        self.g = [((self.z[i + 1] - self.z[i]) / (self.d[i + 1] - self.d[i]) if self.d[i + 1] > self.d[i] else 0.0)
                  for i in range(len(self.pts) - 1)]
        self.mass = mass
        self._up: dict = {}
        self.down = [0.0]
        for i, gr in enumerate(self.g):
            self.down.append(self.down[-1] + (self.d[i + 1] - self.d[i]) / _down_speed(gr))
        self.breaks = sorted(s["at_m"] for s in climb["interruptions"] if s["kind"] in BREAKS)

    def up(self, watts: float) -> list:
        """Cumulative seconds from the foot to each point, riding at `watts`."""
        key = round(watts)
        if key not in self._up:
            t = [0.0]
            for i, gr in enumerate(self.g):
                t.append(t[-1] + (self.d[i + 1] - self.d[i]) / speed(gr, watts, self.mass))
            self._up[key] = t
        return self._up[key]

    def after(self, i: int, seconds: float, watts: float) -> int | None:
        """Index reached riding `seconds` at `watts` from point i, or None past the top."""
        t = self.up(watts)
        j = bisect.bisect_left(t, t[i] + seconds)
        return j if j < len(t) else None

    def section(self, i: int, j: int, target: float) -> float:
        """How well the stretch i..j suits an effort: gradient near `target` %, steady, unbroken."""
        if j <= i:
            return -1e9
        avg = (self.z[j] - self.z[i]) / (self.d[j] - self.d[i]) * 100
        win = [(self.z[min(k + 5, j)] - self.z[k]) / max(1.0, self.d[min(k + 5, j)] - self.d[k]) * 100
               for k in range(i, max(i + 1, j - 4), 5)]
        spread = (sum((w - avg) ** 2 for w in win) / len(win)) ** 0.5 if len(win) > 1 else 0.0
        breaks = sum(1 for b in self.breaks if self.d[i] + 30 < b < self.d[j] - 30)
        return 30 * max(0.0, 1 - abs(avg - target) / 4) + 10 * max(0.0, 1 - spread / 4) - 25 * breaks

    def grade(self, i: int, j: int) -> float:
        return (self.z[j] - self.z[i]) / (self.d[j] - self.d[i]) * 100 if self.d[j] > self.d[i] else 0.0


def target_grade(intensity: float) -> float:
    """The gradient (%) that suits an effort: gentler for sweet spot, steeper for VO2 max."""
    return min(9.0, max(4.5, 5 + 15 * (intensity - 0.9)))


# --- planning one climb ---------------------------------------------------------------------

def plan(climb: dict, warm_s: int, efs: list, ftp: float, mass: float) -> dict | None:
    """The best way to ride `efs` on this climb, or None if even one effort doesn't fit."""
    pr = _Profile(climb, mass)
    n = len(pr.pts)
    longest = max(efs, key=lambda e: e[0])
    target = target_grade(longest[1])
    starts = range(0, n - 1, 5)  # every ~100 m

    easy = pr.up(EASY * ftp)
    climb_in = lambda s: max(0.0, easy[s] - 120) / 60  # noqa: E731  (minutes of easy climbing before the work)

    best_rep = None
    for s in starts:
        e = pr.after(s, longest[0], longest[1] * ftp)
        if e is None:
            break
        sc = pr.section(s, e, target) - 0.8 * climb_in(s)
        if best_rep is None or sc > best_rep[0]:
            best_rep = (sc, s, e)
    if best_rep is None:
        return None

    best_up = None
    if len(efs) > 1:
        for s in starts:
            i, spans, ok = s, [], True
            for k, (dur, inten, rec) in enumerate(efs):
                j = pr.after(i, dur, inten * ftp)
                if j is None:
                    ok = False
                    break
                spans.append((i, j))
                if k < len(efs) - 1:
                    i = pr.after(j, rec, EASY * ftp)
                    if i is None:
                        ok = False
                        break
            if not ok:
                break
            sc = (sum(pr.section(a, b, target_grade(efs[k][1])) for k, (a, b) in enumerate(spans)) / len(spans)
                  + 8 - 0.8 * climb_in(s))  # no turning around: nicer on a long climb
            if best_up is None or sc > best_up[0]:
                best_up = (sc, spans)

    if best_up and best_up[0] >= best_rep[0] - 3:
        mode, score, spans = "up", best_up[0], best_up[1]
    else:
        mode, score, spans = "repeats", best_rep[0], [(best_rep[1], best_rep[2])]
    return {"mode": mode, "score": score, "spans": spans, "profile": pr}


# --- recommending -----------------------------------------------------------------------------

NO_EFFORTS = "This workout has no hard efforts. Climb suggestions are for interval sessions."


def doing_outside(workout: dict, ftp: int | None, weight: int | None, pace: int | None = None) -> dict:
    """Riding this workout outside, before picking a start place: {"howto": [...], "assumed": {...}},
    or {"message"} when it has no efforts to put on a climb. Shown on every ride."""
    shape = structure(workout, pace)
    efs = shape["efforts"]
    if not efs:
        return {"howto": [], "message": NO_EFFORTS}
    kg = weight or DEFAULT_KG
    watts = ftp or round(DEFAULT_WKG * kg)
    howto = outside_tips(shape["warm_s"], efs, bool(shape["openers"]), watts, kg + BIKE_KG)
    steady = [(d, i) for kind, d, i in shape["after"] if kind == "steady"]
    if steady:
        d, i = max(steady)
        howto[-1] = (f"On the way home ride the steady {_minutes(d)} at about {round(i * watts)} W on "
                     "flat or rolling roads, then cool down easy.")
    return {"howto": howto, "assumed": {"weight": weight is None, "ftp": ftp is None, "ftp_w": watts, "weight_kg": kg}}


def recommend(workout: dict, lat: float, lon: float, ftp: int | None, weight: int | None,
              pace: int | None = None, limit: int = 4) -> dict:
    """The best climbs near (lat, lon) for this workout's efforts, each with a plan in plain words."""
    warm_s, efs = efforts(workout, pace)
    if not efs:
        return {"climbs": [], "message": NO_EFFORTS}
    kg = weight or DEFAULT_KG
    watts = ftp or round(DEFAULT_WKG * kg)
    mass = kg + BIKE_KG
    longest = max(efs, key=lambda e: e[0])
    target = target_grade(longest[1])
    outside = doing_outside(workout, ftp, weight, pace)
    howto, assumed = outside["howto"], outside["assumed"]

    by_road = road_km(lat, lon)
    near = []
    for c in _load():
        straight = haversine((lat, lon), c["foot"]) / 1000
        if straight > SEARCH_KM:
            continue
        if by_road:
            if c.get("foot_node") not in by_road:
                continue  # not reachable by road within MAX_RIDE_KM (across a lake, say)
            km = by_road[c["foot_node"]] + c.get("foot_off_m", 0) * ROAD_FACTOR / 1000
        else:
            km = straight * ROAD_FACTOR
        if km > MAX_RIDE_KM:
            continue
        # quick check before planning: is the whole climb long enough for the longest effort?
        if c["length_m"] / speed(c["avg"] / 100, longest[1] * watts, mass) < longest[0] * 0.95:
            continue
        rough = 30 * max(0.0, 1 - abs(c["avg"] - target) / 4) - 2 * sum(
            1 for s in c["interruptions"] if s["kind"] in BREAKS) - 0.8 * km
        near.append((rough, km, c))
    if not near:
        return {"howto": howto, "climbs": [], "assumed": assumed,
                "message": "No climbs in our database near there yet. It covers about 50 km around Goldau "
                           "in Central Switzerland."}
    near.sort(key=lambda x: -x[0])

    ideal_km = warm_s / 3600 * APPROACH_KMH
    picked = []
    for _, ride_km, c in near[:150]:
        p = plan(c, warm_s, efs, watts, mass)
        if not p:
            continue
        score = p["score"]
        score += 4 * c.get("bike_route", 0)
        score -= 8 if c["classes"].get("primary", 0) > 0.3 else 0
        # Close is good: the ride there is the warm-up; much further and it's a big outing.
        score -= 1.2 * max(0.0, ride_km - max(8.0, 1.5 * ideal_km))
        picked.append((score, ride_km, c, p))
    picked.sort(key=lambda x: -x[0])

    out = []
    for score, ride_km, c, p in picked:
        a = p["profile"].pts[p["spans"][0][0]]
        if any(haversine(a[:2], o["work_start"]) < 600 for o in out):
            continue  # another way onto the same stretch of road
        out.append(_describe(c, p, efs, warm_s, ride_km, watts, round(score, 1)))
        if len(out) == limit:
            break
    return {"howto": howto, "climbs": out, "assumed": assumed}


def outside_tips(warm_s: int, efs: list, openers: bool, ftp: float, mass: float) -> list:
    """Riding it outside, before any particular climb: where the efforts go and how to recover there.
    (How to do the workout itself is on every workout: app/howto.py.)"""
    longest = max(efs, key=lambda e: e[0])
    t = target_grade(longest[1])
    km = speed(t / 100, longest[1] * ftp, mass) * longest[0] / 1000
    lines = []
    if warm_s:
        lines.append("Warm up on the way to the climb" + (", openers included: any short rise will do." if openers else "."))
    lines.append(f"Do the main efforts uphill: a climb holds the power steady and has no stops. Look for a steady "
                 f"{t - 1:.0f}–{t + 1:.0f}% that takes at least {_minutes(longest[0])} at that power, about {km:.1f} km.")
    if len(efs) > 1:
        lines.append("Between efforts, turn and roll back down to where you started; on a long climb you can "
                     "instead spin easy and keep going up.")
    lines.append("Cool down on the way home.")
    return lines


# --- plain-language plan ----------------------------------------------------------------------

def _where(c: dict, pr: _Profile, i: int) -> str:
    """'km 2.1 (845 m) near Alosen' / 'km 2.1 (845 m) on Ratenstrasse' / 'km 2.1 (845 m)'."""
    x, z = pr.d[i], pr.z[i]
    spot = f"km {x / 1000:.1f} ({z:,.0f} m)".replace(",", "'")
    if x < 50:
        spot = f"the bottom ({z:,.0f} m)".replace(",", "'")
    near = [name for at, name in c.get("landmarks", []) if abs(at - x) <= 250]
    if near:
        return f"{spot} near {near[0]}"
    street = None
    for at, name in c.get("streets_at", []):
        if at <= x + 1:
            street = name
    return f"{spot} on {street}" if street else spot


def _name(c: dict) -> str:
    """'Oberägeri → Raten'; when both ends are in the same village, 'Lauerz, Langerlistrasse'."""
    if c["to"] and c["to"] != c["from"]:
        return f"{c['from'] or '?'} → {c['to']}"
    street = c["streets"][-1] if c.get("streets") else None
    return f"{c['from']}, {street}" if c["from"] and street else (c["from"] or street or "Climb")


def _describe(c: dict, p: dict, efs: list, warm_s: int, ride_km: float, ftp: float, score: float) -> dict:
    pr, spans = p["profile"], p["spans"]
    lines = [f"Ride there as your warm-up: about {ride_km:.0f} km, {_minutes(ride_km / APPROACH_KMH * 3600)} easy."
             + (" Add a few easy minutes before the first effort." if warm_s > ride_km / APPROACH_KMH * 3600 + 300 else "")]
    same = len({(e[0], round(e[1], 2)) for e in efs}) == 1
    first = spans[0][0]
    if pr.d[first] > 300:
        lines.append(f"Ride easy up the first {pr.d[first] / 1000:.1f} km to the start of the work "
                     f"(about {_minutes(pr.up(EASY * ftp)[first])}).")
    if p["mode"] == "repeats":
        s, e = spans[0]
        dur, inten, _ = max(efs, key=lambda x: x[0])
        down = pr.down[e] - pr.down[s]
        recs = [r for _, _, r in efs[:-1]]
        rec = sorted(recs)[len(recs) // 2] if recs else 0
        line = (f"Start each effort at {_where(c, pr, s)}. Ride {_minutes(dur)} at {round(inten * ftp)} W, which takes you "
                f"to about {_where(c, pr, e)}. Turn and roll back down, about {_minutes(down)}")
        if rec and rec - down >= 60:
            line += f"; spin easy at the bottom for another {_minutes(rec - down)}"
        elif rec and down - rec >= 90:
            line += f", a bit longer than the planned {_minutes(rec)}"
        lines.append(line + ".")
        lines.append(f"That's one effort and its recovery: do it {len(efs)} times in all." if same else
                     f"That's one effort and its recovery: do all {len(efs)} like that, turning when each "
                     "one's time is up (shorter ones turn earlier).")
    else:
        lines.append("Do the efforts back-to-back on the way up, spinning easy in between:")
        for k, (a, b) in enumerate(spans):
            dur, inten, rec = efs[k]
            lines.append(f"{k + 1}. {_minutes(dur)} at {round(inten * ftp)} W, from {_where(c, pr, a)} "
                         f"to {_where(c, pr, b)}" + (f"; then {_minutes(rec)} easy" if k < len(spans) - 1 else ""))
        last = spans[-1][1]
        rest = pr.d[-1] - pr.d[last]
        lines.append(f"From there it's {rest / 1000:.1f} km more to the top at {c['to'] or 'the top'}; ride on easy or turn "
                     "and roll back down." if rest > 300 else f"That's the top: {c['to'] or 'the top'}. Roll back down easy.")
    work = [pr.grade(a, b) for a, b in spans]
    lights = [x for x in pr.breaks if any(pr.d[a] <= x <= pr.d[b] for a, b in spans)]
    lines.append(f"Where you work: about {sum(work) / len(work):.1f}% "
                 + ("with no traffic lights or stop signs." if not lights else
                    f"with {len(lights)} traffic light or stop sign on the way; take it easy there."))
    lines.append("Afterwards ride home easy as your cool-down.")
    if p["mode"] == "repeats":
        s, e = spans[0]
        summary = f"Efforts from {_where(c, pr, s)} to {_where(c, pr, e)}, then turn and roll back down."
    else:
        summary = (f"Efforts back-to-back going up, from {_where(c, pr, spans[0][0])} to "
                   f"{_where(c, pr, spans[-1][1])}.")
    return {
        "id": c["id"], "name": _name(c), "from": c["from"], "to": c["to"], "summary": summary,
        "distance_km": round(ride_km), "length_m": c["length_m"], "gain_m": c["gain_m"], "avg": c["avg"],
        "top_ele": c["top_ele"], "lights": sum(1 for s in c["interruptions"] if s["kind"] == "signal"),
        "mode": p["mode"], "plan": lines,
        "sections": [[round(pr.d[a]), round(pr.d[b])] for a, b in spans],
        "turn_m": round(pr.d[spans[0][1]]) if p["mode"] == "repeats" else None,
        "foot": c["foot"], "top": c["top"], "work_start": list(pr.pts[spans[0][0]][:2]),
        "path": [[round(la, 5), round(lo, 5), round(z), round(x)] for la, lo, z, x in pr.pts],
        "score": score,
    }
