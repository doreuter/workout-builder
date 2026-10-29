"""The climb finder (app/climbdb.py) on small made-up road networks."""

import math

from app.climbdb import Network, decode_polyline, encode_polyline, path_points, place_name

M = 111_320  # metres per degree of latitude
LAT0, LON0 = 47.05, 8.55


def north(lat):
    return (lat - LAT0) * M


def east(lat, lon):
    return (lon - LON0) * M * math.cos(math.radians(LAT0))


def line(first_id, start, metres, heading="n", step=50):
    """Node ids and coordinates every `step` m from `start` going north or east."""
    n = int(metres // step)
    k = M * math.cos(math.radians(LAT0))
    coords = [[start[0] + i * step / M, start[1]] if heading == "n" else [start[0], start[1] + i * step / k]
              for i in range(n + 1)]
    return list(range(first_id, first_id + n + 1)), coords


def way(nodes, coords, bike=False, **tags):
    tags.setdefault("highway", "residential")
    return {"nodes": nodes, "coords": coords, "tags": tags, "bike_route": bike}


def climbs_in(ways, elev, special=None):
    net = Network(ways, special)
    net.set_profiles({e.id: [(e.vdist[i], elev(*c)) for i, c in enumerate(e.coords)] for e in net.edges})
    return [c.stats() for c in net.find_climbs()]


def at(metres_north, metres_east=0.0):
    return (LAT0 + metres_north / M, LON0 + metres_east / (M * math.cos(math.radians(LAT0))))


def test_one_climb_across_differently_named_streets():
    a_ids, a = line(1, at(0), 1000)
    b_ids, b = line(a_ids[-1], tuple(a[-1]), 1000)
    found = climbs_in([way(a_ids, a, name="Unten"), way(b_ids, b, name="Oben")],
                      lambda lat, lon: 400 + 0.07 * north(lat))
    assert len(found) == 1
    c = found[0]
    assert abs(c["length_m"] - 2000) < 30 and abs(c["gain_m"] - 140) <= 3 and c["avg"] == 7.0
    assert c["streets"] == ["Unten", "Oben"]


def test_follows_the_climbing_branch_not_a_flat_side_road():
    main_ids, main = line(1, at(0), 2000)
    side_ids, side = line(100, tuple(main[20]), 1000, heading="e")
    side_ids[0] = main_ids[20]  # the side road leaves from the junction at 1 km
    found = climbs_in([way(main_ids, main, name="Main"), way(side_ids, side, name="Side")],
                      lambda lat, lon: 400 + 0.07 * north(lat))
    assert len(found) == 1 and abs(found[0]["length_m"] - 2000) < 30


def test_prefers_straight_on_over_a_sharp_turn_of_similar_grade():
    main_ids, main = line(1, at(0), 2000)
    side_ids, side = line(100, tuple(main[20]), 1000, heading="e")
    side_ids[0] = main_ids[20]
    found = climbs_in([way(main_ids, main), way(side_ids, side)],
                      lambda lat, lon: 400 + 0.06 * north(lat) + 0.07 * east(lat, lon))
    top = found[0]["top"]
    assert abs(north(top[0]) - 2000) < 30 and abs(east(top[0], top[1])) < 30


def test_a_small_dip_doesnt_end_the_climb_but_a_descent_does():
    ids, pts = line(1, at(0), 2100)

    def small_dip(lat, lon):
        d = north(lat)
        return 400 + 0.07 * min(d, 1000) - (5 if 1000 < d <= 1100 else 0) + 0.07 * max(0, d - 1100)
    found = climbs_in([way(ids, pts)], small_dip)
    assert len(found) == 1 and found[0]["length_m"] >= 2000

    ids, pts = line(1, at(0), 2800)

    def descent(lat, lon):
        d = north(lat)
        if d <= 1200:
            return 400 + 0.07 * d
        if d <= 1600:
            return 484 - 0.1 * (d - 1200)
        return 444 + 0.07 * (d - 1600)
    found = climbs_in([way(ids, pts)], descent)
    assert len(found) == 2 and all(1100 <= c["length_m"] <= 1250 for c in found)


def test_a_climb_can_start_partway_along_a_road():
    ids, pts = line(1, at(0), 2000)
    found = climbs_in([way(ids, pts)], lambda lat, lon: 400 - 0.06 * min(north(lat), 500) + 0.07 * max(0, north(lat) - 500))
    assert len(found) == 1
    assert abs(north(found[0]["foot"][0]) - 500) < 40 and abs(found[0]["length_m"] - 1500) < 40


def test_bridges_are_ridden_straight_across():
    a_ids, a = line(1, at(0), 900)
    b_ids, b = line(a_ids[-1], tuple(a[-1]), 200)
    c_ids, c = line(b_ids[-1], tuple(b[-1]), 900)
    valley = lambda lat, lon: 400 + 0.06 * north(lat) - (40 if 925 < north(lat) < 1075 else 0)  # noqa: E731  (under the bridge only)
    found = climbs_in([way(a_ids, a), way(b_ids, b, bridge="yes"), way(c_ids, c)], valley)
    assert len(found) == 1 and found[0]["length_m"] >= 1950


def test_interruptions_junctions_and_bike_routes_along_the_climb():
    ids, pts = line(1, at(0), 2000)
    x_ids, x = line(500, tuple(pts[30]), 100, heading="e")
    x_ids[0] = ids[30]  # a bigger road crosses at 1.5 km
    found = climbs_in([way(ids, pts, bike=True, name="Bergstrasse"), way(x_ids, x, highway="secondary")],
                      lambda lat, lon: 400 + 0.07 * north(lat), special={ids[20]: "signal", ids[0]: "give_way"})
    c = found[0]
    assert [(s["kind"], round(s["at_m"], -2)) for s in c["interruptions"]] == [("signal", 1000)]
    assert [round(j, -2) for j in c["junctions_at_m"]] == [1500]
    assert c["bike_route"] == 1.0 and c["classes"] == {"residential": 1.0}


def test_place_names_people_would_use():
    places = [[*at(-100, 50), "Steinen", "village"], [*at(2500, 0), "Ecce Homo", "hamlet"], [*at(3000, 900), "Hof", "hamlet"]]
    passes = [[*at(2600, 20), "Kleiner Pass"]]
    assert place_name(at(0), places, passes, top=False) == "Steinen"
    assert place_name(at(2500), places, passes, top=True) == "Kleiner Pass"   # a pass wins at the top
    assert place_name(at(2500), places, [], top=True) == "Ecce Homo"


def test_stored_path_round_trips():
    pts = [(47.05, 8.55), (47.05123, 8.55001), (46.9, 8.7)]
    assert decode_polyline(encode_polyline(pts)) == pts
    ids, pts = line(1, at(0), 1500)
    c = climbs_in([way(ids, pts)], lambda lat, lon: 400 + 0.07 * north(lat))[0]
    path = path_points(c)
    assert path[0][3] == 0 and path[-1][3] == c["length_m"] and len(path) == len(c["path"]["ele"])
    assert abs(path[-1][2] - path[0][2] - c["gain_m"]) < 1
