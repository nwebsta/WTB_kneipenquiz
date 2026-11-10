from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kneipenmonopoly.config import load_settings
from kneipenmonopoly.db import DB
from kneipenmonopoly.gamedata import load_game_data
from kneipenmonopoly.service import Game, NullNotifier
from kneipenmonopoly.web.app import create_app

ROOT = Path(__file__).resolve().parent.parent
EULE = (51.439342, 7.012116)


@pytest.fixture
def ctx(tmp_path):
    s = load_settings(env={"DATA_DIR": str(tmp_path), "TEXTS_PATH": str(tmp_path / "texte.toml"), "ADMIN_PASSWORD": "geheim", "SESSION_SECRET": "x" * 32,
                           "GAME_DATA_PATH": str(ROOT / "kneipenmonopoly_spieldaten.json")})
    game = Game(s, load_game_data(s.game_data_path), DB(s.db_path), NullNotifier())
    app = create_app(s, game=game, run_bot=False)
    with TestClient(app) as client:
        yield client, game


def login(client):
    r = client.post("/login", data={"name": "Anna", "password": "geheim"}, follow_redirects=False)
    assert r.status_code == 303


def test_requires_login(ctx):
    client, _ = ctx
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = client.get("/partials/queue", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"
    assert client.post("/login", data={"name": "x", "password": "falsch"}).status_code == 401


def test_full_admin_flow(ctx):
    client, game = ctx
    login(client)
    for path in ["/", "/pruefen", "/karte", "/chats", "/verwaltung", "/protokoll", "/partials/dashboard"]:
        assert client.get(path).status_code == 200, path

    client.post("/verwaltung/team/neu", data={"name": "Alpha", "color": "#ff0000", "code": "AAA"})
    client.post("/verwaltung/team/neu", data={"name": "Beta", "color": "#00ff00", "code": ""})
    assert len(game.teams()) == 2
    game.join(1, "chat", "AAA")
    client.post("/verwaltung/feld/die-eule/aufgabe", data={"text": "Singt ein Lied"})
    client.post("/verwaltung/zufallsaufgabe/neu", data={"text": "Prostet dem Nachbartisch zu"})
    client.post("/verwaltung/spiel/start")
    assert game.phase == "running"

    a = game.team_by_chat(1)["id"]
    r = game.handle_location(a, *EULE)
    assert "Singt ein Lied" in r.text
    photo = game.s.photo_dir / "test.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xd9")
    assert game.submit_photo(a, str(photo), "fid", "test").ok
    game.log_message(team_id=a, chat_id=1, direction="in", kind="photo", photo_path=str(photo), file_unique_id="test")
    game.log_message(team_id=a, chat_id=1, direction="in", kind="location", text="Standort", lat=EULE[0], lon=EULE[1])

    page = client.get("/pruefen").text
    assert "Die Eule" in page and "Singt ein Lied" in page and "kauft Die Eule" in page
    assert "nicht geteilt" in page
    game.update_live(a, *EULE)
    page = client.get("/pruefen").text
    assert "jetzt:" in page and "ok-text" in page
    live = client.get("/api/karte").json()["live"]
    assert live[0]["team"] == "Alpha" and live[0]["lat"] == EULE[0] and live[0]["ended"] is False
    sub = game.pending_submissions()[0]
    # Bild-URL muss eindeutig sein, sonst zeigt der Browser nach einem Reset alte Bilder
    assert f"/foto/{sub['id']}?v=test" in page
    foto = client.get(f"/foto/{sub['id']}?v=test")
    assert foto.status_code == 200 and "no-cache" in foto.headers["cache-control"]
    r = client.post(f"/pruefen/{sub['id']}/ok", data={"task_done": "1"})
    assert "Freigegeben" in r.text
    r = client.post(f"/pruefen/{sub['id']}/ok", data={})
    assert "Schon erledigt" in r.text
    assert game.state().points_of(a) == 2

    data = client.get("/api/karte").json()
    assert any(s["owner"] == "Alpha" for s in data["spots"])
    assert data["positions"][0]["team"] == "Alpha"
    chats = client.get(f"/chats?team={a}").text
    assert "/chatfoto/" in chats and "openstreetmap" in chats
    # Gruppe ohne Team (z.B. vor /join) erscheint unter „nicht verknüpfte Chats“
    game.log_message(team_id=None, chat_id=-999, direction="in", kind="text", text="/join FALSCH",
                     sender="Unbekannt", chat_title="Gruppe Gamma")
    free = client.get("/chats?team=frei").text
    assert "Gruppe Gamma" in free and "nicht verknüpft" in free and "/join FALSCH" in free
    assert "Gruppe Gamma" not in client.get(f"/partials/chats?team={a}").text
    assert "nicht verknüpfte Chats (1)" in client.get("/chats").text

    client.post("/verwaltung/punkte", data={"team": a, "delta": "-1", "comment": "Test"})
    assert game.state().points_of(a) == 1
    client.post("/verwaltung/besitzer", data={"spot": "die-eule", "team": "", "comment": "Test"})
    assert "die-eule" not in game.state().owners
    client.post("/verwaltung/feld/zucca/ersetzen", data={"reserve": "zweibar"})
    assert game.spot("zucca").name == "Zweibar"
    client.post("/verwaltung/feld/gatz/deaktivieren")
    assert not game.spot("gatz").active
    client.post("/verwaltung/spieldauer", data={"dauer": "3:00"})
    assert game.end_time() is not None and game.duration_min() == 180
    dash = client.get("/partials/dashboard").text
    assert "⏱️ noch 2:59" in dash or "⏱️ noch 3:00" in dash
    assert client.get("/verwaltung").status_code == 200

    proto = client.get("/protokoll").text
    assert "besucht Die Eule" in proto
    ev = [e for e in game.db.events() if e.type == "VISIT_APPROVED"][0]
    client.post(f"/protokoll/{ev.id}/zuruecknehmen", data={"comment": "Test"})
    assert game.state().points_of(a) == -1

    assert "Alpha" in client.get("/export/stand.csv").text
    assert "VISIT_APPROVED" in client.get("/export/protokoll.csv").text
    assert client.get("/export/spiel.json").json()["phase"] == "running"

    client.post("/chats/senden", data={"target": "alle", "text": "Hallo"})
    assert any("Hallo" in t for _, t in game.notifier.sent)

    # Ereignis- und Aktionskarten verwalten, Videoanruf in der Prüf-Warteschlange
    client.post("/verwaltung/ereigniskarte/neu", data={"offset": "3:00", "kind": "broadcast", "text": "Letzte Runde!"})
    assert any(c["offset_min"] == 180 for c in game.event_cards())
    client.post("/verwaltung/aktionskarte/neu", data={"text": "Singt ein Lied"})
    assert "Singt ein Lied" in client.get("/verwaltung").text
    import asyncio
    asyncio.run(game.request_action(a))
    page = client.get("/pruefen").text
    assert "Videoanruf" in page and "Singt ein Lied" in page and "Die Eule" in page
    call = game.open_calls()[0]
    r = client.post(f"/pruefen/anruf/{call['id']}/ok")
    assert "Gespeichert" in r.text
    assert "Schon erledigt" in client.post(f"/pruefen/anruf/{call['id']}/nein").text
    assert "Ereigniskarten" in client.get("/partials/dashboard").text

    client.post("/verwaltung/testmodus", data={"on": "1"})
    assert game.test_mode()
    assert "TESTMODUS" in client.get("/").text
    b = game.teams()[1]["id"]
    assert game.test_checkin(b, "1").ok
    game.submit_photo(b, None, "f", "tb")
    assert "Test-Check-in" in client.get("/pruefen").text
    client.post("/verwaltung/testmodus", data={})
    assert not game.test_mode()

    client.post("/verwaltung/spiel/ende")
    assert game.phase == "ended"
    client.post("/verwaltung/datenschutz", data={"confirm": "LÖSCHEN"})
    assert game.messages() == []

    client.post("/verwaltung/reset", data={"confirm": "falsch"})
    assert game.phase == "ended"
    client.post("/verwaltung/reset", data={"confirm": "RESET"})
    assert game.phase == "setup" and len(game.teams()) == 2
