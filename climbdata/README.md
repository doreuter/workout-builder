# Climb database

`switzerland.jsonl.gz` lists the road climbs of Switzerland (11,301 in the 2026-10-30 build), found by
`scripts/build_climbs.py` with the climb finder in `app/climbdb.py`. The app reads it to suggest where
to ride a workout's intervals; it's built offline, not on the server.

## Sources and licences

- **Roads, traffic signals, stop signs, rail crossings, bike routes, place names:** © OpenStreetMap
  contributors, under the [Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/).
  This file is a derived database, so it's available under the ODbL too.
- **Elevation:** © swisstopo, swissALTI3D / DTM 2 m, via the free profile service
  (`api3.geo.admin.ch`). Swisstopo geodata is free to use with attribution.

Anywhere a climb is shown, credit "© OpenStreetMap contributors, © swisstopo".

## What's in it

JSON lines, gzip'd: the first line describes the database, then one climb per line
(`app.climbdb.read_climbs` reads it line by line, so the whole country loads in little memory).

```json
{"version": 1, "built": "2026-10-30", "region": "Switzerland", "center": [46.8, 8.23], "radius_km": 175, "sources": {...}}
{
   "id": "c00001", "from": "Schwyz", "to": "Ibergeregg",
   "length_m": 10420, "gain_m": 878, "foot_ele": 520, "top_ele": 1398,
   "avg": 8.4, "max200": 11.6, "steadiness": 1.9,
   "interruptions": [{"kind": "signal", "at_m": 120}],   // signal, stop, give_way, rail, roundabout
   "junctions_at_m": [480],                              // bigger roads joining
   "streets": ["Ibergereggstrasse", "..."],
   "classes": {"secondary": 0.8, "residential": 0.2},    // share of the length
   "bike_route": 0.9, "max_speed": 80,
   "foot": [47.0186, 8.6531], "top": [47.0174, 8.7332],
   "path": {"step_m": 20, "polyline": "…", "ele": [520.4, 522.1, ...]}
}
```

`path` holds a point every 20 m from the foot (the last one is the top): positions as a Google encoded
polyline, elevations in `ele`. `app.climbdb.path_points(climb)` turns it into `(lat, lon, elevation,
metres from the foot)`.

Climbs are found across the road network whatever the streets are called: a climb is the way a rider
naturally keeps going uphill (see the module docstring in `app/climbdb.py`). Only paved roads open to
bikes count: no motorways or trunk roads, private or gravel roads. Bridges and road tunnels are ridden
straight through (the terrain model has the valley floor or the mountain there). A climb is at least
1 km with 50 m of gain and 3 % average. A climb that starts on a bigger one and mostly follows it is
left out as a piece of it; a different approach that joins higher up is kept as its own climb.

`tests/test_climb_data.py` checks climbs we know are in it, whole and with sane numbers (Steinen →
Ecce Homo, Zug → Zugerberg, Schwyz → Schwyzerhöhe and → Ibergeregg, Sattelegg, Raten, Pragel).

## Rebuilding

```bash
python -m venv .venv-build && .venv-build/bin/pip install -r requirements-build.txt
curl -L -o switzerland.osm.pbf https://download.geofabrik.de/europe/switzerland-latest.osm.pbf
.venv-build/bin/python scripts/build_climbs.py switzerland.osm.pbf climbdata/switzerland.jsonl.gz \
    --cache ~/.cache/vibegrind/elevation.jsonl
```

The extract is about 550 MB; the whole build takes about 25 minutes (10 of them asking Swisstopo for
elevation). Elevation answers are cached, so a
rebuild only asks Swisstopo about roads it hasn't seen.

Next to it: `switzerland-roads.bin.gz`, the bigger roads as a packed graph for distances by road
(`app.climbdb.pack_road_graph`), and `switzerland-places.json.gz`, the towns, villages and hamlets you can
start from.
