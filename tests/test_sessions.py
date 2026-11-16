import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kneipenmonopoly.config import load_settings
from kneipenmonopoly.db import DB
from kneipenmonopoly.gamedata import load_game_data
from kneipenmonopoly.service import Game, NullNotifier
from kneipenmonopoly.sessions import SessionManager, utcnow
from kneipenmonopoly.web.app import create_app

ROOT = Path(__file__).resolve().parent.parent
TOKEN_A = "111111111:" + "A" * 35
TOKEN_B = "222222222:" + "B" * 35
TOKEN_C = "333333333:" + "C" * 35
BAD = "999999999:" + "X" * 35


async def fake_check(token):
    if token == BAD:
        return False, "Telegram kennt diesen Bot-Token nicht."
    return True, "Bot" + token.split(":")[0][:3]


class Clock:
    def __init__(self):
        self.now = utcnow()

    def __call__(self):
        return self.now


def settings(tmp_path, **extra):
    env = {"DATA_DIR": str(tmp_path / "data"), "TEXTS_PATH": str(tmp_path / "texte.toml"),
           "SESSION_SECRET": "x" * 32, "MASTER_PASSWORD": "master123",
           "GAME_DATA_PATH": str(ROOT / "kneipenmonopoly_spieldaten.json")}
    env.update(extra)
    return load_settings(env=env)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def mgr(tmp_path):
    clock = Clock()
    m = SessionManager(settings(tmp_path), run_bots=False, check_token=fake_check, clock=clock)
    m.clock_obj = clock
    yield m
    run(m.stop())


def test_create_isolated_sessions(mgr):
    a = run(mgr.create("Rüttenscheid", TOKEN_A, "geheim1", "geheim1"))
    b = run(mgr.create("Altstadt", TOKEN_B, "geheim2", "geheim2"))
    assert a.ok and b.ok, (a.message, b.message)
    ga, gb = mgr.get(a.sid).game, mgr.get(b.sid).game
    ga.create_team("Alpha", "#f00", "AAA")
    assert [t["name"] for t in ga.teams()] == ["Alpha"] and gb.teams() == []
    assert ga.s.db_path != gb.s.db_path and ga.s.db_path.exists()
    assert ga.s.texts_path.parent == ga.s.data_dir
    assert mgr.check_password(a.sid, "geheim1") and not mgr.check_password(a.sid, "geheim2")
    over = {x["name"]: x for x in mgr.overview()}
    assert over["Rüttenscheid"]["bot"] == "Bot111" and over["Rüttenscheid"]["days_left"] in (29, 30)
    row = mgr.row(a.sid)
    assert "geheim1" not in json.dumps(dict(row))                  # nur Hash gespeichert


@pytest.mark.parametrize("name,token,pw,pw2,msg", [
    ("", TOKEN_A, "geheim1", "geheim1", "Namen"),
    ("X", TOKEN_A, "kurz", "kurz", "mindestens"),
    ("X", TOKEN_A, "geheim1", "geheim2", "stimmen nicht"),
    ("X", "", "geheim1", "geheim1", "Bot-Token"),
    ("X", "kein-token", "geheim1", "geheim1", "nicht wie ein Bot-Token"),
    ("X", BAD, "geheim1", "geheim1", "kennt diesen"),
])
def test_invalid_create_leaves_nothing(mgr, name, token, pw, pw2, msg):
    res = run(mgr.create(name, token, pw, pw2))
    assert not res.ok and msg in res.message
    assert mgr.rows() == []
    assert not mgr.base.sessions_dir.exists() or not any(mgr.base.sessions_dir.iterdir())


def test_same_bot_or_name_twice_rejected_and_limit(tmp_path):
    m = SessionManager(settings(tmp_path, MAX_SESSIONS="2"), run_bots=False, check_token=fake_check)
    assert run(m.create("Eins", TOKEN_A, "geheim1")).ok
    same_bot = run(m.create("Zwei", "111111111:" + "Z" * 35, "geheim1"))   # neuer Token, gleicher Bot
    assert not same_bot.ok and "Eins" in same_bot.message
    assert not run(m.create("eins", TOKEN_B, "geheim1")).ok                # Name (ohne Groß/klein)
    assert run(m.create("Zwei", TOKEN_B, "geheim1")).ok
    full = run(m.create("Drei", TOKEN_C, "geheim1"))
    assert not full.ok and "Maximum 2" in full.message


def test_setup_import_on_create(mgr):
    setup = {"format": "kneipenmonopoly-setup", "version": 1, "action_cards": ["Singt ein Lied"],
             "teams": [{"name": "Delta", "color": "#abcdef", "code": "DDD"}]}
    res = run(mgr.create("Mit Setup", TOKEN_A, "geheim1", None, setup))
    assert res.ok, res.message
    g = mgr.get(res.sid).game
    assert [t["name"] for t in g.teams()] == ["Delta"]
    assert [c["text"] for c in g.action_cards()] == ["Singt ein Lied"]
    bad = run(mgr.create("Kaputt", TOKEN_B, "geheim1", None, {"format": "irgendwas"}))
    assert not bad.ok and "Import" in bad.message and mgr.find_by_name("Kaputt") is None


def test_expiry_extend_and_delete(mgr):
    a = run(mgr.create("Alt", TOKEN_A, "geheim1")).sid
    b = run(mgr.create("Neu", TOKEN_B, "geheim1")).sid
    d_a = mgr.session_dir(a)
    assert d_a.exists()
    mgr.clock_obj.now += timedelta(days=20)
    assert mgr.extend(b).ok                                       # Neu: +30 Tage
    mgr.clock_obj.now += timedelta(days=11)
    assert run(mgr.delete_expired()) == ["Alt"]
    assert mgr.row(a) is None and mgr.get(a) is None and not d_a.exists()
    assert mgr.get(b) is not None
    assert run(mgr.delete(b)).ok and not mgr.session_dir(b).exists()


def test_password_and_token_change(mgr):
    a = run(mgr.create("A", TOKEN_A, "geheim1")).sid
    run(mgr.create("B", TOKEN_B, "geheim1"))
    assert not mgr.set_password(a, "kurz").ok
    assert mgr.set_password(a, "neues-pw", "neues-pw").ok and mgr.check_password(a, "neues-pw")
    assert not run(mgr.set_token(a, TOKEN_B)).ok                  # gehört Session B
    assert not run(mgr.set_token(a, BAD)).ok
    assert run(mgr.set_token(a, TOKEN_C)).ok
    assert mgr.get(a).settings.bot_token == TOKEN_C and mgr.row(a)["bot_username"] == "Bot333"


def test_legacy_single_game_becomes_first_session(tmp_path):
    s = settings(tmp_path, BOT_TOKEN=TOKEN_A, ADMIN_PASSWORD="altes-pw")
    # Altbestand wie vor dem Update: Datenbank, Fotos, Texte direkt in data/
    old = Game(s, load_game_data(s.active_game_data_path), DB(s.db_path), NullNotifier())
    old.create_team("Alpha", "#f00", "AAA")
    (s.photo_dir / "x.jpg").write_bytes(b"jpg")
    old.db.close()
    s.texts_path.write_text('[spiel]\nstart = "Los!"\n', encoding="utf-8")

    m = SessionManager(s, run_bots=False, check_token=fake_check)
    run(m.start())
    try:
        (row,) = m.rows()
        rt = m.get(row["id"])
        assert [t["name"] for t in rt.game.teams()] == ["Alpha"]
        assert (rt.settings.photo_dir / "x.jpg").exists() and not s.db_path.exists()
        assert rt.game.t("spiel.start") == "Los!"
        assert m.check_password(row["id"], "altes-pw") and row["bot_token"] == TOKEN_A
        assert m.check_master("master123") and not m.check_master("altes-pw")
    finally:
        run(m.stop())
    # Zweiter Start: nichts wird doppelt übernommen
    m2 = SessionManager(s, run_bots=False, check_token=fake_check)
    assert m2.migrate_legacy() is None and len(m2.rows()) == 1


def test_master_falls_back_to_admin_password(tmp_path):
    m = SessionManager(settings(tmp_path, MASTER_PASSWORD="", ADMIN_PASSWORD="altes-pw"), run_bots=False)
    assert m.check_master("altes-pw") and not m.check_master("")


# ---------------------------------------------------------------- Webseite
@pytest.fixture
def web(tmp_path):
    clock = Clock()
    m = SessionManager(settings(tmp_path), run_bots=False, check_token=fake_check, clock=clock)
    app = create_app(m.base, manager=m)
    with TestClient(app) as client:
        yield client, m


def create(client, name, token, pw="geheim1", admin="Anna", **files):
    return client.post("/sessions/neu", data={"session_name": name, "name": admin, "bot_token": token,
                                              "password": pw, "password2": pw}, files=files or None)


def test_web_create_login_and_isolation(web):
    client, m = web
    page = client.get("/login").text
    assert "Noch keine Session" in page and "Neue Session anlegen" in page
    r = create(client, "Rüttenscheid", TOKEN_A)
    assert r.status_code == 200 and "Rüttenscheid" in r.text and "@Bot111" in r.text   # direkt eingeloggt
    a = m.find_by_name("Rüttenscheid")["id"]
    client.post("/verwaltung/team/neu", data={"name": "Alpha", "color": "#ff0000", "code": "AAA"})
    assert [t["name"] for t in m.get(a).game.teams()] == ["Alpha"]

    bad = create(client, "Kaputt", BAD)
    assert bad.status_code == 400 and "kennt diesen" in bad.text and m.find_by_name("Kaputt") is None

    create(client, "Altstadt", TOKEN_B)
    b = m.find_by_name("Altstadt")["id"]
    assert "Alpha" not in client.get("/verwaltung").text                  # andere Session, andere Teams
    client.get("/logout")
    start = client.get("/login").text
    assert "Rüttenscheid" in start and "Altstadt" in start
    wrong = client.post("/login", data={"sid": a, "name": "Ben", "password": "geheim-falsch"})
    assert wrong.status_code == 401
    ok = client.post("/login", data={"sid": a, "name": "Ben", "password": "geheim1"})
    assert ok.status_code == 200 and "Alpha" in client.get("/verwaltung").text
    assert b != a


def test_web_session_deleted_logs_out(web):
    client, m = web
    create(client, "Weg", TOKEN_A)
    sid = m.find_by_name("Weg")["id"]
    r = client.post("/session/loeschen", data={"confirm": "falsch"})
    assert m.get(sid) is not None and "genau eintippen" in r.text
    r = client.post("/session/loeschen", data={"confirm": "Weg"})
    assert m.get(sid) is None and "gelöscht" in r.text
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_web_master(web):
    client, m = web
    create(client, "Eins", TOKEN_A)
    sid = m.find_by_name("Eins")["id"]
    client.get("/logout")
    assert client.post(f"/master/{sid}/loeschen", follow_redirects=False).headers["location"] == "/master"
    assert m.get(sid) is not None                                         # ohne Master-Login nichts
    assert client.post("/master/login", data={"password": "falsch"}).status_code == 401
    page = client.post("/master/login", data={"password": "master123"}).text
    assert "Eins" in page and "@Bot111" in page
    before = m.row(sid)["expires_at"]
    client.post(f"/master/{sid}/verlaengern")
    assert m.row(sid)["expires_at"] > before
    client.post(f"/master/{sid}/passwort", data={"password": "vom-master"})
    assert m.check_password(sid, "vom-master")
    r = client.post(f"/master/{sid}/oeffnen")
    assert r.status_code == 200 and "Dashboard" in r.text
    r = client.post(f"/master/{sid}/loeschen")
    assert m.get(sid) is None and "gelöscht" in r.text


def test_web_bot_warning_and_token_change(web):
    client, m = web
    create(client, "Eins", TOKEN_A)
    sid = m.find_by_name("Eins")["id"]
    m.get(sid).bot_error = "Telegram lehnt den Bot-Token ab"
    assert "Bot läuft nicht" in client.get("/").text
    r = client.post("/session/token", data={"token": TOKEN_B})
    assert "Neuer Bot läuft" in r.text and m.get(sid).settings.bot_token == TOKEN_B
