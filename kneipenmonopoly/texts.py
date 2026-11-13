"""Alle Spielertexte aus einer TOML-Datei, mit Platzhaltern.

Die Standardtexte liegen in `texte_standard.toml` im Paket. Die bearbeitbare
Datei (Standard: `texte.toml` im Projektordner) überschreibt sie Schlüssel für
Schlüssel. Fehlt sie, wird sie beim Start als Kopie der Standardtexte angelegt.
Änderungen werden ohne Neustart übernommen. Ist die Datei kaputt, gelten die
zuletzt gültigen Texte weiter, und `error` beschreibt das Problem.
"""
from __future__ import annotations

import html
import logging
import re
import shutil
import tomllib
from pathlib import Path
from string import Formatter

log = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent / "texte_standard.toml"

# Texte, deren Standard leer ist, die aber trotzdem Platzhalter haben dürfen
ALLOWED_PLACEHOLDERS = {
    "checkin.frei_bar": {"punkte"},
    "checkin.frei_bahnhof": {"punkte", "nummer"},
    "checkin.besetzt_bar": {"team", "punkte"},
    "checkin.besetzt_bar_strasse": {"team", "punkte"},
    "checkin.besetzt_bahnhof": {"team", "punkte", "anzahl"},
    "checkin.besucher_punkte": {"punkte"},
}


# Texte, die Telegram als reinen Text zeigt (Buttons, kurze Pop-ups, Bildunterschrift):
# keine HTML-Formatierung, Werte werden nicht escaped.
PLAIN_KEYS = {
    "allgemein.button_kurzen", "allgemein.button_aktion", "allgemein.nicht_zugeordnet_kurz",
    "allgemein.auswahl_ungueltig", "allgemein.karte_beschriftung",
    "checkin.auswahl_button", "checkin.auswahl_zu_alt",
}

# Was hinter den Platzhaltern steckt: (Beschreibung, Beispielwert für die Vorschau).
# Beispielwerte mit HTML sind fertige Telegram-Bausteine, die der Bot selbst zusammensetzt.
PLACEHOLDER_INFO: dict[str, tuple[str, str]] = {
    "team": ("Name eines Teams (je nach Text: eigenes, besuchendes oder besitzendes Team)", "Die Durstigen"),
    "teams": ("Mehrere Teamnamen, fett und mit „und“ verbunden", "<b>Die Durstigen</b> und <b>Bierdeckel</b>"),
    "bar": ("Name der Kneipe/Bar bzw. des Bahnhofs", "Die Eule"),
    "bahnhof": ("Name des Bahnhofs", "Nordbahnhof"),
    "feld": ("Name eines eigenen Feldes", "Zucca"),
    "strasse": ("Name der Straße (Farbgruppe)", "Gelb"),
    "adresse": ("Adresse der neuen Bar", "Brigittastr. 12"),
    "alt": ("Vorheriger Name bzw. Punktestand", "Zucca"),
    "neu": ("Neuer Name bzw. Punktestand", "Zweibar"),
    "meter": ("Entfernung in Metern", "23"),
    "minuten": ("Minuten bis es wieder geht", "12"),
    "punkte": ("Punkte (Zahl, bei manchen Texten mit Vorzeichen)", "2"),
    "nummer": ("Feldnummer bzw. der wievielte Bahnhof", "15"),
    "anzahl": ("Anzahl (Bahnhöfe, Fotos, übrige Karten …)", "3"),
    "platz": ("Platz in der Rangliste, beim Endstand als Medaille", "🥇"),
    "status": ("Besitzer-Angabe einer Feldzeile, oder leer", "👑 Die Durstigen"),
    "art": ("Straße oder „Bahnhof“ in der eigenen Feldliste", "Gelb ✅"),
    "kuerzel": ("Bahnhofs-Kürzel (erster Buchstabe), bei mehreren mit / getrennt", "N/O/S/W"),
    "erstes": ("Erstes Bahnhofs-Kürzel als Beispiel", "N"),
    "text": ("Text einer Ereigniskarte bzw. Nachricht der Spielleitung oder Kommentar", "0,5 statt 0,33!"),
    "aufgabe": ("Aufgabe für diese Bar", "Stoßt mit dem Wirt an"),
    "karte": ("Text der gezogenen Aktionskarte", "Singt den Refrain eures Lieblingslieds"),
    "plus": ("Punkte, wenn die Aktion geschafft wird", "1"),
    "minus": ("Punkte, wenn die Aktion nicht geschafft wird", "-1"),
    "wo": ("Satz zur Kneipe des letzten Check-ins (aus aktion.wo_zuletzt) oder leer",
           " Ihr seid zuletzt in <b>Die Eule</b> eingecheckt, bleibt dort."),
    "rest": ("Übrige Aktionskarten bzw. verbleibende Spielzeit", "3"),
    "faktor": ("Punkte-Faktor, z.B. bei doppelten Punkten", "2"),
    "uhrzeit": ("Uhrzeit (HH:MM)", "21:45"),
    "dauer": ("Spieldauer", "3 Stunden"),
    "grund": ("Grund der Ablehnung", "Bar nicht erkennbar"),
    "was": ("Was die Spielleitung korrigiert hat", "„Die Durstigen besucht Zucca“ zurückgenommen"),
    "kommentar": ("Kommentar der Spielleitung (aus korrektur.kommentar) oder leer", " (Foto war von gestern)"),
    "frist": ("Minuten für das Foto nach dem Check-in", "20"),
    "frei": ("Punkte für eine freie Bar", "1"),
    "miete": ("Punkte für den Besitzer", "1"),
    "miete_strasse": ("Punkte für den Besitzer mit kompletter Straße", "2"),
    "besucher": ("Hinweis auf Besucherpunkte (aus regeln.besucher) oder leer", ""),
    "strassenbonus": ("Einmaliger Bonus für eine komplette Straße", "2"),
    "bahnhof_punkte": ("Punkte nach Anzahl Bahnhöfe", "1, 2, 3, 4"),
    "aktion_plus": ("Punkte für eine geschaffte Aktion", "1"),
    "aktion_minus": ("Punkte für eine nicht geschaffte Aktion", "-1"),
    "abklingzeit": ("Minuten bis dieselbe fremde Bar wieder zählt", "30"),
    "endzeit": ("Satz zur Spielzeit (aus regeln.endzeit/spielzeit) oder leer",
                "\n\n⏱️ Spielzeit: <b>3 Stunden</b>, Schluss ist ca. um <b>23:30</b> Uhr."),
}


class Raw(str):
    """Wert, der schon fertiges Telegram-HTML enthält und nicht escaped werden darf."""


class _Keep(dict):
    """Unbekannte Platzhalter bleiben sichtbar stehen, statt einen Fehler auszulösen."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _flatten(data: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in data.items():
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, full + "."))
        elif isinstance(value, str):
            out[full] = value
    return out


def _load(path: Path) -> dict[str, str]:
    with path.open("rb") as fh:
        return _flatten(tomllib.load(fh))


def placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name}


class Texts:
    def __init__(self, user_path: str | Path | None = None, default_path: Path = DEFAULT_PATH):
        self.default_path = default_path
        self.defaults = _load(default_path)
        self.user_path = Path(user_path) if user_path else None
        self.user: dict[str, str] = {}
        self.error: str | None = None
        self._mtime: float | None = None
        if self.user_path and not self.user_path.exists():
            try:
                self.user_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(default_path, self.user_path)
            except OSError as e:
                log.warning("Konnte %s nicht anlegen: %s", self.user_path, e)
        self._reload()

    def _reload(self) -> None:
        if not self.user_path or not self.user_path.exists():
            return
        mtime = self.user_path.stat().st_mtime
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            self.user = _load(self.user_path)
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as e:
            self.error = f"{self.user_path.name}: {e}"
            log.error("Textdatei fehlerhaft, die vorherigen Texte gelten weiter: %s", self.error)
            return
        problems = []
        for key, tpl in self.user.items():
            if key not in self.defaults:
                problems.append(f"unbekannter Text „{key}“")
                continue
            allowed = placeholders(self.defaults[key]) | ALLOWED_PLACEHOLDERS.get(key, set())
            unknown = placeholders(tpl) - allowed if _parses(tpl) else {"?"}
            if unknown:
                problems.append(f"„{key}“: unbekannter Platzhalter {', '.join(sorted(unknown))}")
        self.error = "; ".join(problems) or None
        if self.error:
            log.warning("Textdatei: %s", self.error)

    def raw(self, key: str) -> str:
        self._reload()
        tpl = self.user.get(key)
        if tpl is None or not _parses(tpl):
            tpl = self.defaults.get(key)
        if tpl is None:
            log.error("Text „%s“ fehlt", key)
            return f"[{key}]"
        return tpl

    def __call__(self, key: str, **values) -> str:
        tpl = self.raw(key)
        safe = {}
        plain = key in PLAIN_KEYS
        for k, v in values.items():
            if plain or isinstance(v, Raw) or not isinstance(v, str):
                safe[k] = v
            else:
                safe[k] = html.escape(v, quote=False)
        try:
            text = tpl.format_map(_Keep(safe))
        except (ValueError, IndexError, KeyError):
            log.error("Text „%s“ lässt sich nicht füllen, nehme Standardtext", key)
            text = self.defaults.get(key, tpl).format_map(_Keep(safe))
        return text.rstrip("\n")


    # ------------------------------------------------------------------ Bearbeiten (Reiter „Texte“)
    def allowed_placeholders(self, key: str) -> set[str]:
        return placeholders(self.defaults.get(key, "")) | ALLOWED_PLACEHOLDERS.get(key, set())

    def current(self, key: str) -> str:
        self._reload()
        return self.user.get(key, self.defaults.get(key, ""))

    def entries(self) -> list[dict]:
        """Alle Texte mit Bereich, Kommentar aus der Standarddatei, aktuellem Wert und Platzhaltern."""
        self._reload()
        out = []
        for section, name, comment in _structure(self.default_path):
            key = f"{section}.{name}"
            default = self.defaults.get(key, "")
            value = self.user.get(key, default)
            out.append({
                "key": key, "section": section, "name": name, "comment": comment,
                "default": default.rstrip("\n"), "value": value.rstrip("\n"),
                "changed": value.rstrip("\n") != default.rstrip("\n"),
                "plain": key in PLAIN_KEYS,
                "placeholders": sorted(self.allowed_placeholders(key)),
            })
        return out

    def validate(self, key: str, value: str) -> str | None:
        if key not in self.defaults:
            return f"Unbekannter Text „{key}“."
        if not _parses(value):
            return "Geschweifte Klammern passen nicht. Platzhalter sehen so aus: {name}. Eine echte Klammer als {{ oder }}."
        unknown = placeholders(value) - self.allowed_placeholders(key)
        if unknown:
            return "Unbekannte Platzhalter: " + ", ".join("{" + u + "}" for u in sorted(unknown)) + \
                   ". Erlaubt: " + (", ".join("{" + p + "}" for p in sorted(self.allowed_placeholders(key))) or "keine")
        if key in PLAIN_KEYS:
            return None
        return telegram_html_problem(value)

    def save(self, key: str, value: str) -> str | None:
        """Text speichern. Rückgabe: Fehlermeldung oder None."""
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        err = self.validate(key, value)
        if err:
            return err
        if not self.user_path:
            return "Keine Textdatei konfiguriert."
        self._reload()
        values = {k: self.user.get(k, v) for k, v in self.defaults.items()}
        values[key] = value
        return self._write(values)

    def overrides(self) -> dict[str, str]:
        """Alle Texte, die vom Standard abweichen (für den Setup-Export)."""
        self._reload()
        return {k: v.rstrip("\n") for k, v in self.user.items()
                if k in self.defaults and v.rstrip("\n") != self.defaults[k].rstrip("\n")}

    def import_overrides(self, data: dict) -> tuple[str | None, int, list[str]]:
        """Übernimmt geänderte Texte, alle anderen gelten danach wieder als Standard.

        Rückgabe: (Fehler, Anzahl übernommen, übersprungene Texte mit Grund).
        """
        if not isinstance(data, dict):
            return "Texte im falschen Format.", 0, []
        if not self.user_path:
            return "Keine Textdatei konfiguriert.", 0, []
        values = dict(self.defaults)
        skipped = []
        for key, value in data.items():
            value = str(value).replace("\r\n", "\n")
            err = self.validate(key, value)
            if err:
                skipped.append(f"{key}: {err}")
                continue
            values[key] = value
        err = self._write(values)
        return err, len(data) - len(skipped), skipped

    def reset(self, key: str) -> str | None:
        if key not in self.defaults:
            return f"Unbekannter Text „{key}“."
        return self.save(key, self.defaults[key])

    def _write(self, values: dict[str, str]) -> str | None:
        """texte.toml aus der Standarddatei (mit allen Kommentaren) und den Werten neu schreiben."""
        text = render_toml(self.default_path.read_text(encoding="utf-8"), values)
        try:
            check = _flatten(tomllib.loads(text))
        except tomllib.TOMLDecodeError as e:
            return f"Interner Fehler beim Schreiben ({e}). Nichts gespeichert."
        for k, v in values.items():
            if check.get(k, "").rstrip("\n") != v.rstrip("\n"):
                return f"Text „{k}“ lässt sich so nicht speichern. Nichts gespeichert."
        tmp = self.user_path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.user_path)
        self._mtime = None
        self._reload()
        return None


_TG_TAGS = "b|i|u|s|code|pre"


def telegram_html_problem(tpl: str) -> str | None:
    """Was Telegram (parse_mode=HTML) ablehnen würde. Platzhalter werden dabei ignoriert."""
    plain = re.sub(r"\{\{|\}\}", "", tpl)
    plain = re.sub(r"\{[A-Za-z0-9_]+\}", "", plain)
    tagless = re.sub(rf"</?({_TG_TAGS})>", "", plain)
    if "<" in tagless or ">" in tagless:
        return "„<“ oder „>“ außerhalb von <b>, <i>, <code> … Telegram würde die Nachricht ablehnen. Als &lt; bzw. &gt; schreiben."
    if re.search(r"&(?!(amp|lt|gt|quot|#\d+);)", plain):
        return "Ein „&“ muss als &amp; geschrieben werden, sonst lehnt Telegram die Nachricht ab."
    stack: list[str] = []
    for closing, tag in re.findall(rf"<(/?)({_TG_TAGS})>", plain):
        if not closing:
            stack.append(tag)
        elif not stack or stack.pop() != tag:
            return f"Tag </{tag}> passt nicht zum geöffneten Tag."
    if stack:
        return f"<{stack[-1]}> wird nicht geschlossen."
    return None


def _structure(path: Path) -> list[tuple[str, str, str]]:
    """(Bereich, Name, Kommentar) in der Reihenfolge der Standarddatei."""
    out, section, comment = [], "", []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        m_sec = _SECTION.match(s)
        if m_sec:
            section, comment = m_sec.group(1), []
            continue
        if s.startswith("#"):
            if section:
                comment.append(s.lstrip("#").strip())
            continue
        if not s:
            comment = []
            continue
        m = _ASSIGN.match(line)
        if m and section:
            out.append((section, m.group(1), "\n".join(comment)))
            comment = []
    return out


_SECTION = re.compile(r"^\[([A-Za-z0-9_]+)\]$")
_ASSIGN = re.compile(r"^([A-Za-z0-9_]+)\s*=\s*(.*)$")


def toml_string(value: str) -> str:
    """Wert als TOML-String: einzeilig in "…", mehrzeilig in '''…''' (oder \"\"\"…\"\"\" falls nötig)."""
    if "\n" not in value:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if "'''" not in value and not value.endswith("'"):
        return "'''\n" + value.rstrip("\n") + "\n'''"
    body = value.rstrip("\n").replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return '"""\n' + body + '\n"""'


def render_toml(template: str, values: dict[str, str]) -> str:
    """Ersetzt in der Standarddatei die Werte, Kommentare und Reihenfolge bleiben erhalten."""
    lines = template.splitlines()
    out, section, i = [], "", 0
    while i < len(lines):
        line = lines[i]
        m_sec = _SECTION.match(line.strip())
        if m_sec:
            section = m_sec.group(1)
            out.append(line)
            i += 1
            continue
        m = _ASSIGN.match(line)
        if m and section and not line.lstrip().startswith("#"):
            name, rest = m.group(1), m.group(2)
            end = i
            for q in ("'''", '"""'):
                if rest.startswith(q) and rest.count(q) < 2:
                    end = i + 1
                    while end < len(lines) and q not in lines[end]:
                        end += 1
                    break
            key = f"{section}.{name}"
            if key in values:
                out.append(f"{name} = {toml_string(values[key])}")
            else:
                out.extend(lines[i:end + 1])
            i = end + 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out) + "\n"


def _parses(tpl: str) -> bool:
    try:
        list(Formatter().parse(tpl))
        return True
    except ValueError:
        return False
