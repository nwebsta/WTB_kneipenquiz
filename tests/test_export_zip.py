import asyncio
import io
import zipfile

from test_web import EULE, ctx, login  # noqa: F401  (Fixture wiederverwenden)


def test_photo_and_chat_zip(ctx):  # noqa: F811
    client, game = ctx
    login(client)
    game.create_team("Alpha", "#ff0000", "AAA")
    game.join(1, "Gruppe A", "AAA")
    a = game.team_by_chat(1)["id"]
    asyncio.run(game.start_game("anna"))
    game.handle_location(a, *EULE)
    game.s.photo_dir.mkdir(parents=True, exist_ok=True)
    sub = game.s.photo_dir / "sub.jpg"
    sub.write_bytes(b"\xff\xd8sub\xff\xd9")
    extra = game.s.photo_dir / "extra.jpg"
    extra.write_bytes(b"\xff\xd8extra\xff\xd9")
    assert game.submit_photo(a, str(sub), "fid", "sub").ok
    game.log_message(team_id=a, chat_id=1, direction="in", kind="photo", photo_path=str(sub), sender="Max")
    game.log_message(team_id=a, chat_id=1, direction="in", kind="photo", photo_path=str(extra), text="Prost")
    game.log_message(team_id=a, chat_id=1, direction="in", kind="location", lat=EULE[0], lon=EULE[1], sender="Max")
    game.log_message(team_id=None, chat_id=99, direction="in", kind="text", text="/join falsch", chat_title="Fremde")
    game.log_message(team_id=a, chat_id=1, direction="out", kind="text", text="Moin\nzweite Zeile", sender="Bot")
    before = (game.db.one("SELECT COUNT(*) n FROM messages")["n"], sorted(p.name for p in game.s.photo_dir.iterdir()))

    r = client.get("/export/fotos.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "fotos-" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    jpgs = [n for n in names if n.endswith(".jpg")]
    assert len(jpgs) == 2 and all(n.startswith("Alpha/") for n in jpgs)
    assert any("Die Eule" in n and "offen" in n for n in jpgs)
    assert any("Chat" in n for n in jpgs)
    assert b"sub" in z.read(next(n for n in jpgs if "Die Eule" in n))
    assert "Prost" in z.read("fotos.csv").decode("utf-8-sig")

    r = client.get("/export/chats.zip")
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert "Alpha.txt" in z.namelist() and "Ohne Team - Fremde.txt" in z.namelist()
    alpha = z.read("Alpha.txt").decode("utf-8")
    assert "📍 Standort" in alpha and "Moin\n    zweite Zeile" in alpha and "← Max" in alpha
    assert "/join falsch" in z.read("alle_nachrichten.csv").decode("utf-8-sig")

    # Export ändert nichts und hinterlässt keine Dateien
    after = (game.db.one("SELECT COUNT(*) n FROM messages")["n"], sorted(p.name for p in game.s.photo_dir.iterdir()))
    assert before == after


def test_export_needs_login(ctx):  # noqa: F811
    client, _ = ctx
    for path in ("/export/fotos.zip", "/export/chats.zip"):
        assert client.get(path, follow_redirects=False).status_code in (303, 401)


# ---------------------------------------------------------------- Recap-PDF
def test_recap_data_balances_and_game_time(tmp_path):
    from test_service import Clock, run, visit
    from kneipenmonopoly.config import load_settings
    from kneipenmonopoly.db import DB
    from kneipenmonopoly.gamedata import load_game_data
    from kneipenmonopoly.service import Game, NullNotifier
    from test_web import ROOT

    s = load_settings(env={"DATA_DIR": str(tmp_path), "TEXTS_PATH": str(tmp_path / "t.toml"),
                           "GAME_DATA_PATH": str(ROOT / "kneipenmonopoly_spieldaten.json")})
    clock = Clock()
    g = Game(s, load_game_data(s.game_data_path), DB(s.db_path), NullNotifier(), clock=clock)
    g.create_team("Alpha", "#f00", "AAA")
    g.create_team("Pils-Piraten mit sehr langem Namen", "#ffff00", "BBB")
    a, b = (t["id"] for t in g.teams())
    run(g.start_game("anna"))
    clock.advance(minutes=10)
    visit(g, a, EULE)                                   # Alpha kauft Die Eule: +1
    clock.advance(minutes=5)
    run(g.pause_game("anna"))
    clock.advance(minutes=20)
    run(g.resume_game("anna"))
    clock.advance(minutes=5)
    visit(g, b, EULE)                                   # Miete: Alpha +1
    clock.advance(minutes=1)
    run(g.adjust_points(b, -2, "anna", "Test"))
    run(g.end_game("anna"))

    d = g.recap_data()
    assert [(r.delta, r.balances[a], r.balances[b]) for r in d.rows] == [(1, 1, 0), (1, 2, 0), (-2, 2, -2)]
    assert round(d.rows[0].game_min) == 10 and round(d.rows[1].game_min) == 20   # Pause zählt nicht
    assert len(d.pauses) == 1 and "gespielt 0:21" in d.subtitle
    assert [t.place for t in d.teams] == [1, 2]

    out = tmp_path / "recap.pdf"
    assert g.export_recap_pdf(out) == 2
    assert out.read_bytes().startswith(b"%PDF")


def test_recap_pdf_download_and_empty_game(ctx):  # noqa: F811
    client, game = ctx
    login(client)
    r = client.get("/export/recap.pdf")                 # vor dem Start: leeres Recap, kein Fehler
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF") and "recap-" in r.headers["content-disposition"]
    for i in range(8):                                  # viele Teams: eine Saldo-Spalte statt je Team
        game.create_team(f"Team {i}", "#123456", f"T{i}X")
    assert client.get("/export/recap.pdf").content.startswith(b"%PDF")
    assert "Recap (PDF)" in client.get("/protokoll").text
