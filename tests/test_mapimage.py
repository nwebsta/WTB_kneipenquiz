import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from kneipenmonopoly.config import load_settings
from kneipenmonopoly.db import DB
from kneipenmonopoly.gamedata import load_game_data
from kneipenmonopoly.mapimage import _spread, short_address
from kneipenmonopoly.service import Game, NullNotifier
from kneipenmonopoly.web.app import create_app

ROOT = Path(__file__).resolve().parent.parent


def make(tmp_path):
    s = load_settings(env={"DATA_DIR": str(tmp_path), "TEXTS_PATH": str(tmp_path / "t.toml"), "MAP_TILES": "false",
                           "ADMIN_PASSWORD": "pw", "SESSION_SECRET": "x" * 32,
                           "GAME_DATA_PATH": str(ROOT / "kneipenmonopoly_spieldaten.json")})
    return Game(s, load_game_data(s.active_game_data_path), DB(s.db_path), NullNotifier())


def test_render_offline_and_cache(tmp_path):
    g = make(tmp_path)
    path = asyncio.run(g.ensure_map_image())
    img = Image.open(path)
    assert img.format == "JPEG" and img.width > 1400 and img.height > 1000
    st = g.map_status()
    assert st["exists"] and not st["outdated"] and st["tiles"] is False
    before = path.stat().st_mtime_ns
    asyncio.run(g.ensure_map_image())
    assert path.stat().st_mtime_ns == before             # unverändert: nicht neu gezeichnet


def test_map_follows_game_data_changes(tmp_path):
    g = make(tmp_path)
    asyncio.run(g.ensure_map_image())
    sig = g.map_status()["signature"]
    asyncio.run(g.replace_spot("zucca", "zweibar", "anna"))   # Bar ersetzt -> andere Karte
    assert g.map_status()["outdated"] and g.map_status()["signature"] != sig
    spec = g.map_spec()
    names = [f.name for grp in spec.groups for f in grp.fields]
    assert "Zweibar" in names and "Zucca" not in names
    assert "zweibar" not in [r.name.lower() for r in spec.reserve]  # benutzte Reserve nicht mehr gelistet
    asyncio.run(g.ensure_map_image())
    assert not g.map_status()["outdated"]


def test_custom_game_data_is_drawn(tmp_path):
    import json
    g = make(tmp_path)
    raw = json.loads((ROOT / "kneipenmonopoly_spieldaten.json").read_text(encoding="utf-8"))
    raw["game"] = "Kneipenmonopoly Testviertel"
    raw["fields"][0]["name"] = "Meine eigene Kneipe"
    assert g.import_setup(raw, set()).ok
    spec = g.map_spec()
    assert spec.title == "Kneipenmonopoly Testviertel"
    assert any(f.name == "Meine eigene Kneipe" for grp in spec.groups for f in grp.fields)
    assert asyncio.run(g.ensure_map_image()).exists()


def test_overlapping_markers_are_spread():
    pts = [(100.0, 100.0), (100.0, 100.0), (105.0, 102.0)]
    out = _spread(pts, [16, 16, 21])
    for i in range(3):
        for j in range(i + 1, 3):
            dist = ((out[i][0] - out[j][0]) ** 2 + (out[i][1] - out[j][1]) ** 2) ** 0.5
            assert dist >= [16, 16, 21][i] + [16, 16, 21][j] - 0.5


def test_short_address():
    assert short_address("Rüttenscheider Str. 201, 45130/45131 Essen") == "Rüttenscheider Str. 201"
    assert short_address("Rü 63") == "Rü 63"


def test_preview_in_admin(tmp_path):
    g = make(tmp_path)
    with TestClient(create_app(g.s, game=g, run_bot=False)) as client:
        client.post("/login", data={"name": "Anna", "password": "pw"})
        assert "Noch nicht erzeugt" in client.get("/verwaltung").text
        r = client.post("/verwaltung/karte/rendern")
        assert "Kartenbild neu erzeugt" in r.text and "/karte/bild.jpg" in r.text
        img = client.get("/karte/bild.jpg")
        assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
        dl = client.get("/karte/bild.jpg?download=1")
        assert "attachment" in dl.headers["content-disposition"]
