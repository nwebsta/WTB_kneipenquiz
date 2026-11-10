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
        for k, v in values.items():
            if isinstance(v, Raw) or not isinstance(v, str):
                safe[k] = v
            else:
                safe[k] = html.escape(v, quote=False)
        try:
            text = tpl.format_map(_Keep(safe))
        except (ValueError, IndexError, KeyError):
            log.error("Text „%s“ lässt sich nicht füllen, nehme Standardtext", key)
            text = self.defaults.get(key, tpl).format_map(_Keep(safe))
        return text.rstrip("\n")


def _parses(tpl: str) -> bool:
    try:
        list(Formatter().parse(tpl))
        return True
    except ValueError:
        return False
