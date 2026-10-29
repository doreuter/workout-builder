"""Climb suggestions (app/climbs.py): reading a workout's efforts, planning them on a climb, and the
recommendations from the real climb database."""

import math

import pytest

from app import climbs
from app.climbdb import encode_polyline, haversine

M = 111_320
THRESHOLD_4x8 = {"name": "4x8", "sport": "cycling", "steps": [
    {"type": "warmup", "duration_s": 900, "intensity": 0.5, "intensity_end": 0.75},
    {"type": "repeat", "count": 4, "steps": [
        {"type": "steady", "duration_s": 480, "intensity": 0.97}, {"type": "rest", "duration_s": 180, "intensity": 0.55}]},
    {"type": "cooldown", "duration_s": 600, "intensity": 0.6, "intensity_end": 0.4}]}


def climb(km, grade, lights_at=(), name=("Unten", "Oben")):
    """A made-up climb in the database's format: straight north at a constant grade."""
    n = int(km * 1000 // 20)
    xs = [i * 20 for i in range(n + 1)]
    pts = [(47.0 + x / M, 8.5) for x in xs]
    ele = [500 + grade * x for x in xs]
    return {"id": "t", "from": name[0], "to": name[1], "length_m": xs[-1], "gain_m": round(ele[-1] - ele[0]),
            "avg": grade * 100, "top_ele": ele[-1], "interruptions": [{"kind": "signal", "at_m": a} for a in lights_at],
            "junctions_at_m": [], "streets": ["Bergstrasse"], "streets_at": [[0, "Bergstrasse"]], "landmarks": [],
            "classes": {"tertiary": 1.0}, "bike_route": 0, "foot": list(pts[0]), "top": list(pts[-1]),
            "path": {"step_m": 20, "polyline": encode_polyline(pts), "ele": ele}}


def test_efforts_are_read_from_the_workout():
    warm, efs = climbs.efforts(THRESHOLD_4x8)
    assert warm == 900 and [e[0] for e in efs] == [480] * 4 and [e[2] for e in efs] == [180, 180, 180, 0]
    over_unders = {"name": "OU", "sport": "cycling", "steps": [
        {"type": "warmup", "duration_s": 600, "intensity": 0.5, "intensity_end": 0.75},
        {"type": "steady", "duration_s": 120, "intensity": 0.95}, {"type": "steady", "duration_s": 60, "intensity": 1.05},
        {"type": "steady", "duration_s": 120, "intensity": 0.95}, {"type": "rest", "duration_s": 300, "intensity": 0.5}]}
    _, efs = climbs.efforts(over_unders)
    assert len(efs) == 1 and efs[0][0] == 300  # one effort, not three


def test_short_climb_means_repeats_and_the_turn_is_where_the_time_runs_out():
    c = climb(4, 0.06)
    p = climbs.plan(c, 900, climbs.efforts(THRESHOLD_4x8)[1], 250, 83)
    assert p["mode"] == "repeats"
    (s, e), = p["spans"]
    ridden = p["profile"].d[e] - p["profile"].d[s]
    assert abs(ridden - climbs.speed(0.06, 0.97 * 250, 83) * 480) < 60  # 8 min at 242 W on 6 %


def test_long_climb_means_efforts_back_to_back_going_up():
    p = climbs.plan(climb(15, 0.07), 900, climbs.efforts(THRESHOLD_4x8)[1], 250, 83)
    assert p["mode"] == "up" and len(p["spans"]) == 4
    assert all(b2 > a2 for (_, a2), (b2, _) in zip(p["spans"], p["spans"][1:]))  # one after the other, uphill


def test_efforts_stay_clear_of_traffic_lights():
    p = climbs.plan(climb(5, 0.06, lights_at=(400,)), 900, climbs.efforts(THRESHOLD_4x8)[1], 250, 83)
    (s, e), = p["spans"]
    assert p["profile"].d[s] >= 400  # the effort starts after the light, not through it


def test_climb_that_is_too_short_for_the_effort_is_skipped():
    assert climbs.plan(climb(1, 0.06), 900, climbs.efforts(THRESHOLD_4x8)[1], 250, 83) is None


needs_data = pytest.mark.skipif(not climbs.available(), reason="climb database isn't built")


@needs_data
def test_recommendations_near_goldau():
    r = climbs.recommend(THRESHOLD_4x8, 47.0504, 8.5466, ftp=250, weight=75)
    assert len(r["climbs"]) == 4 and not r["assumed"]["weight"]
    assert any("uphill" in line for line in r["howto"]) and r["howto"][0].startswith("Warm up 15 min")
    for c in r["climbs"]:
        assert c["mode"] in ("repeats", "up") and c["plan"] and c["summary"] and c["distance_km"] <= climbs.MAX_RIDE_KM
        assert c["path"] and c["sections"] and all(a < b for a, b in c["sections"])
    starts = [c["work_start"] for c in r["climbs"]]
    assert all(haversine(a, b) > 600 for i, a in enumerate(starts) for b in starts[i + 1:])  # different hills


@needs_data
def test_road_distance_goes_round_the_lake():
    """Seelisberg is ~10 km from Goldau as the crow flies but across the lake: much further by road."""
    seelisberg = next(c for c in climbs._load() if c["from"] == "Seelisberg")
    straight = haversine((47.0504, 8.5466), seelisberg["foot"]) / 1000
    by_road = climbs.road_km(47.0504, 8.5466).get(seelisberg["foot_node"])
    assert by_road is None or by_road > 2.5 * straight


@needs_data
def test_a_workout_without_efforts_gets_an_explanation():
    easy = {"name": "Z2", "sport": "cycling", "steps": [{"type": "steady", "duration_s": 3600, "intensity": 0.65}]}
    r = climbs.recommend(easy, 47.0504, 8.5466, 250, 75)
    assert r["climbs"] == [] and "interval" in r["message"]


@needs_data
def test_start_points_by_name():
    names = [p["label"] for p in climbs.find_places("gold")]
    assert names[0] == "Goldau"  # names starting with the query, towns and villages first
    oberdorf = [p["label"] for p in climbs.find_places("Oberdorf")]
    assert len(set(oberdorf)) == len(oberdorf) > 1  # the many Oberdorfs are told apart
    assert climbs.find_places("g") == []
