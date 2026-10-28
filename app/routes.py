"""Suggest an outdoor route that fits a workout, via OpenRouteService (round-trip routing + elevation).

Reaches any user from a start point they pick — no per-user account. ORS is EU-hosted (Heidelberg);
the API key stays on the server (ORS_API_KEY). We ask ORS for a few round-trip loops near the start,
read each one's elevation profile, score it against the workout's main demand, and return the best as a
candidate to *check*, never a route we certify. OSM/Overpass is blocked on the owner's network, so this
is built against recorded responses and exercised live on Render.
"""

from __future__ import annotations

import json
import math
import os
import urllib.request

from .workout import HARD_FROM, flatten

ORS_BASE = "https://api.openrouteservice.org"
PROFILE = "cycling-road"
ASSUMED_KMH = 26.0  # to turn a workout's duration into a rough target distance
SEEDS = (1, 2, 3)   # a few different loops to choose between
MIN_KM, MAX_KM = 8, 120


class RouteError(RuntimeError):
    pass


def available() -> bool:
    return bool(os.environ.get("ORS_API_KEY"))


def tile_key() -> str | None:
    """Thunderforest key for the cycling basemap (a client-side, domain-restricted key)."""
    return os.environ.get("THUNDERFOREST_API_KEY") or None


# --- the one network call (monkeypatched in tests) --------------------------------------

def _directions(lat: float, lon: float, length_m: int, seed: int) -> dict:
    """Ask ORS for one round-trip loop as GeoJSON. Raises RouteError on any failure."""
    key = os.environ.get("ORS_API_KEY")
    if not key:
        raise RouteError("Route suggestions aren't set up on this server.")
    payload = {
        "coordinates": [[lon, lat]],
        "elevation": True,
        "instructions": False,
        "options": {"round_trip": {"length": length_m, "points": 4, "seed": seed}},
    }
    req = urllib.request.Request(
        f"{ORS_BASE}/v2/directions/{PROFILE}/geojson",
        data=json.dumps(payload).encode(),
        headers={"Authorization": key, "Content-Type": "application/json",
                 "Accept": "application/geo+json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RouteError("Route service is busy (rate limit). Try again in a minute.")
        raise RouteError(f"Route service error ({e.code}).")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise RouteError(f"Couldn't reach the route service ({type(e).__name__}).")


# --- profile analysis -------------------------------------------------------------------

def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dphi, dl = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _climb_minutes(length_m: float, grade: float) -> float:
    """Rough climbing time at threshold: slower the steeper it gets."""
    for g, kmh in ((3, 20.0), (5, 15.0), (7, 12.0), (9, 9.5), (11, 8.0)):
        if grade <= g:
            return length_m / 1000 / kmh * 60
    return length_m / 1000 / 6.5 * 60


def _climbs(dist: list[float], ele: list[float]) -> list[dict]:
    """Sustained climbs (>= 30 m gain, >= 3% average) from an ordered distance/elevation profile."""
    n = len(ele)
    if n < 3:
        return []
    sm = list(ele)
    for i in range(1, n - 1):
        sm[i] = (ele[i - 1] + ele[i] + ele[i + 1]) / 3
    climbs, i = [], 0
    while i < n - 1:
        k, peak, top = i, sm[i], i
        while k + 1 < n and sm[k + 1] >= sm[k] - 3:  # tolerate small dips
            k += 1
            if sm[k] > peak:
                peak, top = sm[k], k
        if top > i:
            gain, length = sm[top] - sm[i], dist[top] - dist[i]
            if gain >= 30 and length > 0 and gain / length >= 0.03:
                grade = gain / length * 100
                climbs.append({"start_m": round(dist[i]), "length_m": round(length), "gain_m": round(gain),
                               "grade": round(grade, 1), "minutes": round(_climb_minutes(length, grade))})
            i = max(top, i + 1)
        else:
            i += 1
    return climbs


def profile(feature: dict) -> dict:
    """Distance, ascent, climbs and a map-ready [lat, lon] path from one ORS GeoJSON feature."""
    coords = feature["geometry"]["coordinates"]  # [[lon, lat, ele], ...]
    pts = [(c[1], c[0]) for c in coords]
    ele = [c[2] for c in coords]
    dist = [0.0]
    for a, b in zip(pts, pts[1:]):
        dist.append(dist[-1] + _haversine(a, b))
    ascent = sum(max(0.0, ele[i] - ele[i - 1]) for i in range(1, len(ele)))
    return {
        "distance_m": round(dist[-1]),
        "ascent_m": round(ascent),
        "climbs": _climbs(dist, ele),
        "path": [[round(lat, 5), round(lon, 5)] for lat, lon in pts],
    }


# --- matching a route to the workout ----------------------------------------------------

def demand(workout: dict, pace: int | None) -> dict:
    """What the session needs from the terrain."""
    segs = flatten(workout, pace)
    hard_from = HARD_FROM["cycling"]
    hard = [s for s in segs if max(s["intensity"] or 0, s["intensity_end"] or 0) >= hard_from and s["duration_s"] >= 30]
    longest = max((s["duration_s"] for s in hard), default=0)
    total = sum(s["duration_s"] for s in segs)
    return {"has_hard": bool(hard), "longest_min": longest / 60, "sustained": longest >= 300, "total_s": total}


def target_distance_m(workout: dict, pace: int | None) -> int:
    total_s = sum(s["duration_s"] for s in flatten(workout, pace))
    km = min(MAX_KM, max(MIN_KM, total_s / 3600 * ASSUMED_KMH))
    return round(km * 1000)


def score(prof: dict, dem: dict, target_m: int) -> float:
    """Higher is a better fit. Distance always matters; terrain depends on the session type."""
    s = 100.0 - abs(prof["distance_m"] - target_m) / target_m * 40
    climbs, ascent = prof["climbs"], prof["ascent_m"]
    if dem["sustained"]:
        if climbs:
            best = max(climbs, key=lambda c: c["gain_m"])
            want = dem["longest_min"] or 1
            s += 30 * max(0.0, 1 - abs(best["minutes"] - want) / want)
            s += min(ascent, 800) / 800 * 10
        else:
            s -= 30  # sustained efforts with nowhere to climb
    elif not dem["has_hard"]:  # endurance: some rolling is nice, distance dominates
        per_km = ascent / max(1, prof["distance_m"] / 1000)
        s += 10 - min(abs(per_km - 8), 10)
    else:  # short, sharp reps: prefer flat so junctions and gradient don't break them
        s -= min(ascent, 1000) / 1000 * 20
    return round(s, 1)


def _summary(prof: dict, dem: dict) -> str:
    km = prof["distance_m"] / 1000
    ascent = prof["ascent_m"]
    if dem["sustained"] and prof["climbs"]:
        c = max(prof["climbs"], key=lambda c: c["gain_m"])
        return (f"A {km:.1f} km loop with {ascent} m of climbing. Do the hard efforts on the "
                f"{c['length_m'] / 1000:.1f} km climb at ~{c['grade']}% (about {c['minutes']} min).")
    if not dem["has_hard"]:
        return f"A {km:.1f} km rolling loop with {ascent} m of climbing — steady all the way round."
    return f"A {km:.1f} km loop kept fairly flat ({ascent} m climbing), so the reps aren't broken by hills."


def suggest_route(workout: dict, pace: int | None, lat: float, lon: float,
                  distance_km: float | None = None, seeds=SEEDS) -> dict:
    """Pick the best-fitting round-trip loop near (lat, lon) for the workout. Raises RouteError."""
    target_m = round(distance_km * 1000) if distance_km else target_distance_m(workout, pace)
    dem = demand(workout, pace)
    best, errors = None, []
    for seed in seeds:
        try:
            geo = _directions(lat, lon, target_m, seed)
            feature = geo["features"][0]
        except (RouteError, KeyError, IndexError) as e:
            errors.append(str(e))
            continue
        prof = profile(feature)
        cand = {**prof, "score": score(prof, dem, target_m), "seed": seed}
        if best is None or cand["score"] > best["score"]:
            best = cand
    if best is None:
        raise RouteError(errors[0] if errors else "No route could be generated near there.")
    best["summary"] = _summary(best, dem)
    best["target_km"] = round(target_m / 1000, 1)
    return best
