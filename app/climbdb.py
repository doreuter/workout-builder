"""Find road climbs across a road network, whatever the streets are called.

Used offline by scripts/build_climbs.py to build the climb database (climbdata/*.json.gz) from an
OpenStreetMap extract and an elevation source. Pure code, no file or network access, so it's tested
with small made-up networks.

The idea: roads form a graph. From every junction, dead end and low point we ride uphill, at each
junction taking the branch a rider would naturally follow (the one that keeps climbing, with a small
penalty for sharp turns and for dropping onto a smaller road) until the road stops climbing. Climbs
that mostly overlap a bigger one are dropped. Street names play no part, so a climb like Steinen ->
Ecce Homo that runs over five streets comes out whole.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

RANK = {"primary": 5, "primary_link": 5, "secondary": 4, "secondary_link": 4, "tertiary": 3, "tertiary_link": 3,
        "unclassified": 2, "residential": 2, "service": 1, "track": 1, "cycleway": 1}
MIN_LENGTH_M = 1000
MIN_GAIN_M = 50
MIN_AVG = 0.03
FLAT_STOP_M = 800      # stop once the road hasn't gained height for this long
MAX_CLIMB_M = 40000
LOOKAHEAD_M = 300      # how far down each branch we look when choosing at a junction
SAMPLE_M = 20          # spacing of the stored profile
OVERLAP_DROP = 0.6     # a climb that starts on a bigger one and shares this much of it is just a piece of it
OWN_ROAD_M = 1000      # ...and a different approach needs this much road of its own to count separately


def lv95(lat: float, lon: float) -> tuple[float, float]:
    """WGS84 -> Swiss LV95 metres (swisstopo's approximate formulas, about 1 m)."""
    p, l = (lat * 3600 - 169028.66) / 10000, (lon * 3600 - 26782.5) / 10000
    e = 2600072.37 + 211455.93 * l - 10938.51 * l * p - 0.36 * l * p * p - 44.54 * l ** 3
    n = 1200147.07 + 308807.95 * p + 3745.25 * l * l + 76.63 * p * p - 194.56 * l * l * p + 119.79 * p ** 3
    return e, n


def haversine(a, b) -> float:
    r = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


@dataclass
class Edge:
    """A stretch of road between two junctions (or dead ends, or low points)."""
    id: int
    a: int
    b: int
    coords: list          # (lat, lon) per vertex
    xy: list              # LV95 (e, n) per vertex
    vnodes: list          # OSM node id per vertex (None for points we split at)
    seg_way: list         # index into `ways` for each segment
    vdist: list           # distance from `a` per vertex, metres
    prof: list = field(default_factory=list)  # [(distance from a, elevation)]

    @property
    def length(self) -> float:
        return self.vdist[-1]


class Network:
    def __init__(self, ways: list, special: dict | None = None):
        """`ways`: [{"nodes": [...], "coords": [[lat, lon], ...], "tags": {...}, "bike_route": bool}]
        `special`: OSM node id -> interruption kind ("signal", "stop", "give_way", "rail", "roundabout")."""
        self.ways = ways
        self.special = special or {}
        self.edges: list[Edge] = []
        self.adj: dict = defaultdict(list)  # node -> [(edge id, +1 if leaving from a else -1)]
        self.node_ways: dict = defaultdict(set)  # OSM node -> ways touching it
        self._next_virtual = -1
        self._oriented: dict = {}
        self._build()

    # --- graph ---------------------------------------------------------------------------
    def _build(self):
        nbrs, pos, seg_way = defaultdict(set), {}, {}
        for wi, w in enumerate(self.ways):
            for n in w["nodes"]:
                self.node_ways[n].add(wi)
            for (n1, c1), (n2, c2) in zip(zip(w["nodes"], w["coords"]), zip(w["nodes"][1:], w["coords"][1:])):
                if n1 == n2:
                    continue
                pos[n1], pos[n2] = tuple(c1), tuple(c2)
                nbrs[n1].add(n2)
                nbrs[n2].add(n1)
                seg_way.setdefault((n1, n2), wi)
                seg_way.setdefault((n2, n1), wi)
        stops = {n for n, s in nbrs.items() if len(s) != 2}
        seen = set()

        def walk(start, nxt):
            nodes = [start, nxt]
            while nodes[-1] not in stops and nodes[-1] != start:
                p, q = tuple(nbrs[nodes[-1]])
                nodes.append(p if q == nodes[-2] else q)
            return nodes

        def add(nodes):
            for p, q in zip(nodes, nodes[1:]):
                seen.add((p, q))
                seen.add((q, p))
            coords = [pos[n] for n in nodes]
            xy = [lv95(*c) for c in coords]
            vdist = [0.0]
            for u, v in zip(xy, xy[1:]):
                vdist.append(vdist[-1] + math.dist(u, v))
            self._add_edge(nodes[0], nodes[-1], coords, xy, list(nodes),
                           [seg_way[(p, q)] for p, q in zip(nodes, nodes[1:])], vdist)

        for j in list(stops):
            for n in nbrs[j]:
                if (j, n) not in seen:
                    add(walk(j, n))
        for n in list(nbrs):  # closed loops without any junction
            for m in nbrs[n]:
                if (n, m) not in seen:
                    stops.add(n)
                    add(walk(n, m))

    def _add_edge(self, a, b, coords, xy, vnodes, seg_way, vdist, prof=None) -> Edge:
        e = Edge(len(self.edges), a, b, coords, xy, vnodes, seg_way, vdist, prof or [])
        self.edges.append(e)
        self.adj[a].append((e.id, 1))
        self.adj[b].append((e.id, -1))
        return e

    # --- elevation -----------------------------------------------------------------------
    def set_profiles(self, profiles: dict):
        """edge id -> [(distance from a, elevation)], then flatten bridges and split at low points."""
        for e in self.edges:
            e.prof = sorted(tuple(p) for p in profiles.get(e.id) or [])
        for e in self.edges:
            self._flatten_bridges(e)
        for e in list(self.edges):
            self._split_at_lows(e)
        self._oriented = {}

    def _flatten_bridges(self, e: Edge):
        """The terrain model has the valley floor under a bridge and the mountain above a tunnel or
        gallery; ride straight through instead."""
        if len(e.prof) < 2:
            return
        spans, start = [], None
        for i, wi in enumerate(e.seg_way + [None]):
            t = self.ways[wi]["tags"] if wi is not None else {}
            on = t.get("bridge") in ("yes", "viaduct") or t.get("tunnel") in ("yes", "building_passage") \
                or t.get("covered") == "yes"
            if on and start is None:
                start = e.vdist[i]
            elif not on and start is not None:
                spans.append((start, e.vdist[i]))
                start = None
        for lo, hi in spans:
            zl, zh = z_at(e.prof, lo), z_at(e.prof, hi)
            e.prof = [(d, zl + (zh - zl) * (d - lo) / (hi - lo) if lo < d < hi and hi > lo else z) for d, z in e.prof]

    def _split_at_lows(self, e: Edge, prominence: float = 6.0):
        """Split an edge at a dip in the middle, so a climb can start right at the bottom of it."""
        if len(e.prof) < 5 or e.length < 300:
            return
        cut = None
        for i in range(1, len(e.prof) - 1):
            d, z = e.prof[i]
            if d < 80 or d > e.length - 80:
                continue
            window = [zz for dd, zz in e.prof if abs(dd - d) <= 150]
            if z > min(window):
                continue
            ahead = max((zz for dd, zz in e.prof if d < dd <= d + 500), default=z)
            behind = max((zz for dd, zz in e.prof if d - 500 <= dd < d), default=z)
            if ahead - z >= prominence and behind - z >= prominence:
                cut = d
                break
        if cut is None:
            return
        # Detach e from its far end, then add the second half as a new edge.
        k = bisect.bisect_left(e.vdist, cut)  # vertex index just past the cut
        f = (cut - e.vdist[k - 1]) / (e.vdist[k] - e.vdist[k - 1]) if e.vdist[k] > e.vdist[k - 1] else 0
        lerp = lambda p, q: (p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f)  # noqa: E731
        mid_c, mid_xy, way = lerp(e.coords[k - 1], e.coords[k]), lerp(e.xy[k - 1], e.xy[k]), e.seg_way[k - 1]
        v = self._next_virtual
        self._next_virtual -= 1
        old_b = e.b
        self.adj[old_b].remove((e.id, -1))
        second = self._add_edge(
            v, old_b, [mid_c] + e.coords[k:], [mid_xy] + e.xy[k:], [None] + e.vnodes[k:],
            [way] + e.seg_way[k:], [0.0] + [x - cut for x in e.vdist[k:]],
            [(0.0, z_at(e.prof, cut))] + [(d - cut, z) for d, z in e.prof if d > cut])
        e.coords, e.xy, e.vnodes = e.coords[:k] + [mid_c], e.xy[:k] + [mid_xy], e.vnodes[:k] + [None]
        e.seg_way, e.vdist = e.seg_way[:k - 1] + [way], e.vdist[:k] + [cut]
        e.prof = [(d, z) for d, z in e.prof if d < cut] + [(cut, second.prof[0][1])]
        e.b = v
        self.adj[v].append((e.id, -1))
        self._split_at_lows(second, prominence)

    # --- riding uphill -------------------------------------------------------------------
    def oriented(self, eid: int, d: int) -> dict:
        """An edge as seen when riding it from a (d=+1) or from b (d=-1)."""
        hit = self._oriented.get((eid, d))
        if hit is not None:
            return hit
        e = self.edges[eid]
        if d == 1:
            o = {"prof": e.prof, "xy": e.xy, "coords": e.coords, "vnodes": e.vnodes, "vdist": e.vdist,
                 "seg_way": e.seg_way, "end": e.b, "length": e.length}
        else:
            L = e.length
            o = {"prof": [(L - x, z) for x, z in reversed(e.prof)], "xy": e.xy[::-1], "coords": e.coords[::-1],
                 "vnodes": e.vnodes[::-1], "vdist": [L - x for x in reversed(e.vdist)], "seg_way": e.seg_way[::-1],
                 "end": e.a, "length": L}
        self._oriented[(eid, d)] = o
        return o

    def _branch_score(self, came: dict, eid: int, d: int) -> float:
        o = self.oriented(eid, d)
        if len(o["prof"]) < 2 or o["length"] < 1:
            return -1e9
        span = min(o["length"], LOOKAHEAD_M)
        grade = (z_at(o["prof"], span) - z_at(o["prof"], 0)) / span
        turn = turn_angle(came["xy"], came["vdist"], o["xy"], o["vdist"])
        here, there = self.ways[came["seg_way"][-1]]["tags"], self.ways[o["seg_way"][0]]["tags"]
        drop = max(0, RANK.get(here.get("highway"), 2) - RANK.get(there.get("highway"), 2))
        name = here.get("name") or here.get("ref")
        same = bool(name) and name == (there.get("name") or there.get("ref"))
        dead_end = len(self.adj[o["end"]]) == 1 and o["length"] < 400
        # Grade in %, minus 0.04 per degree of turn and 3 per step down in road class (a pass road
        # beats a steeper farm road), plus 1.5 for carrying on along the same street.
        return grade * 100 - 0.04 * turn - 3 * drop + (1.5 if same else 0) - (3 if dead_end else 0)

    def _hw(self, wi: int) -> str:
        return self.ways[wi]["tags"].get("highway", "")

    def ride_up(self, node, eid: int, d: int):
        """Ride uphill from `node` starting along edge (eid, d). Returns the climb or None."""
        segs, prof = [], []
        start_z = None
        top_d = top_z = None
        visited = {node}
        cur = (eid, d)
        while True:
            o = self.oriented(*cur)
            if len(o["prof"]) < 2:
                break
            base = prof[-1][0] if prof else 0.0
            stop = False
            for x, z in (o["prof"] if not prof else o["prof"][1:]):
                prof.append((base + x, z))
                if start_z is None:
                    start_z, top_d, top_z = z, base + x, z
                if z > top_z:
                    top_d, top_z = base + x, z
                elif top_z - z > min(25.0, max(8.0, 0.1 * (top_z - start_z))):
                    stop = True
                    break
            segs.append((cur[0], cur[1], base))
            if stop or prof[-1][0] - top_d > FLAT_STOP_M or prof[-1][0] > MAX_CLIMB_M:
                break
            node = o["end"]
            if node in visited:
                break
            visited.add(node)
            options = [(self._branch_score(o, e2, d2), e2, d2) for e2, d2 in self.adj[node] if e2 != cur[0]]
            if not options:
                break
            score, e2, d2 = max(options)
            if score < -2:  # every way on goes down: the top
                break
            cur = (e2, d2)
        if not prof or top_d is None:
            return None
        # Trim the flat run-in (the first 100 m climbing under 2 %).
        start_d = 0.0
        while start_d + 100 < top_d and z_at(prof, start_d + 100) - z_at(prof, start_d) < 2:
            start_d += SAMPLE_M
        length, gain = top_d - start_d, top_z - z_at(prof, start_d)
        if length < MIN_LENGTH_M or gain < MIN_GAIN_M or gain / length < MIN_AVG:
            return None
        return Climb(self, segs, prof, start_d, top_d)

    def find_climbs(self) -> list:
        """Every distinct climb in the network, biggest first."""
        found = []
        for node, inc in list(self.adj.items()):
            for eid, d in inc:
                o = self.oriented(eid, d)
                if len(o["prof"]) < 2:
                    continue
                span = min(o["length"], 150)
                if span < 20 or (z_at(o["prof"], span) - z_at(o["prof"], 0)) / span < 0.02:
                    continue  # not heading uphill from here
                c = self.ride_up(node, eid, d)
                if c:
                    found.append(c)
        found.sort(key=lambda c: (-c.gain, -c.length))
        kept, covered = [], set()
        for c in found:
            keys, start = c.keys(), c.keys(upto=300)
            shared = len(keys & covered)
            # A piece of a bigger climb starts on it. A different approach that joins it higher up is its
            # own climb, but only with at least 1 km of its own road (not just another street in the village).
            if keys and shared / len(keys) >= OVERLAP_DROP and (
                    len(start & covered) >= len(start) / 2 or (len(keys) - shared) * 50 < OWN_ROAD_M):
                continue
            kept.append(c)
            covered |= keys
        return kept


class Climb:
    """One climb: the ridden edges plus the elevation profile from foot (start_d) to top (top_d)."""

    def __init__(self, net: Network, segs: list, prof: list, start_d: float, top_d: float):
        self.net, self.segs, self.prof = net, segs, prof
        self.start_d, self.top_d = start_d, top_d
        self.length = top_d - start_d
        self.gain = z_at(prof, top_d) - z_at(prof, start_d)

    def _pieces(self):
        """(edge id, direction, distance where it starts on the ride, oriented edge) for each ridden edge."""
        for eid, d, base in self.segs:
            yield eid, d, base, self.net.oriented(eid, d)

    def keys(self, upto: float | None = None) -> set:
        """50 m buckets of road in riding direction (the first `upto` m only, if given): what climbs share."""
        out = set()
        end = self.top_d if upto is None else min(self.top_d, self.start_d + upto)
        for eid, d, base, o in self._pieces():
            lo, hi = max(self.start_d, base), min(end, base + o["length"])
            x = lo
            while x < hi:
                out.add((eid, d, int((x - base) // 50)))
                x += 50
        return out

    def vertices(self):
        """Every road vertex on the climb with its distance from the foot: (dist, lat, lon, node, way)."""
        last = None
        for eid, d, base, o in self._pieces():
            for i, (x, c) in enumerate(zip(o["vdist"], o["coords"])):
                at = base + x
                if at < self.start_d - 1 or at > self.top_d + 1 or (last is not None and at <= last):
                    continue
                last = at
                way = o["seg_way"][min(i, len(o["seg_way"]) - 1)]
                yield at - self.start_d, c[0], c[1], o["vnodes"][i], way

    def point_at(self, dist: float):
        """(lat, lon) at `dist` metres past the foot."""
        target = self.start_d + dist
        for eid, d, base, o in self._pieces():
            if base <= target <= base + o["length"] + 1e-6:
                x = target - base
                k = max(1, min(bisect.bisect_left(o["vdist"], x), len(o["vdist"]) - 1))
                x0, x1 = o["vdist"][k - 1], o["vdist"][k]
                f = (x - x0) / (x1 - x0) if x1 > x0 else 0
                (la0, lo0), (la1, lo1) = o["coords"][k - 1], o["coords"][k]
                return la0 + (la1 - la0) * f, lo0 + (lo1 - lo0) * f
        o = self.net.oriented(*self.segs[-1][:2])
        return o["coords"][-1]

    def stats(self) -> dict:
        net = self.net
        z0 = z_at(self.prof, self.start_d)
        n100 = int(self.length // 100)
        g100 = [(z_at(self.prof, self.start_d + 100 * (i + 1)) - z_at(self.prof, self.start_d + 100 * i)) / 100
                for i in range(n100)]
        g200 = [(z_at(self.prof, x + 200) - z_at(self.prof, x)) / 200
                for x in frange(self.start_d, self.top_d - 200, SAMPLE_M)] or [self.gain / self.length]
        xs = frange(0, self.length, SAMPLE_M) + [self.length]
        stops, junctions, by_class, streets = [], [], defaultdict(float), []
        bike, speeds, prev = 0.0, [], None
        ridden = {o_way for *_, o_way in self.vertices()}
        for at, lat, lon, node, way in self.vertices():
            seg = (at - prev[0]) if prev else 0.0
            if prev:
                w = net.ways[prev[1]]
                by_class[w["tags"].get("highway", "")] += seg
                bike += seg if w.get("bike_route") else 0
                sp = str(w["tags"].get("maxspeed", ""))
                if sp.isdigit():
                    speeds.append(int(sp))
                name = w["tags"].get("name") or w["tags"].get("ref")
                if name and (not streets or streets[-1] != name):
                    streets.append(name)
            prev = (at, way)
            if node is None or at <= 1 or at >= self.length - 1:
                continue
            kind = net.special.get(node)
            if kind:
                stops.append({"kind": kind, "at_m": round(at)})
            here = RANK.get(net.ways[way]["tags"].get("highway", ""), 2)
            others = [wi for wi in net.node_ways.get(node, ()) if wi not in ridden]
            if any(RANK.get(net.ways[wi]["tags"].get("highway", ""), 2) >= max(3, here) for wi in others):
                junctions.append(round(at))
        return {
            "length_m": round(self.length), "gain_m": round(self.gain),
            "foot_ele": round(z0), "top_ele": round(z0 + self.gain),
            "avg": round(self.gain / self.length * 100, 1),
            "max200": round(max(g200) * 100, 1),
            "steadiness": round(statistics.pstdev(g100) * 100, 1) if len(g100) > 1 else 0.0,
            "interruptions": stops, "junctions_at_m": junctions,
            "streets": streets,
            "classes": {k: round(v / self.length, 2) for k, v in sorted(by_class.items(), key=lambda kv: -kv[1]) if v},
            "bike_route": round(bike / self.length, 2) if self.length else 0.0,
            "max_speed": max(speeds) if speeds else None,
            "foot": [round(v, 5) for v in self.point_at(0)], "top": [round(v, 5) for v in self.point_at(self.length)],
            # Every SAMPLE_M metres from the foot (the last point is the top): positions as an encoded
            # polyline, elevations alongside. Compact, and the app decodes only the climbs it uses.
            "path": {"step_m": SAMPLE_M,
                     "polyline": encode_polyline([self.point_at(x) for x in xs]),
                     "ele": [round(z_at(self.prof, self.start_d + x), 1) for x in xs]},
        }


# --- small helpers ------------------------------------------------------------------------

def z_at(prof: list, x: float) -> float:
    """Elevation at distance x along a profile [(distance, elevation)], interpolated."""
    if not prof:
        return 0.0
    if x <= prof[0][0]:
        return prof[0][1]
    if x >= prof[-1][0]:
        return prof[-1][1]
    k = bisect.bisect_left(prof, (x, -1e18))
    (x0, z0), (x1, z1) = prof[k - 1], prof[k]
    return z0 + (z1 - z0) * ((x - x0) / (x1 - x0) if x1 > x0 else 0)


def encode_polyline(points: list, precision: int = 5) -> str:
    """Google's encoded polyline format for [(lat, lon), ...]."""
    out, prev = [], (0, 0)
    f = 10 ** precision
    for lat, lon in points:
        cur = (round(lat * f), round(lon * f))
        for delta in (cur[0] - prev[0], cur[1] - prev[1]):
            v = ~(delta << 1) if delta < 0 else delta << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev = cur
    return "".join(out)


def decode_polyline(s: str, precision: int = 5) -> list:
    pts, i, lat, lon = [], 0, 0, 0
    f = 10 ** precision
    while i < len(s):
        vals = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(result >> 1) if result & 1 else result >> 1)
        lat, lon = lat + vals[0], lon + vals[1]
        pts.append((lat / f, lon / f))
    return pts


def path_points(climb: dict) -> list:
    """A stored climb's route as [(lat, lon, elevation, metres from the foot)]."""
    p = climb["path"]
    pts = decode_polyline(p["polyline"])
    return [(la, lo, z, min(i * p["step_m"], climb["length_m"])) for i, ((la, lo), z) in enumerate(zip(pts, p["ele"]))]


def frange(a: float, b: float, step: float) -> list:
    out, x = [], a
    while x < b:
        out.append(x)
        x += step
    return out


def _bearing(xy, vdist, from_end: bool, span: float = 30.0) -> float:
    if len(xy) < 2:
        return 0.0
    if from_end:
        i = len(xy) - 2
        while i > 0 and vdist[-1] - vdist[i] < span:
            i -= 1
        (x0, y0), (x1, y1) = xy[i], xy[-1]
    else:
        i = 1
        while i < len(xy) - 1 and vdist[i] < span:
            i += 1
        (x0, y0), (x1, y1) = xy[0], xy[i]
    return math.degrees(math.atan2(x1 - x0, y1 - y0))


def turn_angle(xy_in, vdist_in, xy_out, vdist_out) -> float:
    """Degrees between arriving along one road and leaving along the next (0 = straight on)."""
    a = _bearing(xy_in, vdist_in, True)
    b = _bearing(xy_out, vdist_out, False)
    return abs((b - a + 180) % 360 - 180)


def place_name(pt, places: list, passes: list, top: bool) -> str:
    """A name people would use: the village at the foot; the pass or nearest named spot at the top."""
    def nearest(kinds, within):
        best = min(((haversine(pt, p[:2]), p[2]) for p in places if p[3] in kinds), default=(1e9, None))
        return best[1] if best[0] <= within else None
    settle, small = {"city", "town", "village", "suburb"}, {"hamlet", "neighbourhood", "locality"}
    if top:
        p = min(((haversine(pt, q[:2]), q[2]) for q in passes if q[2]), default=(1e9, None))
        if p[0] <= 1500:  # roads often top out a little before or after the pass marker
            return p[1]
        return nearest({"peak"} | small, 900) or nearest(settle, 2500) or nearest(small, 3000) or ""
    return nearest(settle, 2500) or nearest(small, 1500) or nearest(settle, 6000) or nearest(small, 4000) or ""
