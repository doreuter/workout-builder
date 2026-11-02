"""Running tracks: the built database has the tracks we know, and runs get a plan in laps."""

import gzip
import json

import pytest

from app import tracks
from app.climbdb import haversine

RUN = {"name": "5 × 1000", "sport": "running", "steps": [
    {"type": "warmup", "duration_s": 900, "intensity": 0.6, "intensity_end": 0.75},
    {"type": "repeat", "count": 5, "steps": [{"type": "steady", "distance_m": 1000, "intensity": 1.03},
                                             {"type": "steady", "duration_s": 120, "intensity": 0.6}]},
    {"type": "cooldown", "duration_s": 600, "intensity": 0.6}]}

# Tracks we know, (lat, lon) of the oval: in Swiss OSM some are tagged athletics, some running, some untagged.
KNOWN = {
    "Letzigrund, Zürich": (47.3829, 8.5040),
    "Leichtathletikstadion Wankdorf, Bern": (46.9637, 7.4655),
    "Herti, Zug": (47.1804, 8.5093),
    "Röhrliberg, Cham": (47.1830, 8.4536),
    "Schützenmatte, Basel": (47.5513, 7.5735),
}


@pytest.fixture(scope="module")
def db():
    if not tracks.available():
        pytest.skip("climbdata/switzerland-tracks.json.gz isn't built")
    return json.load(gzip.open(tracks.DATA, "rt"))["tracks"]


@pytest.mark.parametrize("name", KNOWN)
def test_known_track_is_in(db, name):
    assert any(haversine(KNOWN[name], t["at"]) < 200 and t["lap_m"] == 400 for t in db), name


def test_no_woodchip_trails(db):
    assert not [t for t in db if "finnenbahn" in (t["name"] or "").lower() or t["surface"] == "woodchips"]
    assert len(db) > 80 and all(180 <= t["lap_m"] <= 400 for t in db)


def test_laps():
    assert tracks.laps(1000, 400) == "2½ laps"
    assert tracks.laps(400, 400) == "1 lap"
    assert tracks.laps(300, 400) == "¾ lap"
    assert tracks.laps(1000, 310) == "3¼ laps"
    assert tracks.laps(1000, 360) == "2¾ laps"  # close enough to the quarter


def test_doing_outside_on_a_track():
    out = tracks.doing_outside(RUN, 270)
    assert any("a lap takes about 1:45" in line for line in out["howto"])
    assert out["assumed"] == {"pace": False, "pace_s": 270}
    assert tracks.doing_outside(RUN, None)["assumed"]["pace"] is True
    easy = {"name": "Easy", "sport": "running", "steps": [{"type": "steady", "duration_s": 2400, "intensity": 0.7}]}
    assert tracks.doing_outside(easy, 270) == {"howto": [], "message": tracks.NO_EFFORTS}


def test_tracks_near_zurich(db):
    r = tracks.recommend(RUN, 47.3769, 8.5417, 270)
    names = [t["name"] for t in r["tracks"]]
    assert len(names) == 4 and any("Letzigrund" in n for n in names)
    plan = r["tracks"][0]["plan"]
    assert any("5 × 1'000 m: 2½ laps each" in line and "1:45 a lap" in line for line in plan)
    assert any("far side" in line for line in plan)  # half laps finish on the home straight
    assert tracks.recommend(RUN, 45.0, 6.0, 270)["tracks"] == []  # far from Switzerland
