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
