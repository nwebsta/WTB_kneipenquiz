import os
import re
import time
from pathlib import Path

from kneipenmonopoly.texts import DEFAULT_PATH, Raw, Texts, placeholders

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "kneipenmonopoly"

# Schlüssel, die im Code per f-String zusammengesetzt werden
DYNAMIC_KEYS = [
    "checkin.eingecheckt_bar", "checkin.eingecheckt_bar_test", "checkin.eingecheckt_bahnhof",
    "checkin.eingecheckt_bahnhof_test", "checkin.wettrennen_bar", "checkin.wettrennen_bahnhof",
    "checkin.foto_bar", "checkin.foto_bahnhof", "spiel.noch_vorbereitung", "spiel.gerade_pause", "spiel.vorbei",
    "foto.bar_gehoert_euch", "foto.bahnhof_gehoert_euch", "foto.vorbeischauen", "foto.aufgabe_erfuellt",
    "aktion.geschafft", "aktion.nicht_geschafft", "stand.zeile", "stand.zeile_ich",
    "checkin.besetzt_bar", "checkin.besetzt_bar_strasse", "foto.an_besitzer", "foto.an_besitzer_strasse",
]


def used_keys() -> set[str]:
    keys = set()
    for py in PKG.rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        keys |= set(re.findall(r'(?:\bT|\.t)\(\s*"([a-z_]+\.[a-z0-9_]+)"', src))
        keys |= set(re.findall(r'"([a-z_]+\.[a-z0-9_]+)"\s+if\b', src))
        keys |= set(re.findall(r'\belse\s+"([a-z_]+\.[a-z0-9_]+)"', src))
    return keys


def test_every_used_key_exists_in_defaults():
    t = Texts(None)
    missing = sorted(k for k in used_keys() | set(DYNAMIC_KEYS) if k not in t.defaults)
    assert missing == []


def test_all_defaults_format_cleanly():
    t = Texts(None)
    for key, tpl in t.defaults.items():
        dummy = {p: "X" for p in placeholders(tpl)}
        out = tpl.format(**dummy)
        assert "{" not in out, key


def test_user_file_overrides_and_escapes(tmp_path):
    user = tmp_path / "texte.toml"
    t = Texts(user)
    assert user.exists()                                  # wird als Kopie angelegt
    assert "Willkommen" in t("join.willkommen", team="A")
    user.write_text('[join]\nwillkommen = "Hallo {team}!"\n', encoding="utf-8")
    os.utime(user, (time.time() + 5, time.time() + 5))    # sicher neue mtime
    assert t("join.willkommen", team="Bier & <Brezel>") == "Hallo Bier &amp; &lt;Brezel&gt;!"
    assert t("join.willkommen", team=Raw("<b>A</b>")) == "Hallo <b>A</b>!"
    assert "Den Code kenne ich nicht" in t("join.code_unbekannt")   # nicht überschrieben -> Standard
    assert t.error is None


def test_broken_file_keeps_working(tmp_path):
    user = tmp_path / "texte.toml"
    user.write_text('[join]\nwillkommen = "kaputt\n', encoding="utf-8")
    t = Texts(user)
    assert t.error and "texte.toml" in t.error
    assert "Willkommen" in t("join.willkommen", team="A")


def test_unknown_placeholder_and_bad_braces_fall_back(tmp_path):
    user = tmp_path / "texte.toml"
    user.write_text('[join]\nwillkommen = "Hi {gibtsnicht}"\ncode_unbekannt = "kaputt {"\n', encoding="utf-8")
    t = Texts(user)
    assert "gibtsnicht" in t.error
    assert t("join.willkommen", team="A") == "Hi {gibtsnicht}"        # bleibt sichtbar statt Absturz
    assert "Den Code kenne ich nicht" in t("join.code_unbekannt")     # unlesbar -> Standard


def test_free_text_switched_on_is_no_error(tmp_path):
    user = tmp_path / "texte.toml"
    user.write_text('[checkin]\nfrei_bar = "🆓 Frei! +{punkte}"\n', encoding="utf-8")
    t = Texts(user)
    assert t.error is None
    assert t("checkin.frei_bar", punkte=1) == "🆓 Frei! +1"
    assert Texts(None)("checkin.frei_bar", punkte=1) == ""      # Standard: leer


def test_every_placeholder_is_explained():
    from kneipenmonopoly.texts import ALLOWED_PLACEHOLDERS, PLACEHOLDER_INFO
    t = Texts(None)
    used = set().union(*(placeholders(v) for v in t.defaults.values()), *ALLOWED_PLACEHOLDERS.values())
    assert sorted(used - set(PLACEHOLDER_INFO)) == []


def test_all_defaults_are_valid_telegram_html():
    t = Texts(None)
    for key, tpl in t.defaults.items():
        assert t.validate(key, tpl) is None, key


def test_entries_have_comments_and_placeholders(tmp_path):
    t = Texts(tmp_path / "texte.toml")
    entries = {e["key"]: e for e in t.entries()}
    assert set(entries) == set(t.defaults)
    e = entries["checkin.kein_feld_hier"]
    assert "{bar}" in e["comment"] and e["placeholders"] == ["bar", "meter"] and not e["changed"]
    assert entries["checkin.frei_bar"]["placeholders"] == ["punkte"]       # leerer Standard, erlaubte Platzhalter


def test_save_reset_and_file_keeps_comments(tmp_path):
    import tomllib
    user = tmp_path / "texte.toml"
    t = Texts(user)
    assert t.save("checkin.feld_zu_weit", 'Zu weit: {bar} ({meter} m) – "sorry" \\o/') is None
    assert t("checkin.feld_zu_weit", bar="Eule", meter=80) == 'Zu weit: Eule (80 m) – "sorry" \\o/'
    multi = "\nZeile 1 mit '''drei'''\nZeile 2 {uhrzeit}"
    assert t.save("regeln.endzeit", multi + "\r\n") is None
    assert t.current("regeln.endzeit").rstrip("\n") == multi
    text = user.read_text(encoding="utf-8")
    assert "# {bar}, {meter}" in text and "[checkin]" in text                 # Kommentare bleiben
    tomllib.loads(text)                                                        # gültiges TOML
    assert next(e for e in t.entries() if e["key"] == "checkin.feld_zu_weit")["changed"]
    assert t.reset("checkin.feld_zu_weit") is None
    assert t.current("checkin.feld_zu_weit") == t.defaults["checkin.feld_zu_weit"]
    assert t.current("regeln.endzeit").rstrip("\n") == multi                  # andere Änderung bleibt


def test_save_rejects_broken_texts(tmp_path):
    t = Texts(tmp_path / "texte.toml")
    assert "Unbekannte Platzhalter" in t.save("checkin.feld_zu_weit", "{bar} {gibtsnicht}")
    assert "Klammern" in t.save("checkin.feld_zu_weit", "kaputt {")
    assert "&" in t.save("checkin.feld_zu_weit", "Bier & Brezel")
    assert "nicht geschlossen" in t.save("checkin.feld_zu_weit", "<b>fett")
    assert "<" in t.save("checkin.feld_zu_weit", "1 < 2")
    assert t.save("checkin.feld_zu_weit", "Bier &amp; Brezel {{echt}}") is None
    assert t.save("gibts.nicht", "x") is not None


def test_texts_page_and_api(tmp_path):
    import asyncio
    from fastapi.testclient import TestClient
    from kneipenmonopoly.config import load_settings
    from kneipenmonopoly.db import DB
    from kneipenmonopoly.gamedata import load_game_data
    from kneipenmonopoly.service import Game, NullNotifier
    from kneipenmonopoly.web.app import create_app
    s = load_settings(env={"DATA_DIR": str(tmp_path), "TEXTS_PATH": str(tmp_path / "texte.toml"),
                           "ADMIN_PASSWORD": "pw", "SESSION_SECRET": "x" * 32, "MAP_TILES": "false",
                           "GAME_DATA_PATH": str(ROOT / "kneipenmonopoly_spieldaten.json")})
    g = Game(s, load_game_data(s.game_data_path), DB(s.db_path), NullNotifier())
    with TestClient(create_app(s, game=g, run_bot=False)) as client:
        client.post("/login", data={"name": "Anna", "password": "pw"})
        page = client.get("/texte").text
        assert "checkin.feld_zu_weit" in page and "Entfernung in Metern" in page and "tx-examples" in page
        r = client.post("/texte/api/speichern", json={"key": "spiel.start", "value": "🚦 <b>Los!</b>"})
        assert r.json()["ok"] and r.json()["entry"]["changed"]
        assert g.t("spiel.start") == "🚦 <b>Los!</b>"                         # Bot nutzt es sofort
        bad = client.post("/texte/api/speichern", json={"key": "spiel.start", "value": "<b>offen"})
        assert bad.status_code == 422 and not bad.json()["ok"]
        r = client.post("/texte/api/speichern", json={"key": "spiel.start", "reset": True})
        assert r.json()["ok"] and not r.json()["entry"]["changed"]
        assert client.get("/texte/datei").status_code == 200


def test_shipped_texte_toml_matches_defaults():
    """Die mitgelieferte texte.toml im Projekt enthält alle Standardtexte."""
    shipped = ROOT / "texte.toml"
    if shipped.exists():
        t = Texts(shipped)
        assert t.error is None
        assert set(t.defaults) <= set(t.user) | set(t.defaults)
    assert DEFAULT_PATH.exists()
