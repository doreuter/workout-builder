"""Acceptance tests for the built climb database: climbs we know must be in it, whole, with sane numbers."""

import math
from pathlib import Path

import pytest

from app.climbdb import path_points, read_climbs

DATA = Path(__file__).resolve().parent.parent / "climbdata" / "switzerland.jsonl.gz"

# name, foot, top, length km (lo, hi), average % (lo, hi)
KNOWN = [
    ("Steinen → Ecce Homo", (47.0500, 8.6108), (47.0694, 8.6170), (2.8, 4.0), (6.5, 9.0)),
    ("Zug → Zugerberg", (47.1650, 8.5200), (47.1431, 8.5362), (5.0, 8.0), (6.0, 9.0)),
    ("Schwyz → Schwyzerhöhe", (47.0110, 8.6441), (46.9899, 8.6298), (2.5, 4.5), (8.0, 11.0)),  # flat by the Muota, then ~9.5 %
    ("Schwyz → Ibergeregg", (47.0212, 8.6536), (47.0174, 8.7332), (9.0, 12.5), (7.0, 9.5)),
    ("Willerzell → Sattelegg", (47.1304, 8.7936), (47.1273, 8.8468), (4.0, 6.5), (5.0, 9.5)),
    ("Oberägeri → Raten", (47.1347, 8.6133), (47.1418, 8.6633), (4.0, 7.0), (4.5, 8.0)),
    ("Muotathal → Pragel", (46.9747, 8.7658), (46.9994, 8.8695), (8.0, 14.0), (6.5, 10.0)),
]


def _hav(a, b):
    r = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


@pytest.fixture(scope="module")
def climbs():
    if not DATA.exists():
        pytest.skip("climbdata/switzerland.jsonl.gz isn't built")
    return read_climbs(DATA)[1]


def _section(c, foot, top, near=350):
    """The foot -> top stretch of climb c, as (km, average %), if the climb rides from one to the other."""
    p = path_points(c)
    fi = min(range(len(p)), key=lambda i: _hav(foot, p[i][:2]))
    ti = min(range(len(p)), key=lambda i: _hav(top, p[i][:2]))
    top_off = _hav(top, p[ti][:2])
    # Roads can top out on a flat summit area a little short of the pass marker.
    if ti <= fi or _hav(foot, p[fi][:2]) > near * 4 or (top_off > near and not (ti == len(p) - 1 and top_off <= 1500)):
        return None
    km = (p[ti][3] - p[fi][3]) / 1000
    return km, (p[ti][2] - p[fi][2]) / (km * 10), _hav(foot, p[fi][:2])


@pytest.mark.parametrize("name,foot,top,length,avg", KNOWN, ids=[k[0] for k in KNOWN])
def test_known_climb_is_found_whole(climbs, name, foot, top, length, avg):
    found = [s for c in climbs if (s := _section(c, foot, top))]
    assert found, f"{name} isn't in the climb database"
    km, pct, _ = min(found, key=lambda s: s[2])
    assert length[0] <= km <= length[1] and avg[0] <= pct <= avg[1], f"{name}: {km:.1f} km at {pct:.1f}%"


def test_climbs_are_well_formed(climbs):
    assert len(climbs) > 500
    for c in climbs[:200]:
        assert c["length_m"] >= 1000 and c["gain_m"] >= 50 and c["avg"] >= 3
        p = path_points(c)
        assert p[0][3] == 0 and p[-1][3] == c["length_m"] and abs(p[-1][2] - p[0][2] - c["gain_m"]) <= 1
        assert c["from"] and c["to"]


# The database covers all of Switzerland: passes and hills in every region, (top, minimum climb km).
ACROSS_CH = {
    "Sustenpass (from Wassen)": ((46.7297, 8.4473), 15),
    "Julierpass": ((46.4724, 9.7280), 6),
    "Weissenstein (from Solothurn)": ((47.2525, 7.5094), 8),
    "Col du Mollendruz": ((46.6487, 6.3653), 8),
    "Gurnigelpass": ((46.7323, 7.4481), 8),
    "Col des Mosses": ((46.3966, 7.1024), 12),
    "Lukmanier": ((46.5631, 8.8013), 12),
}


@pytest.mark.parametrize("name", ACROSS_CH)
def test_climbs_across_switzerland(climbs, name):
    top, km = ACROSS_CH[name]
    near = [c for c in climbs if _hav(top, c["top"]) < 600]
    assert any(c["length_m"] >= km * 1000 for c in near), f"{name}: {[(c['from'], c['length_m']) for c in near]}"


def test_road_graph_packs_and_unpacks():
    from app.climbdb import pack_road_graph, unpack_road_graph
    g = unpack_road_graph(pack_road_graph([[47.0, 8.5], [47.01, 8.5], [47.0, 8.51]], [[0, 1, 1100], [0, 2, 760]]))
    neighbours = lambda i: sorted(zip(g["nbr"][g["start"][i]:g["start"][i + 1]], g["length"][g["start"][i]:g["start"][i + 1]]))
    assert neighbours(0) == [(1, 1100.0), (2, 760.0)] and neighbours(1) == [(0, 1100.0)]
    assert list(g["lat"]) == [47.0, 47.01, 47.0]
