import asyncio
import copy
from pathlib import Path

from fastapi.testclient import TestClient

from kneipenmonopoly.config import load_settings
from kneipenmonopoly.db import DB
from kneipenmonopoly.gamedata import load_game_data
from kneipenmonopoly.service import Game, NullNotifier, normalize_game_data
from kneipenmonopoly.web.app import create_app

ROOT = Path(__file__).resolve().parent.parent


def make(tmp_path):
    s = load_settings(env={"DATA_DIR": str(tmp_path), "TEXTS_PATH": str(tmp_path / "t.toml"), "MAP_TILES": "false",
                           "ADMIN_PASSWORD": "pw", "SESSION_SECRET": "x" * 32,
                           "GAME_DATA_PATH": str(ROOT / "kneipenmonopoly_spieldaten.json")})
    return Game(s, load_game_data(s.active_game_data_path), DB(s.db_path), NullNotifier())


def data(g):
    return copy.deepcopy(normalize_game_data(g.game_data_raw()))


def test_normalize_keeps_repo_data_stable(tmp_path):
    g = make(tmp_path)
    d = data(g)
    assert [f["no"] for f in d["fields"]] == list(range(1, 23))
    assert g.data.fields["zucca"].no == next(f["no"] for f in d["fields"] if f["id"] == "zucca")


def test_add_street_and_bar_without_ids(tmp_path):
    g = make(tmp_path)
    d = data(g)
    d["groups"].append({"name": "Lila", "color": "#8000ff"})
    d["fields"].append({"name": "Neue Bar am Markt", "type": "kneipe", "group": None, "address": "Markt 1",
                        "lat": 51.4361, "lon": 7.0040})
    # Straße hat noch keine ID: Feld verweist per normalisiertem Namen
    d = normalize_game_data({**d, "fields": d["fields"][:-1]})
    d["fields"].append({"name": "Neue Bar am Markt", "type": "kneipe", "group": "lila", "address": "Markt 1",
                        "lat": 51.4361, "lon": 7.0040})
    res, notes = g.editor_save(d, "anna")
    assert res.ok, res.message
    assert "neue-bar-am-markt" in g.data.fields
    f = g.data.fields["neue-bar-am-markt"]
    assert f.group == "lila" and f.no == 23 and f.type == "kneipe"
    assert g.spot("neue-bar-am-markt").name == "Neue Bar am Markt"


def test_moving_bar_renumbers(tmp_path):
    g = make(tmp_path)
    d = data(g)
    fruh = next(f for f in d["fields"] if f["id"] == "frueher-oder-spaeter")
    fruh["group"] = "dunkelblau"                     # von Braun (1) ans Ende, wie der Editor es macht
    d["fields"] = [f for f in d["fields"] if f is not fruh] + [fruh]
    assert g.editor_save(d, "anna")[0].ok
    assert g.data.fields["mimaya"].no == 1
    assert g.data.fields["frueher-oder-spaeter"].no == 22
    assert "frueher-oder-spaeter" in g.data.groups["dunkelblau"].field_ids


def test_delete_bar_removes_its_task_and_makes_backup(tmp_path):
    g = make(tmp_path)
    g.set_task("zucca", "Singen")
    d = data(g)
    assert g.editor_save(d, "anna")[0].ok            # erster Stand importiert
    d["fields"] = [f for f in d["fields"] if f["id"] != "zucca"]
    res, _ = g.editor_save(d, "anna")
    assert res.ok and "zucca" not in g.data.fields
    assert "zucca" not in g.tasks()
    assert list((tmp_path / "spieldaten-backups").glob("spieldaten-*.json"))


def test_map_image_follows_editor(tmp_path):
    g = make(tmp_path)
    sig = g.map_signature()
    d = data(g)
    d["fields"][0]["name"] = "Umbenannt"
    g.editor_save(d, "anna")
    assert g.map_signature() != sig


def test_during_game_only_attributes(tmp_path):
    g = make(tmp_path)
    asyncio.run(g.start_game("anna"))
    d = data(g)
    d["fields"] = d["fields"][1:]                     # löschen: nicht erlaubt
    res, _ = g.editor_save(d, "anna")
    assert not res.ok and "Vorbereitung" in res.message
    d = data(g)
    z = next(f for f in d["fields"] if f["id"] == "zucca")
    z["lat"], z["name"] = 51.4395, "Zucca (Eingang Seite)"
    res, _ = g.editor_save(d, "anna")
    assert res.ok, res.message
    assert g.spot("zucca").lat == 51.4395 and g.spot("zucca").name == "Zucca (Eingang Seite)"


def test_invalid_data_rejected(tmp_path):
    g = make(tmp_path)
    d = data(g)
    d["fields"][0]["lat"] = 123
    res, _ = g.editor_save(d, "anna")
    assert not res.ok and "ungültig" in res.message.lower()
    d = data(g)
    d["fields"][0]["group"] = "gibts-nicht"
    assert not g.editor_save(d, "anna")[0].ok
    d = data(g)
    d["groups"] = []
    assert not g.editor_save(d, "anna")[0].ok
    assert not g.s.imported_game_data_path.exists()     # nichts geschrieben


def test_station_letter_clash_is_noted(tmp_path):
    g = make(tmp_path)
    d = data(g)
    d["stations"].append({"name": "Nebenbahnhof", "venue": "Kiosk", "lat": 51.436, "lon": 7.0})
    res, notes = g.editor_save(d, "anna")
    assert res.ok and any("beginnen beide mit N" in n for n in notes)


def test_editor_api(tmp_path):
    g = make(tmp_path)
    with TestClient(create_app(g.s, game=g, run_bot=False)) as client:
        client.post("/login", data={"name": "Anna", "password": "pw"})
        assert "Karten-Editor" in client.get("/editor").text
        j = client.get("/editor/api/daten").json()
        assert j["struktur"] is True and len(j["data"]["fields"]) == 22
        d = j["data"]
        d["reserve"].append({"name": "Neue Reserve", "address": "Rü 1", "lat": 51.43, "lon": 7.0})
        r = client.post("/editor/api/speichern", json=d)
        assert r.status_code == 200 and r.json()["ok"]
        assert any(x["name"] == "Neue Reserve" for x in r.json()["data"]["reserve"])
        bad = client.post("/editor/api/speichern", json={"groups": []})
        assert bad.status_code == 422 and not bad.json()["ok"]
