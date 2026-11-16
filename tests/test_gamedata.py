from pathlib import Path

import pytest

from kneipenmonopoly.gamedata import GameDataError, distance_m, load_game_data, parse_game_data

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "kneipenmonopoly_spieldaten.json"


def test_real_game_data_loads():
    gd = load_game_data(DATA)
    assert len(gd.fields) == 22
    assert len(gd.groups) == 8
    assert len(gd.stations) == 4
    assert len(gd.reserve) == 10
    assert gd.fields["zucca"].group == "gruen"
    board = gd.board
    assert board.group_of("ampuette") == "dunkelblau"


def test_inconsistent_group_rejected():
    raw = {
        "groups": [{"id": "g", "name": "G", "color": "#000", "field_ids": ["a", "x"]}],
        "fields": [{"id": "a", "no": 1, "name": "A", "group": "g", "lat": 0, "lon": 0}],
    }
    with pytest.raises(GameDataError):
        parse_game_data(raw)


def test_distance_reasonable():
    # Fritzpatrick's -> Hudson's, laut Daten ca. 50 m auseinander
    d = distance_m(51.431095, 7.004159, 51.430927, 7.00481)
    assert 40 < d < 60


def test_example_cities_load_and_fit_in_1_5_km(tmp_path):
    import itertools
    import json

    from kneipenmonopoly.config import load_settings
    from kneipenmonopoly.db import DB
    from kneipenmonopoly.gamedata import distance_m, load_game_data, parse_game_data
    from kneipenmonopoly.service import Game, NullNotifier

    files = sorted((ROOT / "beispiele").glob("spieldaten_*.json"))
    assert len(files) >= 6
    for p in files:
        raw = json.loads(p.read_text(encoding="utf-8"))
        d = parse_game_data(raw)
        assert len(d.fields) == 22 and len(d.groups) == 8 and len(d.stations) == 4, p.name
        pts = [(x.lat, x.lon) for x in [*d.fields.values(), *d.stations.values(), *d.reserve.values()]]
        lats, lons = [a for a, _ in pts], [b for _, b in pts]
        assert distance_m(min(lats), lons[0], max(lats), lons[0]) <= 1500, p.name
        assert distance_m(lats[0], min(lons), lats[0], max(lons)) <= 1500, p.name
        fields = [(f.lat, f.lon) for f in d.fields.values()]
        assert min(distance_m(*a, *b) for a, b in itertools.combinations(fields, 2)) >= 45, p.name
    # Import in eine Session (wie „Setup übernehmen“) klappt
    s = load_settings(env={"DATA_DIR": str(tmp_path), "TEXTS_PATH": str(tmp_path / "t.toml"),
                           "GAME_DATA_PATH": str(ROOT / "kneipenmonopoly_spieldaten.json")})
    g = Game(s, load_game_data(s.game_data_path), DB(s.db_path), NullNotifier())
    res = g.import_setup(json.loads(files[0].read_text(encoding="utf-8")), set())
    assert res.ok, res.message
    assert g.data.city != "Essen"
