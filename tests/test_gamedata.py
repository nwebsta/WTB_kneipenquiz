from pathlib import Path

import pytest

from kneipenmonopoly.gamedata import GameDataError, distance_m, load_game_data, parse_game_data

DATA = Path(__file__).resolve().parent.parent / "kneipenmonopoly_spieldaten.json"


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
