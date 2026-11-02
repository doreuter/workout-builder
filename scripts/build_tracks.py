"""Build the running-track database (oval athletics tracks) from an OpenStreetMap extract.

Run offline, like scripts/build_climbs.py (needs osmium, see requirements-build.txt):

    python scripts/build_tracks.py switzerland.osm.pbf climbdata/switzerland-tracks.json.gz

Tracks are OSM `leisure=track` for athletics or running, mapped as a line round lane 1, as a filled
area, or as a ring (outer and inner edge). Only oval ones go in: straight sprint lanes and woodchip
trails don't suit interval runs. Each gets its lap length, a name from the sports ground or school it's
in, and the village it's in (from climbdata/switzerland-places.json.gz, built with the climbs).
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sys
from datetime import date

import osmium

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.climbdb import haversine  # noqa: E402

SYNTHETIC = {"tartan", "rubber", "synthetic", "acrylic", "plastic", "artificial_turf"}
SKIP_SURFACE = {"woodchips", "grass", "sand", "wood", "dirt", "ground", "mud"}
GROUNDS = {("leisure", "stadium"), ("leisure", "sports_centre"), ("amenity", "school"), ("amenity", "college"), ("amenity", "university"), ("leisure", "recreation_ground")}
NAME_NEAR_M = 250


def _xy(ring):
    """Ring of (lat, lon) as metres on a local plane."""
    la0 = ring[0][0]
    k = 111320 * math.cos(math.radians(la0))
    return [((lo - ring[0][1]) * k, (la - la0) * 110540) for la, lo in ring]


def perimeter(ring) -> float:
    return sum(haversine(ring[i], ring[i + 1]) for i in range(len(ring) - 1))


def area_m2(ring) -> float:
    p = _xy(ring)
    return abs(sum(p[i][0] * p[i + 1][1] - p[i + 1][0] * p[i][1] for i in range(len(p) - 1))) / 2


def is_oval(ring) -> bool:
    """Shaped like an athletics oval (4πA/P² about 0.8): not a thin sprint straight (about 0.15), nor a
    woodchip trail winding through a wood (0.3–0.6)."""
    p = perimeter(ring)
    return p > 0 and 4 * math.pi * area_m2(ring) / p ** 2 > 0.65


def lap_m(lane1: float) -> int | None:
    """Lap length from the length round lane 1: standard tracks are 400 m; smaller ones to 10 m."""
    if 370 <= lane1 <= 520:
        return 400
    if 180 <= lane1 < 370:
        return int(round(lane1 / 10) * 10)
    return None


def inside(pt, ring) -> bool:
    """Is (lat, lon) inside the ring (even-odd rule)?"""
    y, x = pt
    hit = False
    for (y1, x1), (y2, x2) in zip(ring, ring[1:]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def centroid(ring):
    return [sum(p[0] for p in ring[:-1]) / (len(ring) - 1), sum(p[1] for p in ring[:-1]) / (len(ring) - 1)]


class Reader(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.tracks, self.grounds = [], []

    def _track(self, tags, ring: bool = False) -> bool:
        """Is this an athletics track? `ring`: mapped with its inner edge, which woodchip trails never are."""
        if tags.get("leisure") != "track" or tags.get("access") in ("no", "private"):
            return False
        sport, surface = tags.get("sport", ""), tags.get("surface", "")
        name = (tags.get("name", "") + " " + tags.get("description", "")).lower()
        if (surface in SKIP_SURFACE or tags.get("indoor") == "yes" or "highway" in tags
                or any(w in name for w in ("finnenbahn", "vita", "finlandaise", "finlandese"))):
            return False
        # In Swiss OSM, sport=running is mostly woodchip trails: those count only when synthetic.
        if "athletics" in sport or not sport:  # untagged ovals are almost all athletics tracks
            return True
        return sport in ("running", "multi") and (ring or surface in SYNTHETIC or tags.get("lanes", "0") > "1")

    def _add(self, tags, outer, lane1):
        lap = lap_m(lane1)
        if not lap or not is_oval(outer):
            return
        self.tracks.append({"tags": dict(tags), "ring": outer, "lap_m": lap, "at": centroid(outer)})

    def way(self, w):
        # Closed lines round lane 1 (area=no); filled areas and rings come through area() below.
        if w.tags.get("area") != "no" or not self._track(w.tags) or w.nodes[0].ref != w.nodes[-1].ref:
            return
        try:
            ring = [(n.lat, n.lon) for n in w.nodes]
        except osmium.InvalidLocationError:
            return
        self._add(w.tags, ring, perimeter(ring))

    def area(self, a):
        tags = a.tags
        try:
            outers = [[(n.lat, n.lon) for n in r] for r in a.outer_rings()]
            if not outers:
                return
            outer = max(outers, key=area_m2)
            inners = [[(n.lat, n.lon) for n in r] for r in a.inner_rings(next(a.outer_rings()))]
        except (osmium.InvalidLocationError, RuntimeError):
            return
        name = tags.get("name")
        if name and any(tags.get(k) == v for k, v in GROUNDS):
            kind = "school" if tags.get("amenity") in ("school", "college", "university") else "ground"
            self.grounds.append({"name": name, "at": centroid(outer), "ring": outer, "size": area_m2(outer),
                                 "kind": kind, "lit": tags.get("lit") == "yes"})
        if self._track(tags, ring=bool(inners)):
            # A ring's inner edge is lane 1; a filled area's outline runs round the outside lane.
            lane1 = perimeter(max(inners, key=area_m2)) if inners else perimeter(outer)
            self._add(tags, outer, lane1)


def simplify(ring, keep=60):
    step = max(1, len(ring) // keep)
    pts = ring[::step]
    return [[round(la, 5), round(lo, 5)] for la, lo in pts + [ring[-1]]]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pbf")
    ap.add_argument("out")
    ap.add_argument("--places", default=os.path.join(os.path.dirname(__file__), "..", "climbdata",
                                                     "switzerland-places.json.gz"))
    a = ap.parse_args()
    r = Reader()
    r.apply_file(a.pbf, locations=True)
    places = json.load(gzip.open(a.places, "rt"))
    towns = [p for p in places if p[3] in ("city", "town", "village")]  # "Zürich", not its quarter

    out = []
    for t in sorted(r.tracks, key=lambda t: (not t["tags"].get("name"), -area_m2(t["ring"]))):
        if any(haversine(t["at"], o["at"]) < 80 for o in out):
            continue  # the same track mapped twice (a line round lane 1 and an area)
        tags = t["tags"]
        # Named after the sports ground (or school) it's in, the smallest one; else one close by.
        near = [g for g in r.grounds if haversine(t["at"], g["at"]) < 3000]
        within = [g for g in near if inside(t["at"], g["ring"])]
        ground = (min(within, key=lambda g: (g["kind"] == "school", g["size"])) if within else
                  min((g for g in near if haversine(t["at"], g["at"]) < NAME_NEAR_M),
                      key=lambda g: (g["kind"] == "school", haversine(t["at"], g["at"])), default=None))
        village = min(towns, key=lambda p: haversine(t["at"], (p[1], p[2])))[0]
        surface = tags.get("surface")
        out.append({
            "id": f"t{len(out) + 1:04d}",
            "name": tags.get("name") or (ground["name"] if ground else None),
            "village": village,
            "school": bool(ground and ground["kind"] == "school"),
            "lap_m": t["lap_m"],
            "surface": "synthetic" if surface in SYNTHETIC else surface,
            "lanes": int(tags["lanes"]) if tags.get("lanes", "").isdigit() else None,
            "lit": tags.get("lit") == "yes" or bool(ground and ground["lit"]),
            "at": [round(x, 5) for x in t["at"]],
            "ring": simplify(t["ring"]),
        })
    with gzip.open(a.out, "wt", encoding="utf-8") as f:
        json.dump({"version": 1, "built": date.today().isoformat(),
                   "sources": {"tracks": "© OpenStreetMap contributors (ODbL)"}, "tracks": out},
                  f, ensure_ascii=False, separators=(",", ":"))
    named = sum(1 for t in out if t["name"])
    print(f"{len(out)} tracks ({named} named, {sum(t['lap_m'] == 400 for t in out)} × 400 m) -> {a.out}")


if __name__ == "__main__":
    main()
