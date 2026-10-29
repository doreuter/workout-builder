"""Build the climb database for a region from an OpenStreetMap extract and Swisstopo elevation.

Run offline (not on the server). Needs `pip install -r requirements-build.txt` (osmium) and:
  - an OSM extract, e.g. https://download.geofabrik.de/europe/switzerland-latest.osm.pbf
  - network access to Swisstopo's profile service (api3.geo.admin.ch), used for elevation

    python scripts/build_climbs.py switzerland.osm.pbf climbdata/central-ch.json.gz \\
        --center 47.0504 8.5466 --radius-km 50 --cache /tmp/climb-elevation-cache.jsonl

Elevation answers are cached in --cache, so a rebuild only asks for roads it hasn't seen. The output is
a gzip'd JSON list of climbs (see climbdata/README.md); the app reads it, never this script.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.climbdb import Network, place_name  # noqa: E402

ROAD = {"primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link",
        "unclassified", "residential", "track", "service", "cycleway"}
PAVED = {"asphalt", "concrete", "paved", "concrete:plates", "paving_stones", "chipseal", "sett"}
UNPAVED = {"gravel", "fine_gravel", "dirt", "ground", "grass", "compacted", "unpaved", "earth", "mud", "sand",
           "pebblestone", "rock", "woodchips", "grass_paver"}
STOPS = {"traffic_signals": "signal", "stop": "stop", "give_way": "give_way", "mini_roundabout": "roundabout"}
PLACE_KINDS = {"city", "town", "village", "suburb", "hamlet", "locality", "neighbourhood"}
PROFILE_URL = "https://api3.geo.admin.ch/rest/services/profile.json"
BATCH_M = 14000      # road per elevation request
SPACING_M = 14       # elevation sample spacing


def rideable(t: dict) -> bool:
    """Paved and open to bikes: no motorways/trunk roads, bike bans, private or gravel roads. Road tunnels
    stay: pass roads run through them (the climb finder rides straight through)."""
    hw = t.get("highway")
    if hw not in ROAD:
        return False
    if t.get("bicycle") in ("no", "dismount"):
        return False
    if t.get("access") in ("no", "private") and t.get("bicycle") not in ("yes", "designated", "permissive"):
        return False
    surface = t.get("surface")
    if surface in UNPAVED:
        return False
    if hw in ("track", "service", "cycleway"):  # only when known to be paved (Swiss farm roads often are)
        if hw == "service" and t.get("service") in ("parking_aisle", "driveway", "drive-through"):
            return False
        return surface in PAVED or t.get("tracktype") == "grade1"
    return True


def read_osm(pbf: str, lat: float, lon: float, radius_km: float) -> dict:
    import osmium

    dlat, dlon = radius_km / 111.32, radius_km / (111.32 * math.cos(math.radians(lat)))
    inside = lambda la, lo: abs(la - lat) <= dlat and abs(lo - lon) <= dlon  # noqa: E731
    ways, special, passes, places, bike_ways = [], {}, [], [], set()
    for o in osmium.FileProcessor(pbf).with_locations().with_filter(osmium.filter.EmptyTagFilter()):
        if o.is_node():
            loc = o.location
            if not loc.valid() or not inside(loc.lat, loc.lon):
                continue
            t = o.tags
            kind = STOPS.get(t.get("highway"))
            if t.get("crossing") == "traffic_signals":
                kind = "signal"
            if t.get("railway") == "level_crossing":
                kind = "rail"
            if kind:
                special[o.id] = kind
            name = t.get("name")
            if name and (t.get("mountain_pass") == "yes" or t.get("natural") == "saddle"):
                passes.append([round(loc.lat, 6), round(loc.lon, 6), name])
            if name and (t.get("place") in PLACE_KINDS or t.get("natural") == "peak"):
                places.append([round(loc.lat, 6), round(loc.lon, 6), name,
                               t.get("place") if t.get("place") in PLACE_KINDS else "peak"])
        elif o.is_way():
            t = dict(o.tags)
            if not rideable(t):
                continue
            try:
                pts = [(n.ref, n.location.lat, n.location.lon) for n in o.nodes]
            except osmium.InvalidLocationError:
                continue
            if not any(inside(la, lo) for _, la, lo in pts):
                continue
            ways.append({"id": o.id, "nodes": [p[0] for p in pts], "coords": [[la, lo] for _, la, lo in pts],
                         "tags": {k: t[k] for k in ("highway", "name", "ref", "surface", "maxspeed", "bridge", "tunnel", "covered",
                                                   "oneway", "bicycle") if k in t}})
        elif o.is_relation() and o.tags.get("route") == "bicycle":
            bike_ways.update(m.ref for m in o.members if m.type == "w")
    for w in ways:
        w["bike_route"] = w["id"] in bike_ways
    return {"ways": ways, "special": special, "passes": passes, "places": places}


# --- elevation (Swisstopo profile service, batched and cached) ------------------------------

def _post(body: bytes) -> list:
    for attempt in range(4):
        try:
            req = urllib.request.Request(PROFILE_URL, data=body,
                                         headers={"User-Agent": "VibeGrind climb builder (hello@vibegrind.app)"})
            return json.loads(urllib.request.urlopen(req, timeout=90).read())
        except Exception:  # noqa: BLE001 - retry with backoff, then give up on this batch
            time.sleep(2 * (attempt + 1))
    return []


def elevation(net: Network, cache_path: str) -> dict:
    """Profiles for every edge. Several roads go into one request, joined by straight hops whose samples
    are ignored. Results are cached per road stretch, so a rebuild only asks about new or changed roads."""
    lock = threading.Lock()
    key = lambda e: hashlib.sha1(json.dumps([[round(x, 1), round(y, 1)] for x, y in e.xy]).encode()).hexdigest()  # noqa: E731
    cache = {}
    if cache_path:
        os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
        if os.path.exists(cache_path):
            for line in open(cache_path):
                k, v = json.loads(line)
                cache[k] = v
    keys = {e.id: key(e) for e in net.edges}
    profiles = {e.id: cache[keys[e.id]] for e in net.edges if keys[e.id] in cache}
    todo = [e for e in net.edges if e.id not in profiles]
    print(f"  elevation: {len(profiles)} road stretches cached, {len(todo)} to fetch", flush=True)

    def cell(e):
        x, y = e.xy[0]
        r = int(y // 2000)
        return r, int(x // 2000) * (1 if r % 2 == 0 else -1)

    batches, cur, total, last = [], [], 0.0, None
    for e in sorted(todo, key=cell):
        hop = math.dist(last, e.xy[0]) if last else 0.0
        if cur and total + hop + e.length > BATCH_M:
            batches.append(cur)
            cur, total, hop = [], 0.0, 0.0
        cur.append(e)
        total += hop + e.length
        last = e.xy[-1]
    if cur:
        batches.append(cur)

    def run(batch):
        line, offsets, d = [], [], 0.0
        for e in batch:
            if line:
                d += math.dist(line[-1], e.xy[0])
            offsets.append(d)
            line += [[round(x, 1), round(y, 1)] for x, y in e.xy]
            d += e.length
        nb = int(min(1000, max(20, math.ceil(d / SPACING_M))))
        body = urllib.parse.urlencode({"geom": json.dumps({"type": "LineString", "coordinates": line}),
                                       "sr": "2056", "nb_points": nb}).encode()
        samples = [[p["dist"], p["alts"].get("DTM2") or p["alts"].get("COMB")] for p in _post(body)
                   if (p["alts"].get("DTM2") or p["alts"].get("COMB")) is not None]
        if not samples:
            return {}
        scale = d / samples[-1][0] if samples[-1][0] else 1.0
        pts = [(x * scale, z) for x, z in samples]
        out = {}
        for e, off in zip(batch, offsets):
            inner = [(round(x - off, 1), round(z, 1)) for x, z in pts if off < x < off + e.length]
            out[e.id] = [(0.0, round(_interp(pts, off), 1))] + inner + [(round(e.length, 1), round(_interp(pts, off + e.length), 1))]
        if cache_path:
            with lock, open(cache_path, "a") as f:
                for eid, prof in out.items():
                    f.write(json.dumps([keys[eid], prof]) + "\n")
        return out

    done = 0
    with ThreadPoolExecutor(4) as pool:
        for part in pool.map(run, batches):
            profiles.update(part)
            done += 1
            if done % 500 == 0 or done == len(batches):
                print(f"  elevation: {done}/{len(batches)} requests", flush=True)
    return profiles


def _interp(pts, x):
    lo, hi = 0, len(pts) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if pts[mid][0] < x:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0:
        return pts[0][1]
    (x0, z0), (x1, z1) = pts[lo - 1], pts[lo]
    return z0 + (z1 - z0) * ((x - x0) / (x1 - x0) if x1 > x0 else 0)


ROUTE_CLASSES = {"primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link",
                 "unclassified", "residential", "cycleway"}


def road_graph(ways: list, climbs: list) -> dict:
    """The bigger roads as a small graph, so the app can work out roughly how far each climb is by road
    (lakes make straight-line distance useless here). Each climb gets its nearest graph node."""
    g = Network([w for w in ways if w["tags"].get("highway") in ROUTE_CLASSES])
    index, nodes = {}, []
    for e in g.edges:
        for n, c in ((e.a, e.coords[0]), (e.b, e.coords[-1])):
            if n not in index:
                index[n] = len(nodes)
                nodes.append([round(c[0], 5), round(c[1], 5)])
    edges = [[index[e.a], index[e.b], round(e.length)] for e in g.edges if e.a != e.b]
    grid = {}
    for i, (la, lo) in enumerate(nodes):
        grid.setdefault((int(la * 100), int(lo * 100)), []).append(i)
    from app.climbdb import haversine
    for c in climbs:
        la, lo = c["foot"]
        best = (1e18, None)
        for r in range(0, 6):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    for i in grid.get((int(la * 100) + dx, int(lo * 100) + dy), ()):
                        d = haversine((la, lo), nodes[i])
                        if d < best[0]:
                            best = (d, i)
            if best[1] is not None:
                break
        c["foot_node"], c["foot_off_m"] = best[1], round(best[0])
    return {"nodes": nodes, "edges": edges}


LANDMARK_KINDS = {"town", "village", "suburb", "hamlet", "neighbourhood", "locality", "pass"}
LANDMARK_NEAR_M = 150


def landmark_index(places: list, passes: list) -> dict:
    """Named places on a ~500 m grid, for finding the ones a climb passes."""
    grid = {}
    for la, lo, name, *kind in places + [p + ["pass"] for p in passes]:
        if (kind[0] if kind else "pass") in LANDMARK_KINDS:
            grid.setdefault((round(la * 200), round(lo * 150)), []).append((la, lo, name))
    return grid


def landmarks(climb: dict, grid: dict) -> list:
    """[metres from the foot, name] for named places the climb passes within LANDMARK_NEAR_M, in order,
    so the app can say "turn just past Alosen" instead of "at km 2.1"."""
    from app.climbdb import haversine, path_points
    seen, out = set(), []
    for la, lo, _, at in path_points(climb):
        k = (round(la * 200), round(lo * 150))
        for dk in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)):
            for pla, plo, name in grid.get((k[0] + dk[0], k[1] + dk[1]), ()):
                if name not in seen and haversine((la, lo), (pla, plo)) <= LANDMARK_NEAR_M:
                    seen.add(name)
                    out.append([round(at), name])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pbf")
    ap.add_argument("out")
    ap.add_argument("--center", nargs=2, type=float, default=[47.0504, 8.5466], metavar=("LAT", "LON"))
    ap.add_argument("--radius-km", type=float, default=50)
    ap.add_argument("--region", default="Central Switzerland")
    ap.add_argument("--cache", default="", help="elevation cache (JSON lines), reused between builds")
    ap.add_argument("--osm-cache", default="", help="save/reuse the roads read from the extract (faster reruns)")
    a = ap.parse_args()

    t0 = time.time()
    print(f"Reading {a.pbf} around {a.center} ({a.radius_km} km)…", flush=True)
    if a.osm_cache and os.path.exists(a.osm_cache):
        osm = json.load(open(a.osm_cache))
        osm["special"] = {int(k): v for k, v in osm["special"].items()}
    else:
        osm = read_osm(a.pbf, a.center[0], a.center[1], a.radius_km)
        if a.osm_cache:
            json.dump(osm, open(a.osm_cache, "w"))
    print(f"  {len(osm['ways'])} rideable ways, {len(osm['special'])} interruption points, "
          f"{len(osm['places'])} places ({time.time() - t0:.0f} s)", flush=True)
    net = Network(osm["ways"], osm["special"])
    km = sum(e.length for e in net.edges) / 1000
    print(f"  network: {len(net.edges)} road stretches, {km:.0f} km", flush=True)
    net.set_profiles(elevation(net, a.cache))
    print(f"  elevation done ({time.time() - t0:.0f} s); finding climbs…", flush=True)
    climbs = net.find_climbs()
    print(f"  {len(climbs)} climbs ({time.time() - t0:.0f} s)", flush=True)

    out = []
    marks = landmark_index(osm["places"], osm["passes"])
    for i, c in enumerate(climbs, 1):
        s = c.stats()
        s["from"] = place_name(s["foot"], osm["places"], osm["passes"], top=False)
        s["to"] = place_name(s["top"], osm["places"], osm["passes"], top=True)
        s["landmarks"] = landmarks(s, marks)
        s["id"] = f"c{i:05d}"
        out.append(s)
    doc = {"version": 1, "built": date.today().isoformat(), "region": a.region,
           "center": a.center, "radius_km": a.radius_km,
           "sources": {"roads": "© OpenStreetMap contributors (ODbL)", "elevation": "© swisstopo (DTM 2 m)"},
           "climbs": out}
    roads = road_graph(osm["ways"], out)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with gzip.open(a.out, "wt", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    roads_out = a.out.replace(".json.gz", "-roads.json.gz")
    with gzip.open(roads_out, "wt", encoding="utf-8") as f:
        json.dump(roads, f, separators=(",", ":"))
    print(f"Road graph: {len(roads['nodes'])} junctions, {len(roads['edges'])} roads -> {roads_out} "
          f"({os.path.getsize(roads_out) / 1e6:.1f} MB)")
    print(f"Wrote {len(out)} climbs to {a.out} ({os.path.getsize(a.out) / 1e6:.1f} MB, {time.time() - t0:.0f} s)")


if __name__ == "__main__":
    main()
