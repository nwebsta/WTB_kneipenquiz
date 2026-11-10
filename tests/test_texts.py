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


def test_shipped_texte_toml_matches_defaults():
    """Die mitgelieferte texte.toml im Projekt enthält alle Standardtexte."""
    shipped = ROOT / "texte.toml"
    if shipped.exists():
        t = Texts(shipped)
        assert t.error is None
        assert set(t.defaults) <= set(t.user) | set(t.defaults)
    assert DEFAULT_PATH.exists()
