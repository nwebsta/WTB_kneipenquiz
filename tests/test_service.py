import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kneipenmonopoly.config import load_settings
from kneipenmonopoly.db import DB
from kneipenmonopoly.gamedata import load_game_data
from kneipenmonopoly.service import Game, NullNotifier

ROOT = Path(__file__).resolve().parent.parent

EULE = (51.439342, 7.012116)
SAILOR = (51.432697, 7.001422)
FRITZ = (51.431095, 7.004159)
OST = (51.435411, 7.011973)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 13, 18, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def g(tmp_path):
    s = load_settings(env={"DATA_DIR": str(tmp_path), "TEXTS_PATH": str(tmp_path / "texte.toml"), "GAME_DATA_PATH": str(ROOT / "kneipenmonopoly_spieldaten.json")})
    data = load_game_data(s.game_data_path)
    clock = Clock()
    game = Game(s, data, DB(s.db_path), NullNotifier(), clock=clock)
    game.clock_obj = clock
    game.create_team("Alpha", "#f00", "AAA")
    game.create_team("Beta", "#0f0", "BBB")
    game.join(100, "Alpha-Chat", "aaa")
    game.join(200, "Beta-Chat", "BBB")
    return game


def tid(g, name):
    return next(t["id"] for t in g.teams() if t["name"] == name)


def visit(g, team, coords, admin="anna", task=False):
    r = g.handle_location(team, *coords)
    assert r.text and "Eingecheckt" in r.text, r.text
    res = g.submit_photo(team, None, "f", f"u{team}{coords}")
    assert res and res.ok
    sub = g.pending_submissions()[-1]
    return run(g.approve(sub["id"], admin, task))


def test_join_and_wrong_code(g):
    res, _ = g.join(300, "x", "nope")
    assert not res.ok
    res, _ = g.join(100, "x", "BBB")
    assert not res.ok and "schon" in res.message
    assert g.team_by_chat(100)["name"] == "Alpha"


def test_no_checkin_before_start(g):
    r = g.handle_location(tid(g, "Alpha"), *EULE)
    assert "noch nicht" in r.text


def test_start_sends_map_and_rules(g):
    run(g.start_game("anna"))
    sent = [t for c, t in g.notifier.sent if c == 100]
    assert any("[KARTE]" in t for t in sent)
    assert any("Regeln" in t for t in sent)


def test_checkin_out_of_radius_names_distance(g):
    run(g.start_game("anna"))
    r = g.handle_location(tid(g, "Alpha"), 51.4300, 7.0200)
    assert "m entfernt" in r.text


def test_multiple_spots_offer_choice(g):
    run(g.start_game("anna"))
    # zwischen Fritzpatrick's und Hudson's
    r = g.handle_location(tid(g, "Alpha"), 51.431011, 7.004484)
    assert r.choices and {sp.id for sp, _ in r.choices} >= {"fritzpatricks", "hudsons"}


def test_full_visit_flow_and_owner_points(g):
    run(g.start_game("anna"))
    a, b = tid(g, "Alpha"), tid(g, "Beta")
    assert visit(g, a, EULE).ok
    assert g.state().owners["die-eule"] == a
    assert g.state().points_of(a) == 1
    assert visit(g, b, EULE).ok
    assert g.state().points_of(a) == 2
    assert any(c == 100 and "war bei euch" in t for c, t in g.notifier.sent)


def test_own_bar_rejected(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    visit(g, a, EULE)
    g.clock_obj.advance(minutes=40)
    r = g.handle_location(a, *EULE)
    assert "gehört euch schon" in r.text


def test_cooldown_for_foreign_bar(g):
    run(g.start_game("anna"))
    a, b = tid(g, "Alpha"), tid(g, "Beta")
    visit(g, a, EULE)
    visit(g, b, EULE)
    g.clock_obj.advance(minutes=10)
    r = g.handle_location(b, *EULE)
    assert "gerade erst" in r.text
    g.clock_obj.advance(minutes=25)
    r = g.handle_location(b, *EULE)
    assert "Eingecheckt" in r.text


def test_photo_without_checkin_hint_rate_limited(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    assert g.submit_photo(a, None, "f", "u1") is not None
    assert g.submit_photo(a, None, "f", "u2") is None


def test_task_bonus_only_when_there_was_a_task(g):
    run(g.start_game("anna"))
    a, b = tid(g, "Alpha"), tid(g, "Beta")
    # keine Aufgabe: Haken wird ignoriert
    g.checkin(a, "die-eule", *EULE)
    g.submit_photo(a, None, "f", "ohne-aufgabe")
    run(g.approve(g.pending_submissions()[0]["id"], "anna", task_done=True))
    assert g.state().points_of(a) == 1
    assert not any("Aufgabe erfüllt" in t for _, t in g.notifier.sent)
    # mit Aufgabe: Bonus zählt
    g.set_task("sailors-pub", "Singt ein Lied")
    g.checkin(b, "sailors-pub", *SAILOR)
    g.submit_photo(b, None, "f", "mit-aufgabe")
    run(g.approve(g.pending_submissions()[0]["id"], "anna", task_done=True))
    assert g.state().points_of(b) == 2


def test_double_approval_prevented(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    g.handle_location(a, *EULE)
    g.submit_photo(a, None, "f", "u")
    sub = g.pending_submissions()[0]
    assert run(g.approve(sub["id"], "anna")).ok
    second = run(g.approve(sub["id"], "bernd"))
    assert not second.ok and "anna" in second.message
    assert g.state().points_of(a) == 1


def test_older_submission_blocks_newer_same_spot(g):
    run(g.start_game("anna"))
    a, b = tid(g, "Alpha"), tid(g, "Beta")
    g.handle_location(a, *EULE)
    g.submit_photo(a, None, "f", "ua")
    g.clock_obj.advance(seconds=5)
    g.handle_location(b, *EULE)
    g.submit_photo(b, None, "f", "ub")
    older, newer = g.pending_submissions()
    assert g.blocked_by(newer)["id"] == older["id"]
    assert not run(g.approve(newer["id"], "anna")).ok
    assert run(g.approve(older["id"], "anna")).ok
    assert run(g.approve(newer["id"], "anna")).ok
    st = g.state()
    assert st.owners["die-eule"] == a and st.points_of(a) == 2


def test_reject_reopens_checkin(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    g.handle_location(a, *EULE)
    g.submit_photo(a, None, "f", "u")
    sub = g.pending_submissions()[0]
    assert run(g.reject(sub["id"], "anna", "Bar nicht erkennbar")).ok
    assert any("abgelehnt" in t and "Bar nicht erkennbar" in t for c, t in g.notifier.sent)
    res = g.submit_photo(a, None, "f", "u2")
    assert res.ok


def test_checkin_timeout_expires(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    g.handle_location(a, *EULE)
    g.clock_obj.advance(minutes=21)
    run(g.sweep())
    assert g.open_checkin(a) is None
    assert any("abgelaufen" in t for c, t in g.notifier.sent)


def test_pause_extends_timeout(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    g.handle_location(a, *EULE)
    g.clock_obj.advance(minutes=10)
    run(g.pause_game("anna"))
    assert "Pause" in g.handle_location(tid(g, "Beta"), *EULE).text
    g.clock_obj.advance(minutes=30)
    run(g.sweep())
    run(g.resume_game("anna"))
    g.clock_obj.advance(minutes=5)
    run(g.sweep())
    assert g.open_checkin(a) is not None


def test_revoke_approval(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    visit(g, a, EULE)
    ev = [e for e in g.db.events() if e.type == "VISIT_APPROVED"][0]
    assert run(g.revoke(ev.id, "anna", "falsche Bar")).ok
    assert "die-eule" not in g.state().owners
    assert g.state().points_of(a) == 0
    assert not run(g.revoke(ev.id, "anna")).ok
    sub = g.db.one("SELECT status FROM submissions")
    assert sub["status"] == "revoked"


def test_station_flow(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    assert visit(g, a, OST).ok
    assert g.state().owners["bahnhof-ost"] == a
    assert g.state().points_of(a) == 1


def test_replace_field_moves_checkin_location(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    run(g.replace_spot("die-eule", "stoffwechsel", "anna"))
    stoff = (51.437358, 7.007849)
    r = g.handle_location(a, *stoff)
    assert "Stoffwechsel" in r.text
    r2 = g.handle_location(a, *EULE)
    assert "Eingecheckt" not in (r2.text or "")


def test_end_game_and_final_ranking(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    visit(g, a, EULE)
    run(g.end_game("anna"))
    assert any("Endstand" in t and "Alpha" in t for c, t in g.notifier.sent)
    assert "vorbei" in g.handle_location(a, *SAILOR).text


def test_game_duration_auto_end(g):
    assert g.set_duration("3:00").ok and g.duration_min() == 180
    assert not g.set_duration("abc").ok
    assert "3 Stunden" in g.rules_text()                       # vor dem Start: nur Dauer
    run(g.start_game("anna"))
    assert any("<b>3 Stunden</b> Spielzeit" in t for _, t in g.notifier.sent)   # Startnachricht
    assert g.end_time() == g.clock_obj.now + timedelta(minutes=180)
    g.clock_obj.advance(minutes=155)
    run(g.sweep())
    assert any("30 Minuten" in t for _, t in g.notifier.sent)
    assert "Noch <b>25 Min</b> Spielzeit" in g.stand_text(tid(g, "Alpha"))
    g.clock_obj.advance(minutes=26)
    run(g.sweep())
    assert g.phase == "ended"


def test_pause_stops_game_clock_and_event_cards(g):
    g.set_duration("1:00")
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    g.clock_obj.advance(minutes=40)
    run(g.pause_game("anna"))
    g.clock_obj.advance(minutes=30)                               # Pause zählt nicht
    run(g.sweep())
    assert g.phase == "paused"
    assert g.game_elapsed() == timedelta(minutes=40)
    run(g.resume_game("anna"))
    # Ereigniskarte bei 0:45 Spielzeit, also erst 5 Minuten nach der Pause fällig
    assert "0,5" not in g.checkin(a, "die-eule", *EULE).message
    g.clock_obj.advance(minutes=6)
    assert "0,5" in g.checkin(a, "sailors-pub", *SAILOR).message
    assert g.end_time() == g.clock_obj.now + timedelta(minutes=14)
    g.clock_obj.advance(minutes=15)
    run(g.sweep())
    assert g.phase == "ended"


def test_no_duration_means_no_auto_end(g):
    g.set_duration("")
    run(g.start_game("anna"))
    g.clock_obj.advance(hours=10)
    run(g.sweep())
    assert g.phase == "running" and g.end_time() is None


def test_manual_correction_and_export(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    assert not run(g.adjust_points(a, 3, "anna", "")).ok
    assert run(g.adjust_points(a, 3, "anna", "Gesangseinlage")).ok
    assert g.state().points_of(a) == 3
    assert "Gesangseinlage" in g.export_events_csv()
    assert '"points": 3' in g.export_json()


def test_test_mode_checkin_by_number(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    assert not g.test_checkin(a, "15").ok          # Testmodus aus
    g.set_test_mode(True)
    assert g.resolve_spot("15") == "die-eule"
    assert g.resolve_spot("n") == "bahnhof-nord"
    assert g.resolve_spot("Zucca") == "zucca"
    assert g.resolve_spot("99") is None
    assert not g.test_checkin(a, "99").ok
    r = g.test_checkin(a, "15")
    assert r.ok and "Testmodus" in r.message and "Die Eule" in r.message
    assert g.open_checkin(a)["is_test"] == 1
    assert g.submit_photo(a, None, "f", "t1").ok
    sub = g.pending_submissions()[0]
    assert sub["is_test"] == 1
    assert run(g.approve(sub["id"], "anna")).ok
    assert g.state().owners["die-eule"] == a
    # Regeln gelten weiter: eigene Bar wird abgelehnt
    assert not g.test_checkin(a, "15").ok
    assert g.test_checkin(a, "O").ok
    assert "/checkin" in g.help_text() and "/checkin" in g.felder_text()


def test_reset_keeps_teams_and_tasks(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    g.set_task("die-eule", "Singen")
    visit(g, a, EULE)
    assert g.reset_game().ok
    assert g.phase == "setup"
    assert g.state().points == {} and g.state().owners == {}
    assert g.pending_count() == 0 and g.messages() == []
    assert g.team_by_chat(100)["name"] == "Alpha"
    assert g.tasks()["die-eule"] == "Singen"


def test_owned_bar_is_normal_visit_without_buttons(g):
    run(g.start_game("anna"))
    a, b = tid(g, "Alpha"), tid(g, "Beta")
    visit(g, a, EULE)
    r = g.checkin(b, "die-eule", *EULE)
    assert r.ok and not r.buttons
    assert "Alpha" not in r.message and "Gehört" not in r.message      # Besitz bleibt beim Check-in geheim
    g.clock_obj.advance(minutes=1)
    rs = g.checkin(b, "bahnhof-ost", *OST)
    assert rs.ok
    g.t.user["checkin.besetzt_bar"] = "Gehört {team}"                  # in texte.toml einschaltbar
    visit(g, a, SAILOR)
    assert "Gehört Alpha" in g.checkin(b, "sailors-pub", *SAILOR).message
    g.submit_photo(b, None, "f", "rent")
    run(g.approve(g.pending_submissions()[0]["id"], "anna"))
    assert g.state().points_of(a) == 3        # Eule + Sailor's gekauft + Miete von Beta


def test_free_bar_alone_has_no_buttons(g):
    run(g.start_game("anna"))
    r = g.checkin(tid(g, "Alpha"), "die-eule", *EULE)
    assert r.ok and not r.buttons and "allen Bieren" in r.message
    assert "frei" not in r.message                       # ohne anderes Team: Status bleibt offen
    rs = g.checkin(tid(g, "Beta"), "bahnhof-ost", *OST)
    assert rs.ok and "frei" not in rs.message


def test_free_text_can_be_switched_on(g):
    g.t.user["checkin.frei_bar"] = "🆓 Frei! +{punkte}"
    run(g.start_game("anna"))
    assert "🆓 Frei! +1" in g.checkin(tid(g, "Alpha"), "die-eule", *EULE).message


def test_free_bar_race_first_upload_wins(g):
    run(g.start_game("anna"))
    a, b = tid(g, "Alpha"), tid(g, "Beta")
    g.checkin(a, "die-eule", *EULE)
    r = g.checkin(b, "die-eule", *EULE)
    assert "Alpha" in r.message and "Wettrennen" in r.message
    assert [d.split("|")[2] for _, d in r.buttons] == ["shot"]
    # Beta ist schneller mit dem Foto
    g.submit_photo(b, None, "f", "beta-first")
    g.clock_obj.advance(seconds=30)
    g.submit_photo(a, None, "f", "alpha-second")
    first, second = g.pending_submissions()
    assert first["team_id"] == b
    assert g.blocked_by(second)["id"] == first["id"]
    run(g.approve(first["id"], "anna"))
    run(g.approve(second["id"], "anna"))
    st = g.state()
    assert st.owners["die-eule"] == b
    assert st.points_of(b) == 2 and st.points_of(a) == 0   # Alphas Foto zählt als Besuch (Miete)


def test_station_race_detected(g):
    run(g.start_game("anna"))
    g.set_test_mode(True)
    a, b = tid(g, "Alpha"), tid(g, "Beta")
    assert g.test_checkin(b, "S").ok
    r = g.test_checkin(a, "S")
    assert "Beta" in r.message and "Wettrennen" in r.message and "den Bahnhof" in r.message
    assert "Kiosk erkennbar" in r.message
    assert [d.split("|")[2] for _, d in r.buttons] == ["shot"]


def test_shot_and_go_closes_checkin_without_points(g):
    run(g.start_game("anna"))
    a, b = tid(g, "Alpha"), tid(g, "Beta")
    g.checkin(a, "die-eule", *EULE)
    g.checkin(b, "die-eule", *EULE)
    ci = g.open_checkin(b)
    res = run(g.choose_mode(b, ci["id"], "shot"))
    assert res.ok and "Kurzen" in res.message
    assert g.open_checkin(b) is None
    assert not run(g.choose_mode(b, ci["id"], "shot")).ok
    assert g.submit_photo(b, None, "f", "nach-kurzem").ok is False   # kein offener Check-in mehr
    assert g.state().points_of(b) == 0


def test_shot_and_go_returns_event_card(g):
    run(g.start_game("anna"))
    a, b = tid(g, "Alpha"), tid(g, "Beta")
    g.clock_obj.advance(minutes=46)
    g.checkin(a, "die-eule", *EULE)
    r = g.checkin(b, "die-eule", *EULE)
    assert "0,5" in r.message
    run(g.choose_mode(b, g.open_checkin(b)["id"], "shot"))
    assert "0,5" in g.checkin(b, "sailors-pub", *SAILOR).message


def test_action_cards(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    assert "noch nicht" in run(g.request_action(a)).message
    g.add_action_card("Singt ein Lied")
    g.add_action_card("Macht eine Polonaise")
    g.checkin(a, "die-eule", *EULE)
    r = run(g.request_action(a))
    assert r.ok and "Videoanruf" in r.message and "Die Eule" in r.message
    assert "offene Anfrage" in run(g.request_action(a)).message
    call = g.open_calls()[0]
    assert call["kind"] == "action" and call["spot_id"] == "die-eule"
    assert g.call_preview(call, True)[0].delta == 1
    run(g.decide_call(call["id"], "anna", False))
    assert g.state().points_of(a) == -1
    r2 = run(g.request_action(a))
    assert r2.ok
    run(g.cancel_call(g.open_calls()[0]["id"], "anna"))
    assert len(g.remaining_action_cards(a)) == 1          # abgebrochene Karte zurück
    run(g.request_action(a))
    run(g.decide_call(g.open_calls()[0]["id"], "anna", True))
    assert "alle Aktionskarten" in run(g.request_action(a)).message
    assert g.state().points_of(a) == 0


def test_action_button_on_checkin_and_rules(g):
    a = tid(g, "Alpha")
    g.add_action_card("Tanzen")
    run(g.start_game("anna"))
    assert g.notifier.buttons == []                        # kein Button in Regeln/Start: nicht in einer Kneipe
    assert g.action_buttons(a) == []
    r = g.checkin(a, "die-eule", *EULE)
    assert ("🎬 Aktionskarte ziehen", "aktion") in r.buttons
    run(g.request_action(a))
    assert g.action_buttons(a) == []                       # Anruf offen
    run(g.decide_call(g.open_calls()[0]["id"], "anna", True))
    assert g.action_buttons(a) == []                       # keine Karte mehr übrig
    assert "/aktion" not in g.rules_text() and "/aktion" not in g.help_text()


def test_race_hides_action_button_and_action_does_not_block_race(g):
    a, b = tid(g, "Alpha"), tid(g, "Beta")
    g.add_action_card("Tanzen")
    g.add_action_card("Singen")
    run(g.start_game("anna"))
    # Alpha ist zuerst in der freien Bar und startet eine Aktion
    assert ("🎬 Aktionskarte ziehen", "aktion") in g.checkin(a, "die-eule", *EULE).buttons
    run(g.request_action(a))
    # Beta kommt dazu: Wettrennen, nur „Kurzen & gehen“, keine Aktionskarte
    r = g.checkin(b, "die-eule", *EULE)
    assert "Wettrennen" in r.message
    assert [d for _, d in r.buttons] == [f"mode|{g.open_checkin(b)['id']}|shot"]
    # Beta lädt zuerst hoch, Alpha danach (mitten in der Aktion)
    g.submit_photo(b, None, "f", "beta")
    g.clock_obj.advance(seconds=20)
    assert g.submit_photo(a, None, "f", "alpha").ok
    run(g.decide_call(g.open_calls()[0]["id"], "anna", True))     # Aktionspunkt für Alpha
    for s in g.pending_submissions():
        run(g.approve(s["id"], "anna"))
    st = g.state()
    assert st.owners["die-eule"] == b                               # Bar an Beta
    assert st.points_of(a) == 1                                     # nur die Aktion
    assert st.points_of(b) == 1 + 1                                 # Kauf + Miete von Alpha


def test_live_location_recorded_for_review_only(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    # Live-Standort direkt an der Eule
    g.update_live(a, *EULE)
    r = g.checkin(a, "die-eule", *EULE)
    assert "Live" not in r.message                                # Spieler sehen nichts davon
    assert g.open_checkin(a)["live_distance_m"] == 0
    # Team läuft 300 m weg und lädt dann das Foto hoch
    g.clock_obj.advance(minutes=2)
    g.update_live(a, 51.4366, 7.0121)
    g.clock_obj.advance(seconds=30)
    assert g.submit_photo(a, None, "f", "live").ok
    sub = g.pending_submissions()[0]
    assert sub["checkin_live_distance_m"] == 0
    assert sub["live_distance_m"] > 250 and sub["live_age_s"] == 30
    snap = g.live_snapshot(a, g.spot("die-eule"))
    assert snap["distance_m"] > 250
    # nur die letzte Position wird gespeichert
    assert g.db.one("SELECT COUNT(*) AS n FROM live_positions")["n"] == 1
    g.purge_private_data()
    assert g.db.one("SELECT COUNT(*) AS n FROM live_positions")["n"] == 0
    assert g.db.one("SELECT live_distance_m FROM submissions")["live_distance_m"] is None


def test_no_live_location_is_fine(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    g.checkin(a, "die-eule", *EULE)
    g.submit_photo(a, None, "f", "nolive")
    sub = g.pending_submissions()[0]
    assert sub["live_distance_m"] is None and sub["checkin_live_distance_m"] is None


def test_chat_migration_updates_team(g):
    g.migrate_chat(100, -100123)
    assert g.team_by_chat(-100123)["name"] == "Alpha"
    assert g.team_by_chat(100) is None


def test_event_card_on_next_bar_checkin_once_per_team(g):
    run(g.start_game("anna"))
    a, b = tid(g, "Alpha"), tid(g, "Beta")
    r = g.checkin(a, "die-eule", *EULE)
    assert "Ereigniskarte" not in r.message
    g.clock_obj.advance(minutes=46)
    # Bahnhof verbraucht die Karte nicht
    assert "Ereigniskarte" not in g.checkin(a, "bahnhof-ost", *OST).message
    r = g.checkin(a, "die-eule", *EULE)
    assert "0,5 statt 0,33" in r.message
    assert g.open_checkin(a)["event_text"].startswith("0,5")
    g.submit_photo(a, None, "f", "ev")
    assert "0,5" in g.pending_submissions()[0]["event_text"]
    run(g.approve(g.pending_submissions()[0]["id"], "anna"))
    assert "Ereigniskarte" not in g.checkin(a, "sailors-pub", *SAILOR).message
    assert "0,5 statt 0,33" in g.checkin(b, "sailors-pub", *SAILOR).message
    g.clock_obj.advance(minutes=45)
    assert "Shot statt 0,33" in g.checkin(a, "sailors-pub", *SAILOR).message


def test_expired_checkin_returns_event_card(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    g.clock_obj.advance(minutes=46)
    assert "0,5" in g.checkin(a, "die-eule", *EULE).message
    g.clock_obj.advance(minutes=21)
    run(g.sweep())
    assert "0,5" in g.checkin(a, "sailors-pub", *SAILOR).message


def test_double_points_card_fires_and_counts_by_upload_time(g):
    run(g.start_game("anna"))
    a, b = tid(g, "Alpha"), tid(g, "Beta")
    g.clock_obj.advance(minutes=149)
    g.checkin(a, "die-eule", *EULE)
    g.submit_photo(a, None, "f", "before")          # vor 2:30 hochgeladen
    g.clock_obj.advance(minutes=2)
    run(g.sweep())
    assert any("doppelt" in t for _, t in g.notifier.sent)
    assert g.state().multiplier == 2
    g.checkin(b, "sailors-pub", *SAILOR)
    g.submit_photo(b, None, "f", "after")           # nach 2:30
    for s in g.pending_submissions():
        run(g.approve(s["id"], "anna"))
    st = g.state()
    assert st.points_of(a) == 1 and st.points_of(b) == 2
    run(g.sweep())
    assert sum("doppelt" in t for _, t in g.notifier.sent) == 2   # je Team einmal, nicht erneut


def test_action_card_only_in_a_bar(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    g.add_action_card("Tanzen")
    g.add_action_card("Singen")
    assert "direkt nach dem Check-in" in run(g.request_action(a)).message  # nicht eingecheckt
    g.checkin(a, "bahnhof-ost", *OST)
    assert g.action_buttons(a) == []                                      # Bahnhof zählt nicht
    assert not run(g.request_action(a)).ok
    r = g.checkin(a, "die-eule", *EULE)
    assert ("🎬 Aktionskarte ziehen", "aktion") in r.buttons
    g.clock_obj.advance(minutes=10)                                       # noch kein Foto: erlaubt
    assert g.action_buttons(a)
    g.submit_photo(a, None, "f", "foto")                                  # Foto eingereicht: vorbei
    assert g.action_buttons(a) == []
    assert "bevor ihr das Foto schickt" in run(g.request_action(a)).message
    g.checkin(a, "sailors-pub", *SAILOR)                                  # neuer Kneipen-Check-in
    g.clock_obj.advance(minutes=21)                                       # Check-in-Frist abgelaufen
    assert g.action_buttons(a) == [] and not run(g.request_action(a)).ok
    g.checkin(a, "sailors-pub", *SAILOR)
    assert run(g.request_action(a)).ok
    assert "bei /stand" not in g.rules_text() and "Nicht im Wettrennen" in g.rules_text()


def test_no_action_card_in_race(g):
    run(g.start_game("anna"))
    a, b = tid(g, "Alpha"), tid(g, "Beta")
    g.add_action_card("Tanzen")
    r_a = g.checkin(a, "die-eule", *EULE)
    assert ("🎬 Aktionskarte ziehen", "aktion") in r_a.buttons            # allein in der freien Bar
    r_b = g.checkin(b, "die-eule", *EULE)                                 # Beta kommt dazu: Wettrennen
    assert all(d != "aktion" for _, d in r_b.buttons)
    assert g.action_buttons(a) == [] and g.action_buttons(b) == []        # auch der alte Button von Alpha
    assert "Wettrennen" in run(g.request_action(a)).message
    assert "Wettrennen" in run(g.request_action(b)).message
    # Bar gehört schon jemandem: kein Wettrennen, Aktion erlaubt
    visit(g, b, SAILOR)
    g.clock_obj.advance(minutes=1)
    g.checkin(a, "sailors-pub", *SAILOR)
    assert run(g.request_action(a)).ok


def test_hidden_scores_hide_owners(g):
    run(g.start_game("anna"))
    a, b = tid(g, "Alpha"), tid(g, "Beta")
    assert g.scores_hidden()                                              # Standard: verdeckt
    visit(g, a, EULE)
    felder = g.felder_text()
    assert "Alpha" not in felder and "geheim" in felder and "Die Eule" in felder and "frei" not in felder
    stand = g.stand_text(a)
    assert "Eure Punkte: <b>1</b>" in stand and "Beta" not in stand
    g.notifier.sent.clear()
    visit(g, b, EULE)
    to_beta = [t for c, t in g.notifier.sent if c == 200]
    to_alpha = [t for c, t in g.notifier.sent if c == 100]
    assert any("einem anderen Team" in t for t in to_beta) and not any("Alpha" in t for t in to_beta)
    assert any("Beta" in t and "war bei euch" in t for t in to_alpha)     # Besitzer erfährt den Besuch
    run(g.end_game("anna"))
    assert "Alpha" in g.felder_text()                                     # nach Spielende sichtbar


def test_hidden_scores_no_street_broadcast(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    visit(g, a, (51.430134, 7.004468))      # Früher oder Später (Braun)
    g.notifier.sent.clear()
    visit(g, a, (51.429656, 7.003469))      # Mimaya -> Braun komplett
    assert any(c == 100 and "komplett" in t for c, t in g.notifier.sent)
    assert not any(c == 200 for c, _ in g.notifier.sent)                  # Beta erfährt nichts


def test_open_scores_still_show_owners(g):
    g.set_scores_hidden(False)
    run(g.start_game("anna"))
    visit(g, tid(g, "Alpha"), EULE)
    assert "Alpha" in g.felder_text()


def test_final_waits_for_open_calls(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    g.add_action_card("Tanzen")
    g.checkin(a, "die-eule", *EULE)
    assert run(g.request_action(a)).ok
    run(g.end_game("anna"))
    assert not any("🏆 <b>Endstand" in t for _, t in g.notifier.sent)
    run(g.decide_call(g.open_calls()[0]["id"], "anna", True))
    assert any("🏆 <b>Endstand" in t for _, t in g.notifier.sent)


def test_reset_clears_calls_and_cards_fire_again(g):
    run(g.start_game("anna"))
    g.add_action_card("Tanzen")
    g.checkin(tid(g, "Alpha"), "die-eule", *EULE)
    assert run(g.request_action(tid(g, "Alpha"))).ok
    assert g.open_calls_count() == 1
    g.clock_obj.advance(minutes=151)
    run(g.sweep())
    g.reset_game()
    assert g.open_calls_count() == 0
    assert not any(g.card_fired(c) for c in g.event_cards())
    assert len(g.action_cards()) == 1 and len(g.event_cards()) == 3


def test_purge(g):
    run(g.start_game("anna"))
    a = tid(g, "Alpha")
    g.log_message(team_id=a, chat_id=100, direction="in", kind="text", text="hallo")
    g.handle_location(a, *EULE)
    g.purge_private_data()
    assert g.messages() == []
    assert g.db.one("SELECT lat FROM checkins")["lat"] is None
