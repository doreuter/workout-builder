"""Hills for a run's hill repeats: steady stretches of quiet road near a start point, from the climb
database (app/climbs.py), with a plan for running the efforts up and jogging back down.

Uphill, the same effort is slower: we take running cost to rise about 5 % per 1 % of gradient, so an
effort's distance up the hill follows from its time, its intensity and the runner's threshold pace.
All computed here, nothing leaves the server.
"""

from __future__ import annotations

from . import climbs
from .climbdb import haversine, path_points
from .howto import minutes, structure, target

SEARCH_KM = 8           # straight line around the start
WALK_FACTOR = 1.25      # straight line -> rough distance on foot
JOG_THERE_KM = 4
DEFAULT_PACE = 300      # threshold pace (s/km) to assume when it isn't set
MAX_EFFORT_S = 6 * 60   # longer efforts are for a track or a flat road, not hill repeats
GRADE = (4.0, 12.0)     # % that suits repeats
IDEAL = 7.0
COST_PER_PCT = 0.05     # uphill running cost per % gradient
DOWN_MS = 2.8           # jogging back down, m/s
QUIET = {"residential", "unclassified", "track", "service", "cycleway", "living_street", "path", "footway"}
STOPS = ("signal", "stop", "rail")


def suits(workout: dict, pace: int | None) -> bool:
    """Hill repeats suit runs whose efforts are all short."""
    efs = structure(workout, pace)["efforts"]
    return bool(efs) and max(d for d, _, _ in efs) <= MAX_EFFORT_S


def up_metres(d: float, i: float, pace: int, grade: float) -> float:
    return d * i * 1000 / pace / (1 + COST_PER_PCT * grade)


def _quiet(c: dict) -> bool:
    return sum(v for k, v in c["classes"].items() if k in QUIET) >= 0.7


def best_section(c: dict, need: float):
    """The best stretch of climb c for repeats of `need` metres uphill: (score, start index, end index,
    grade %), or None. Steady gradient near IDEAL, with no lights, stop signs or rail crossings on it."""
    p = path_points(c)
    step = p[1][3] - p[0][3] if len(p) > 1 else 20
    span = max(2, round((need + 30) / step))  # a little spare at the top
    stops = [s["at_m"] for s in c["interruptions"] if s["kind"] in STOPS] + c.get("junctions_at_m", [])
    best = None
    for a in range(0, len(p) - span):
        b = a + span
        length = p[b][3] - p[a][3]
        g = (p[b][2] - p[a][2]) / length * 100
        if not GRADE[0] <= g <= GRADE[1] or any(p[a][3] - 10 <= x <= p[b][3] for x in stops):
            continue
        k = max(1, round(60 / step))  # how steady: grades over 60 m pieces
        pieces = [(p[j + k][2] - p[j][2]) / (p[j + k][3] - p[j][3]) * 100 for j in range(a, b - k + 1, k)]
        dev = max(abs(x - g) for x in pieces) if pieces else 0
        if dev > 5:
            continue
        score = -abs(g - IDEAL) - 0.8 * dev - 0.002 * p[a][3]  # nearer the bottom is a bit easier to find
        if best is None or score > best[0]:
            best = (score, a, b, g)
    return best


def _street(c: dict, at: float) -> str | None:
    street = None
    for x, name in c.get("streets_at", []):
        if x <= at + 1:
            street = name
    return street


def recommend(workout: dict, lat: float, lon: float, pace: int | None, limit: int = 4) -> list:
    """The best hills near (lat, lon) for this run's efforts, each with a plan; [] when the efforts are
    too long for hill repeats."""
    if not suits(workout, pace):
        return []
    p = pace or DEFAULT_PACE
    efs = structure(workout, pace)["efforts"]
    d, i, _ = max(efs, key=lambda e: e[0])
    found = []
    for c in climbs._load():
        km = haversine((lat, lon), c["foot"]) / 1000 * WALK_FACTOR
        if km > SEARCH_KM * WALK_FACTOR or not _quiet(c) or c["avg"] < GRADE[0] - 1:
            continue
        need = up_metres(d, i, p, IDEAL)
        sec = best_section(c, need)
        if not sec:
            continue
        score, a, b, g = sec
        pts = path_points(c)
        at = pts[a][:2]
        km = haversine((lat, lon), at) / 1000 * WALK_FACTOR
        found.append((score - 1.5 * max(0.0, km - 1.0), km, c, a, b, g))  # close by beats a bit steadier
    found.sort(key=lambda x: -x[0])
    out = []
    for _, km, c, a, b, g in found:
        pts = path_points(c)
        h = _describe(c, pts, a, b, g, km, efs, p)
        if any(haversine(pts[a][:2], o["foot"]) < 300 or o["street"] == h["street"] for o in out):
            continue  # another climb sharing the same stretch or street
        out.append(h)
        if len(out) == limit:
            break
    return out


def _describe(c: dict, pts: list, a: int, b: int, g: float, km: float, efs: list, pace: int) -> dict:
    start, end = pts[a][3], pts[b][3]
    street = _street(c, start) or c["from"] or "the hill"
    name = f"{street}, {c['from']}" if c["from"] and c["from"] not in street else street
    d, i, _ = max(efs, key=lambda e: e[0])
    up = up_metres(d, i, pace, g)
    down = up / DOWN_MS
    recs = [r for _, _, r in efs[:-1]]
    rec = sorted(recs)[len(recs) // 2] if recs else 0
    same = len({(e[0], round(e[1], 2)) for e in efs}) == 1
    jog = (f"Jog there as your warm-up: about {km:.1f} km, {minutes(km * pace / 0.65)} easy."
           if km <= JOG_THERE_KM else None)
    lines = [jog] if jog else []
    if start > 100:
        metres = f"{start:,.0f}".replace(",", "'")
        lines.append(f"The repeats start {metres} m up {street}, where it steadies at about {g:.0f} %.")
    line = (f"Run {minutes(d)} up at the effort of {target('running', i, None, pace)}: by feel, not pace, as "
            f"uphill you'll be slower. That's about {round(up / 10) * 10:.0f} m. Jog back down to the start, "
            f"about {minutes(down)}")
    if rec and rec - down >= 45:
        line += f", then walk or jog on the flat for another {minutes(rec - down)}"
    lines.append(line + ".")
    lines.append(f"Do it {len(efs)} times in all." if same else
                 f"Do all {len(efs)} like that; shorter efforts turn sooner.")
    lines.append("Run tall and drive your arms; keep the last repeat as strong as the first.")
    lines.append("It's a road: keep to the side and watch for traffic, especially jogging back down.")
    sec = [p for p in pts if start - 1 <= p[3] <= end + 1]
    return {
        "id": f"{c['id']}-{a}", "name": name, "distance_km": round(km, 1), "length_m": round(end - start),
        "gain_m": round(sec[-1][2] - sec[0][2]), "avg": round(g, 1), "top_ele": round(sec[-1][2]), "lights": 0,
        "to": _street(c, end), "street": street,
        "summary": f"about {round(min(up, end - start) / 10) * 10:.0f} m up at {g:.0f} %" + (f" · {jog}" if jog else ""),
        "plan": lines, "foot": list(sec[0][:2]), "top": list(sec[-1][:2]),
        "turn_m": round(min(up, end - start)), "sections": [[0, round(min(up, end - start))]],
        "path": [[round(la, 5), round(lo, 5), round(z), round(x - start)] for la, lo, z, x in sec],
        "walk": True,
    }
