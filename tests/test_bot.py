import asyncio
from pathlib import Path
from types import SimpleNamespace

from telegram.error import ChatMigrated

from kneipenmonopoly.bot import TelegramNotifier, keyboard
from kneipenmonopoly.config import load_settings
from kneipenmonopoly.db import DB
from kneipenmonopoly.gamedata import load_game_data
from kneipenmonopoly.service import Game

ROOT = Path(__file__).resolve().parent.parent


class FakeBot:
    """Erster Versand an die alte Gruppe scheitert wie bei Telegram mit ChatMigrated."""

    def __init__(self, old: int, new: int):
        self.old, self.new = old, new
        self.sent: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id, text, **kw):
        if chat_id == self.old:
            raise ChatMigrated(self.new)
        self.sent.append((chat_id, text, kw.get("reply_markup")))


def test_send_follows_supergroup_migration(tmp_path):
    s = load_settings(env={"DATA_DIR": str(tmp_path), "TEXTS_PATH": str(tmp_path / "texte.toml"), "GAME_DATA_PATH": str(ROOT / "kneipenmonopoly_spieldaten.json")})
    g = Game(s, load_game_data(s.game_data_path), DB(s.db_path))
    g.create_team("Alpha", "#f00", "AAA")
    g.join(-5357774063, "B", "AAA")
    bot = FakeBot(-5357774063, -1004315904282)
    g.notifier = TelegramNotifier(SimpleNamespace(bot=bot), g)
    team_id = g.teams()[0]["id"]
    asyncio.run(g.send_team(team_id, "Hallo", [("🎬 Aktionskarte ziehen", "aktion")]))
    assert bot.sent[0][0] == -1004315904282
    assert bot.sent[0][2] is not None
    assert g.team(team_id)["chat_id"] == -1004315904282
    assert g.messages()[-1]["chat_id"] == -1004315904282


class PhotoBot:
    def __init__(self):
        self.photos = []

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.photos.append((chat_id, "datei" if hasattr(photo, "read") else photo))
        return SimpleNamespace(photo=[SimpleNamespace(file_id="klein"), SimpleNamespace(file_id="FILE123")])


def test_map_is_sent_as_image_and_reused(tmp_path):
    s = load_settings(env={"DATA_DIR": str(tmp_path), "TEXTS_PATH": str(tmp_path / "t.toml"), "MAP_TILES": "false",
                           "GAME_DATA_PATH": str(ROOT / "kneipenmonopoly_spieldaten.json")})
    g = Game(s, load_game_data(s.game_data_path), DB(s.db_path))
    bot = PhotoBot()
    g.notifier = TelegramNotifier(SimpleNamespace(bot=bot), g)
    asyncio.run(g.notifier.send_map(1, "Karte"))
    asyncio.run(g.notifier.send_map(2, "Karte"))
    assert bot.photos == [(1, "datei"), (2, "FILE123")]     # einmal hochladen, dann file_id
    asyncio.run(g.replace_spot("zucca", "zweibar", "anna"))  # Karte ändert sich -> neu hochladen
    asyncio.run(g.notifier.send_map(3, "Karte"))
    assert bot.photos[-1] == (3, "datei")


def test_keyboard_one_button_per_row():
    kb = keyboard([("A", "a"), ("B", "b")])
    assert [[b.text for b in row] for row in kb.inline_keyboard] == [["A"], ["B"]]
    assert keyboard(None) is None and keyboard([]) is None
