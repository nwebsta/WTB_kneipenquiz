import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from kneipenmonopoly.config import load_settings
from kneipenmonopoly.db import DB
from kneipenmonopoly.gamedata import load_game_data
from kneipenmonopoly.service import SETUP_PARTS, Game, NullNotifier
from kneipenmonopoly.web.app import create_app

ROOT = Path(__file__).resolve().parent.parent
ALL = set(SETUP_PARTS)


def make(tmp_path, name):
    d = tmp_path / name
    s = load_settings(env={"DATA_DIR": str(d), "TEXTS_PATH": str(d / "texte.toml"), "ADMIN_PASSWORD": "pw",
                           "SESSION_SECRET": "x" * 32,
                           "GAME_DATA_PATH": str(ROOT / "kneipenmonopoly_spieldaten.json")})
    return Game(s, load_game_data(s.active_game_data_path), DB(s.db_path), NullNotifier())


def test_round_trip_laptop_to_server(tmp_path):
    laptop = make(tmp_path, "laptop")
    laptop.create_team("Alpha", "#ff0000", "AAA")
    laptop.create_team("Beta", "#00ff00", "BBB")
    laptop.join(123, "Gruppe", "AAA")
    laptop.add_action_card("Trink dein Bier halb aus!")
    laptop.add_action_card("Singt ein Lied")
    laptop.set_task("die-eule", "Stoßt mit dem Wirt an")
    laptop.add_random_task("Macht ein Gruppenfoto mit Fremden")
    laptop.save_event_card(None, 200, "broadcast", "Letzte Runde!")
    laptop.set_duration("2:30")
    exported = json.loads(json.dumps(laptop.export_setup()))       # wie über eine Datei
    assert "chat_id" not in json.dumps(exported["teams"])          # Telegram-Gruppen bleiben lokal

    server = make(tmp_path, "server")
    server.create_team("Alpha", "#000000", "ZZZ")                  # existiert schon, wird aktualisiert
    res = server.import_setup(exported, ALL)
    assert res.ok, res.message
    teams = {t["name"]: t for t in server.teams()}
    assert set(teams) == {"Alpha", "Beta"} and teams["Alpha"]["color"] == "#ff0000" and teams["Alpha"]["code"] == "AAA"
    assert [c["text"] for c in server.action_cards()] == ["Trink dein Bier halb aus!", "Singt ein Lied"]
    assert server.tasks() == {"die-eule": "Stoßt mit dem Wirt an"}
    assert [r["text"] for r in server.random_tasks()] == ["Macht ein Gruppenfoto mit Fremden"]
    assert any(c["text"] == "Letzte Runde!" for c in server.event_cards()) and len(server.event_cards()) == 4
    assert server.duration_min() == 150


def test_texts_travel_with_setup(tmp_path):
    laptop = make(tmp_path, "laptop")
    assert laptop.t.save("spiel.start", "🚦 <b>Prost, los!</b>") is None
    exported = json.loads(json.dumps(laptop.export_setup()))
    assert exported["texts"] == {"spiel.start": "🚦 <b>Prost, los!</b>"}      # nur Geänderte
    server = make(tmp_path, "server")
    assert server.t.save("spiel.pause", "Pause!") is None                     # wird durch Import Standard
    exported["texts"]["gibts.nicht"] = "x"                                     # ungültig -> übersprungen
    res = server.import_setup(exported, {"texte"})
    assert res.ok and "übersprungen" in res.message, res.message
    assert server.t("spiel.start") == "🚦 <b>Prost, los!</b>"
    assert server.t("spiel.pause") == server.t.defaults["spiel.pause"].rstrip("\n")
    assert server.t.overrides() == {"spiel.start": "🚦 <b>Prost, los!</b>"}


def test_only_selected_parts(tmp_path):
    laptop = make(tmp_path, "laptop")
    laptop.add_action_card("Nur diese")
    laptop.create_team("Gamma", "#123456", "GGG")
    server = make(tmp_path, "server")
    server.add_action_card("Bleibt")
    res = server.import_setup(laptop.export_setup(), {"teams"})
    assert res.ok
    assert [c["text"] for c in server.action_cards()] == ["Bleibt"]
    assert [t["name"] for t in server.teams()] == ["Gamma"]


def test_plain_game_data_import_moves_kiosk_and_survives_restart(tmp_path):
    server = make(tmp_path, "server")
    raw = json.loads((ROOT / "kneipenmonopoly_spieldaten.json").read_text(encoding="utf-8"))
    sued = next(s for s in raw["stations"] if s["id"] == "bahnhof-sued")
    sued.update(lat=51.4280, lon=7.0030, venue="Neuer Kiosk")
    res = server.import_setup(raw, set())                           # reine Spieldaten-Datei
    assert res.ok, res.message
    assert server.spot("bahnhof-sued").lat == 51.4280
    assert "importiert" in server.game_data_source()
    # Neustart: importierte Spieldaten haben Vorrang
    again = make(tmp_path, "server")
    assert again.spot("bahnhof-sued").lat == 51.4280
    assert again.reset_game_data().ok
    assert again.spot("bahnhof-sued").lat != 51.4280


def test_broken_game_data_is_rejected(tmp_path):
    server = make(tmp_path, "server")
    raw = json.loads((ROOT / "kneipenmonopoly_spieldaten.json").read_text(encoding="utf-8"))
    raw["groups"][0]["field_ids"].append("gibts-nicht")
    res = server.import_setup(raw, set())
    assert not res.ok and "fehlerhaft" in res.message
    assert not server.s.imported_game_data_path.exists()


def test_import_blocked_while_game_runs(tmp_path):
    server = make(tmp_path, "server")
    asyncio.run(server.start_game("anna"))
    res = server.import_setup(server.export_setup(), ALL)
    assert not res.ok and "Vorbereitung" in res.message


def test_web_upload_and_download(tmp_path):
    server = make(tmp_path, "server")
    app = create_app(server.s, game=server, run_bot=False)
    with TestClient(app) as client:
        client.post("/login", data={"name": "Anna", "password": "pw"})
        setup = {"format": "kneipenmonopoly-setup", "version": 1, "action_cards": ["Hochgeladen"],
                 "teams": [{"name": "Delta", "color": "#abcdef", "code": "DDD"}]}
        r = client.post("/verwaltung/import", data={"teil": ["aktionskarten", "teams"]},
                        files={"datei": ("setup.json", json.dumps(setup).encode("utf-8"), "application/json")})
        assert r.status_code == 200 and "Import fertig" in r.text
        assert [c["text"] for c in server.action_cards()] == ["Hochgeladen"]
        down = client.get("/export/setup.json")
        assert down.json()["teams"][0]["name"] == "Delta"
        assert "attachment" in down.headers["content-disposition"]
        assert client.get("/export/spieldaten.json").json()["fields"]
        bad = client.post("/verwaltung/import", data={"teil": ["teams"]},
                          files={"datei": ("x.json", b"kein json", "application/json")})
        assert "Keine gültige JSON-Datei" in bad.text
        assert "Export / Import" in client.get("/verwaltung").text
