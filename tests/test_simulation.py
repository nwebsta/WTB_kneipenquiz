import asyncio
from pathlib import Path

from kneipenmonopoly.config import load_settings
from kneipenmonopoly.db import DB
from kneipenmonopoly.gamedata import load_game_data
from kneipenmonopoly.service import Game, NullNotifier
from kneipenmonopoly.simulation import Simulator, make_photo

ROOT = Path(__file__).resolve().parent.parent


def make_game(tmp_path):
    s = load_settings(env={"DATA_DIR": str(tmp_path), "TEXTS_PATH": str(tmp_path / "texte.toml"),
                           "GAME_DATA_PATH": str(ROOT / "kneipenmonopoly_spieldaten.json"),
                           "COOLDOWN_MIN": "0"})
    g = Game(s, load_game_data(s.game_data_path), DB(s.db_path), NullNotifier())
    for name, color, code, chat in [("A", "#ff0000", "AAA", 1), ("B", "#00aa00", "BBB", 2), ("C", "#0000ff", "CCC", 3)]:
        g.create_team(name, color, code)
        g.join(chat, name, code)
    g.add_action_card("Singt ein Lied")
    return g


def test_make_photo_is_valid_png(tmp_path):
    p = tmp_path / "x.png"
    make_photo(p, "#ff8800", 42, beers=4)
    data = p.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and b"IEND" in data[-12:]


def test_simulation_plays_and_stays_off_telegram(tmp_path):
    g = make_game(tmp_path)
    sim = Simulator(g)
    sim.tick, sim.speed_mps, sim.time_scale = 0.01, 5000, 0.0

    async def scenario():
        msg = await sim.start(send_to_groups=False, auto_approve=True)
        assert "3 Team" in msg and g.phase == "running"
        for _ in range(400):
            await asyncio.sleep(0.01)
            if g.db.one("SELECT COUNT(*) AS n FROM submissions WHERE status='approved'")["n"] >= 3:
                break
        await sim.stop()

    asyncio.run(scenario())
    st = g.state()
    assert sum(st.points.values()) > 0                      # es wurde gespielt
    assert g.db.one("SELECT COUNT(*) AS n FROM live_positions")["n"] == 3
    assert g.notifier.sent == []                            # nichts an Telegram
    sims = g.db.all("SELECT * FROM messages WHERE sender LIKE 'Bot (Simulation%'")
    assert sims                                             # Bot-Antworten stehen im Chatlog
    photos = g.db.all("SELECT photo_path FROM submissions")
    assert photos and all(Path(p["photo_path"]).exists() for p in photos)
    assert g.muted_teams == set() and not sim.running
    assert any("eingecheckt" in e[2] for e in sim.events)


def test_all_teams_start_central_and_pick_random_bars(tmp_path):
    from kneipenmonopoly.gamedata import distance_m
    g = make_game(tmp_path)
    sim = Simulator(g)
    sim.tick = 3600  # nach dem Start keinen Schritt machen

    async def scenario():
        await sim.start()
        clat, clon = sim.center()
        for st in sim.teams.values():
            assert distance_m(st.lat, st.lon, clat, clon) < 10
        targets = {sim._pick_target(next(iter(sim.teams.values()))) for _ in range(200)}
        assert len(targets) > 10                       # zufällig über viele Felder verteilt
        await sim.stop()

    asyncio.run(scenario())


def test_speed_presets(tmp_path):
    g = make_game(tmp_path)
    sim = Simulator(g)
    assert sim.speed == "normal" and sim.speed_mps == 12.0
    assert "Turbo" in sim.set_speed("turbo") and sim.speed_mps == 80.0 and sim.time_scale == 0.15
    assert sim.set_speed("gibtsnicht") == "Unbekanntes Tempo." and sim.speed == "turbo"

    async def scenario():
        await sim.start(speed="realistisch")
        assert sim.speed_mps == 1.4 and "Realistisch" in sim.speed_label
        await sim.stop()

    asyncio.run(scenario())


def test_simulation_can_send_to_groups(tmp_path):
    g = make_game(tmp_path)
    sim = Simulator(g)
    sim.tick, sim.speed_mps, sim.time_scale = 0.01, 5000, 0.0

    async def scenario():
        await sim.start(team_ids=[g.teams()[0]["id"]], send_to_groups=True)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if any("Eingecheckt" in t for _, t in g.notifier.sent):
                break
        await sim.stop()

    asyncio.run(scenario())
    assert any(c == 1 and "Eingecheckt" in t for c, t in g.notifier.sent)
    assert len(sim.teams) == 1
