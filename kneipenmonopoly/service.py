"""Spiellogik rund um Datenbank und Benachrichtigungen.

Hier passiert alles, was Bot und Webseite gemeinsam brauchen. Der Spielstand
wird immer aus dem Ereignisprotokoll berechnet (rules.replay).
"""
from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import logging
import random
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from . import rules as R
from .config import Settings
from .db import DB
from .gamedata import GameData, GameDataError, distance_m, parse_game_data
from .mapimage import MapField, MapGroup, MapSpec, MapStation, render_map
from .texts import Raw, Texts

log = logging.getLogger(__name__)

PHASE_LABEL = {"setup": "Vorbereitung", "running": "läuft", "paused": "pausiert", "ended": "beendet"}
SETUP_FORMAT = "kneipenmonopoly-setup"
SETUP_PARTS = {
    "spieldaten": "Kneipen, Bahnhöfe und Reserve-Bars (Karte)",
    "teams": "Teams (Name, Farbe, Code; Telegram-Gruppen bleiben)",
    "aktionskarten": "Aktionskarten",
    "ereigniskarten": "Ereigniskarten",
    "aufgaben": "Aufgaben pro Feld und Zufallsaufgaben",
    "einstellungen": "Spieldauer und „Punktestand verdecken“",
}
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def esc(s: object) -> str:
    return html.escape(str(s), quote=False)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def slugify(text: str) -> str:
    t = text.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("é", "e"), ("è", "e"), ("&", "und")):
        t = t.replace(a, b)
    t = "".join(c if c.isalnum() else "-" for c in t)
    while "--" in t:
        t = t.replace("--", "-")
    return t.strip("-") or "feld"


def normalize_game_data(raw: dict) -> dict:
    """Spieldaten aus dem Editor aufräumen und vereinheitlichen.

    - fehlende IDs aus dem Namen erzeugen (eindeutig)
    - `field_ids` jeder Straße aus der Reihenfolge der Felder ableiten
    - Feldnummern 1..n in Straßen- und Feldreihenfolge neu vergeben
    - Koordinaten prüfen
    """
    if not isinstance(raw, dict):
        raise ValueError("kein Objekt")
    out = {k: v for k, v in raw.items() if k not in ("groups", "fields", "stations", "reserve")}
    used: set[str] = set()

    def new_id(name: str, existing: str | None, pool: set[str]) -> str:
        if existing and existing not in pool:
            pool.add(existing)
            return existing
        base = slugify(name)
        cand, n = base, 2
        while cand in pool:
            cand, n = f"{base}-{n}", n + 1
        pool.add(cand)
        return cand

    def coord(item: dict, what: str) -> tuple[float, float]:
        lat, lon = float(item["lat"]), float(item["lon"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f"Koordinaten von {what} ungültig")
        return round(lat, 6), round(lon, 6)

    group_ids: set[str] = set()
    groups = []
    for g in raw.get("groups", []):
        name = str(g.get("name", "")).strip()
        if not name:
            raise ValueError("Straße ohne Namen")
        gid = new_id(name, g.get("id"), group_ids)
        color = str(g.get("color") or "#888888")
        groups.append({"id": gid, "name": name, "color": color, "field_ids": []})
    by_gid = {g["id"]: g for g in groups}
    if not groups:
        raise ValueError("Es braucht mindestens eine Straße")

    fields = []
    for f in raw.get("fields", []):
        name = str(f.get("name", "")).strip()
        if not name:
            raise ValueError("Kneipe ohne Namen")
        if f.get("group") not in by_gid:
            raise ValueError(f"{name}: keine gültige Straße")
        lat, lon = coord(f, name)
        fid = new_id(name, f.get("id"), used)
        fields.append({"id": fid, "no": 0, "name": name, "type": "kneipe" if f.get("type") == "kneipe" else "bar",
                       "group": f["group"], "address": str(f.get("address", "")).strip(), "lat": lat, "lon": lon})
    no = 1
    for g in groups:                         # Nummern in Straßen- und Feldreihenfolge
        for f in fields:
            if f["group"] == g["id"]:
                g["field_ids"].append(f["id"])
                f["no"] = no
                no += 1
    fields.sort(key=lambda f: f["no"])

    stations = []
    for s in raw.get("stations", []):
        name = str(s.get("name", "")).strip()
        if not name:
            raise ValueError("Bahnhof ohne Namen")
        lat, lon = coord(s, name)
        stations.append({"id": new_id(name, s.get("id"), used), "name": name,
                         "venue": str(s.get("venue", "")).strip(), "lat": lat, "lon": lon,
                         "verified_open": bool(s.get("verified_open", True))})
    reserve = []
    for r in raw.get("reserve", []):
        name = str(r.get("name", "")).strip()
        if not name:
            raise ValueError("Reserve-Bar ohne Namen")
        lat, lon = coord(r, name)
        reserve.append({"id": new_id(name, r.get("id"), used), "name": name,
                        "address": str(r.get("address", "")).strip(), "lat": lat, "lon": lon})
    out.update(groups=groups, fields=fields, stations=stations, reserve=reserve)
    return out


def structure_of(raw: dict) -> tuple:
    """Was sich im laufenden Spiel nicht ändern darf: welche Felder/Bahnhöfe/Reserve es gibt und welche Straße wozu gehört."""
    return (
        tuple((g["id"], tuple(g["field_ids"])) for g in raw["groups"]),
        tuple(s["id"] for s in raw["stations"]),
        tuple(r["id"] for r in raw["reserve"]),
    )


def fmt_duration(minutes: int) -> str:
    """180 -> „3 Stunden“, 150 -> „2 Std 30 Min“, 45 -> „45 Min“."""
    h, m = divmod(int(minutes), 60)
    if h and not m:
        return "1 Stunde" if h == 1 else f"{h} Stunden"
    if h:
        return f"{h} Std {m} Min"
    return f"{m} Min"


class Notifier(Protocol):
    async def send_text(self, chat_id: int, text: str, buttons: list[tuple[str, str]] | None = None) -> None: ...
    async def send_map(self, chat_id: int, caption: str) -> None: ...


class NullNotifier:
    """Wird benutzt, wenn kein Bot läuft (Tests, Web-only)."""

    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.buttons: list[tuple[int, list]] = []

    async def send_text(self, chat_id: int, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        self.sent.append((chat_id, text))
        if buttons:
            self.buttons.append((chat_id, buttons))

    async def send_map(self, chat_id: int, caption: str) -> None:
        self.sent.append((chat_id, "[KARTE] " + caption))


@dataclass
class Spot:
    id: str
    kind: str            # field | station
    name: str
    lat: float
    lon: float
    address: str
    group: str | None
    group_name: str
    color: str
    active: bool
    original_name: str | None = None   # gesetzt, wenn durch Reserve ersetzt
    is_kneipe: bool = False
    no: int | None = None


@dataclass
class LocationResult:
    text: str | None = None                          # direkte Antwort
    choices: list[tuple[Spot, float]] | None = None  # mehrere Felder im Radius
    buttons: list[tuple[str, str]] | None = None     # z.B. Kurzen & gehen


@dataclass
class ActionResult:
    ok: bool
    message: str
    buttons: list[tuple[str, str]] | None = None   # (Beschriftung, callback_data) für Telegram


# Ereigniskarten, mit denen eine neue Datenbank startet. In der Verwaltung änderbar.
DEFAULT_EVENT_CARDS = [
    (45, "next_checkin", "0,5 statt 0,33! In dieser Kneipe trinkt jede*r ein großes Bier (0,5 l)."),
    (90, "next_checkin", "Shot statt 0,33! In dieser Kneipe gibt's für jede*n einen Kurzen statt Bier."),
    (150, "double_points", "Ab jetzt zählen alle Punkte doppelt, bis zum Spielende! "
                           "Es zählt die Uhrzeit, zu der ihr das Foto hochladet."),
]
EVENT_CARD_KINDS = {
    "next_checkin": "beim nächsten Check-in in einer Kneipe",
    "double_points": "sofort an alle: doppelte Punkte",
    "broadcast": "sofort an alle: nur Nachricht",
}


class Game:
    def __init__(self, settings: Settings, data: GameData, db: DB,
                 notifier: Notifier | None = None, clock: Callable[[], datetime] = utcnow,
                 texts: Texts | None = None):
        self.s = settings
        self.data = data
        self.t = texts or Texts(settings.texts_path)
        self.db = db
        self.notifier: Notifier = notifier or NullNotifier()
        # Teams, deren Bot-Nachrichten nicht an Telegram gehen (Simulation). Sie landen nur im Chatlog.
        self.muted_teams: set[int] = set()
        self.clock = clock
        self.board = data.board
        self._state: R.State | None = None
        self.s.photo_dir.mkdir(parents=True, exist_ok=True)
        if self.db.get_kv("event_cards_seeded") is None:
            for offset, kind, text in DEFAULT_EVENT_CARDS:
                self.db.run("INSERT INTO event_cards(offset_min,kind,text) VALUES(?,?,?)", (offset, kind, text))
            self.db.set_kv("event_cards_seeded", "1")

    # =========================================================================
    # Zustand
    # =========================================================================
    def state(self) -> R.State:
        if self._state is None:
            self._state = R.replay(self.db.events(), self.board, self.s.rules)
        return self._state

    def invalidate(self) -> None:
        self._state = None

    def _event(self, conn, actor: str, type_: str, data: dict) -> R.Event:
        ev = self.db.insert_event(conn, iso(self.clock()), actor, type_, data)
        self.invalidate()
        return ev

    def add_event(self, actor: str, type_: str, data: dict) -> R.Event:
        with self.db.tx() as conn:
            return self._event(conn, actor, type_, data)

    @property
    def phase(self) -> str:
        return self.state().phase

    def local(self, dt: datetime | str | None) -> datetime | None:
        if dt is None:
            return None
        if isinstance(dt, str):
            dt = parse_iso(dt)
        return dt.astimezone(self.s.tz)

    def hm(self, dt: datetime | str | None) -> str:
        d = self.local(dt)
        return d.strftime("%H:%M") if d else "–"

    # =========================================================================
    # Felder
    # =========================================================================
    def spot(self, spot_id: str, state: R.State | None = None) -> Spot:
        st = state or self.state()
        if spot_id in self.data.stations:
            s = self.data.stations[spot_id]
            return Spot(s.id, "station", s.name, s.lat, s.lon, s.venue, None, "Bahnhof", "#222222",
                        spot_id not in st.inactive)
        f = self.data.fields[spot_id]
        g = self.data.groups[f.group]
        rid = st.replaced.get(spot_id)
        if rid and rid in self.data.reserve:
            r = self.data.reserve[rid]
            return Spot(f.id, "field", r.name, r.lat, r.lon, r.address, g.id, g.name, g.color,
                        spot_id not in st.inactive, original_name=f.name, is_kneipe=False, no=f.no)
        return Spot(f.id, "field", f.name, f.lat, f.lon, f.address, g.id, g.name, g.color,
                    spot_id not in st.inactive, is_kneipe=f.type == "kneipe", no=f.no)

    def all_spots(self, state: R.State | None = None) -> list[Spot]:
        st = state or self.state()
        ids = sorted(self.data.fields, key=lambda i: self.data.fields[i].no) + list(self.data.stations)
        return [self.spot(i, st) for i in ids]

    def nearby(self, lat: float, lon: float) -> list[tuple[Spot, float]]:
        out = [(sp, distance_m(lat, lon, sp.lat, sp.lon)) for sp in self.all_spots() if sp.active]
        out.sort(key=lambda x: x[1])
        return out

    def spot_label(self, spot_id: str) -> str:
        sp = self.spot(spot_id)
        if sp.kind == "station":
            return f"🚉 {sp.name}"
        return f"{sp.name} ({sp.group_name})"

    # =========================================================================
    # Teams
    # =========================================================================
    def teams(self) -> list:
        return self.db.all("SELECT * FROM teams ORDER BY id")

    def team(self, team_id: int):
        return self.db.one("SELECT * FROM teams WHERE id=?", (team_id,))

    def team_by_chat(self, chat_id: int):
        return self.db.one("SELECT * FROM teams WHERE chat_id=?", (chat_id,))

    def team_names(self) -> dict[int, str]:
        return {t["id"]: t["name"] for t in self.teams()}

    def team_name(self, team_id: int | None) -> str:
        if team_id is None:
            return "–"
        return self.team_names().get(team_id, f"Team #{team_id}")

    def create_team(self, name: str, color: str, code: str | None = None) -> ActionResult:
        name = name.strip()
        if not name:
            return ActionResult(False, "Name fehlt.")
        code = (code or "").strip().upper() or "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))
        try:
            self.db.run("INSERT INTO teams(name,color,code,created_at) VALUES(?,?,?,?)",
                        (name, color or "#888888", code, iso(self.clock())))
        except Exception as e:  # sqlite3.IntegrityError
            return ActionResult(False, f"Name oder Code schon vergeben ({e}).")
        return ActionResult(True, f"Team {name} angelegt, Code {code}.")

    def update_team(self, team_id: int, name: str, color: str, code: str) -> ActionResult:
        try:
            self.db.run("UPDATE teams SET name=?, color=?, code=? WHERE id=?",
                        (name.strip(), color, code.strip().upper(), team_id))
        except Exception as e:
            return ActionResult(False, f"Speichern fehlgeschlagen ({e}).")
        return ActionResult(True, "Team gespeichert.")

    def delete_team(self, team_id: int) -> ActionResult:
        if self.phase != "setup":
            return ActionResult(False, "Teams lassen sich nur vor Spielbeginn löschen.")
        self.db.run("DELETE FROM teams WHERE id=?", (team_id,))
        return ActionResult(True, "Team gelöscht.")

    def unlink_team(self, team_id: int) -> ActionResult:
        self.db.run("UPDATE teams SET chat_id=NULL, chat_title=NULL WHERE id=?", (team_id,))
        return ActionResult(True, "Chat-Verknüpfung entfernt.")

    def join(self, chat_id: int, chat_title: str, code: str) -> tuple[ActionResult, bool]:
        """Verknüpft einen Chat mit einem Team. Rückgabe: (Ergebnis, Karte schicken?)."""
        T = self.t
        code = code.strip().upper()
        if not code:
            return ActionResult(False, T("join.ohne_code")), False
        team = self.db.one("SELECT * FROM teams WHERE code=?", (code,))
        if team is None:
            return ActionResult(False, T("join.code_unbekannt")), False
        current = self.team_by_chat(chat_id)
        if current is not None and current["id"] != team["id"]:
            return ActionResult(False, T("join.chat_vergeben", team=current["name"])), False
        moved = team["chat_id"] is not None and team["chat_id"] != chat_id
        self.db.run("UPDATE teams SET chat_id=?, chat_title=? WHERE id=?", (chat_id, chat_title, team["id"]))
        parts = [T("join.willkommen", team=team["name"])]
        if moved:
            parts.append(T("join.umgezogen"))
        send_map = self.phase in ("running", "paused")
        parts.append(T("join.spiel_laeuft") if send_map else T("join.spiel_wartet"))
        return ActionResult(True, "\n\n".join(parts)), send_map

    # =========================================================================
    # Nachrichten senden / protokollieren
    # =========================================================================
    def log_message(self, *, team_id: int | None, chat_id: int | None, direction: str, kind: str,
                    text: str | None = None, sender: str | None = None, tg_message_id: int | None = None,
                    photo_path: str | None = None, file_unique_id: str | None = None,
                    lat: float | None = None, lon: float | None = None, chat_title: str | None = None) -> None:
        self.db.run(
            "INSERT INTO messages(ts,team_id,chat_id,tg_message_id,sender,direction,kind,text,photo_path,"
            "file_unique_id,lat,lon,chat_title) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iso(self.clock()), team_id, chat_id, tg_message_id, sender, direction, kind, text, photo_path,
             file_unique_id, lat, lon, chat_title))

    def known_photo(self, file_unique_id: str) -> str | None:
        row = self.db.one("SELECT photo_path FROM messages WHERE file_unique_id=? AND photo_path IS NOT NULL "
                          "UNION ALL SELECT photo_path FROM submissions WHERE file_unique_id=? "
                          "AND photo_path IS NOT NULL LIMIT 1", (file_unique_id, file_unique_id))
        if row and row["photo_path"] and Path(row["photo_path"]).exists():
            return row["photo_path"]
        return None

    def photo_dest(self, file_unique_id: str) -> Path:
        return self.s.photo_dir / f"{file_unique_id}.jpg"

    def migrate_chat(self, old_chat_id: int, new_chat_id: int) -> None:
        """Telegram hat eine Gruppe zur Supergruppe gemacht: neue Chat-ID übernehmen."""
        n = self.db.run("UPDATE teams SET chat_id=? WHERE chat_id=?", (new_chat_id, old_chat_id)).rowcount
        if n:
            log.info("Chat %s ist jetzt %s (Supergruppe), Team-Verknüpfung angepasst", old_chat_id, new_chat_id)

    # =========================================================================
    # Live-Standort (nur für die Spielleitung sichtbar, nur letzte Position)
    # =========================================================================
    def update_live(self, team_id: int, lat: float, lon: float, accuracy: float | None = None,
                    live_until: datetime | None = None) -> None:
        self.db.run(
            "INSERT INTO live_positions(team_id,lat,lon,accuracy,ts,live_until) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(team_id) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, accuracy=excluded.accuracy, "
            "ts=excluded.ts, live_until=COALESCE(excluded.live_until, live_positions.live_until)",
            (team_id, lat, lon, accuracy, iso(self.clock()), iso(live_until) if live_until else None))

    def live_snapshot(self, team_id: int, sp: "Spot") -> dict | None:
        """Letzte Live-Position des Teams im Vergleich zum Feld, oder None."""
        row = self.db.one("SELECT * FROM live_positions WHERE team_id=?", (team_id,))
        if row is None:
            return None
        now = self.clock()
        return {
            "lat": row["lat"], "lon": row["lon"], "accuracy": row["accuracy"],
            "distance_m": round(distance_m(row["lat"], row["lon"], sp.lat, sp.lon), 1),
            "age_s": max(0, int((now - parse_iso(row["ts"])).total_seconds())),
            "ended": bool(row["live_until"]) and parse_iso(row["live_until"]) < now,
        }

    async def send_team(self, team_id: int, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        team = self.team(team_id)
        if team is None:
            return
        if team_id in self.muted_teams:
            self.log_message(team_id=team_id, chat_id=team["chat_id"], direction="out", kind="text",
                             text=text, sender="Bot (Simulation, nicht gesendet)")
            return
        if team["chat_id"] is None:
            return
        try:
            await self.notifier.send_text(team["chat_id"], text, buttons)
        except Exception:
            log.exception("Senden an Team %s fehlgeschlagen", team_id)
        team = self.team(team_id)  # Chat-ID kann sich beim Senden geändert haben
        self.log_message(team_id=team_id, chat_id=team["chat_id"], direction="out", kind="text",
                         text=text, sender="Bot")

    async def send_all(self, text: str, exclude: set[int] | None = None) -> None:
        for t in self.teams():
            if exclude and t["id"] in exclude:
                continue
            await self.send_team(t["id"], text)

    async def send_map_to(self, team_id: int) -> None:
        team = self.team(team_id)
        if team is None or team["chat_id"] is None or team_id in self.muted_teams:
            return
        try:
            await self.notifier.send_map(team["chat_id"], self.t("allgemein.karte_beschriftung"))
        except Exception:
            log.exception("Karte an Team %s fehlgeschlagen", team_id)
        self.log_message(team_id=team_id, chat_id=team["chat_id"], direction="out", kind="other",
                         text="[Karte gesendet]", sender="Bot")

    async def send_welcome_pack(self, team_id: int) -> None:
        await self.send_map_to(team_id)
        await self.send_team(team_id, self.rules_text())

    async def admin_message(self, admin: str, text: str, team_id: int | None = None) -> None:
        msg = self.t("allgemein.spielleitung", text=text)
        if team_id is None:
            await self.send_all(msg)
        else:
            await self.send_team(team_id, msg)

    # =========================================================================
    # Spielsteuerung
    # =========================================================================
    def scores_hidden(self) -> bool:
        v = self.db.get_kv("scores_hidden")
        return self.s.scores_hidden_default if v is None else v == "1"

    def set_scores_hidden(self, hidden: bool) -> None:
        self.db.set_kv("scores_hidden", "1" if hidden else "0")

    # =========================================================================
    # Spieluhr: Spieldauer ab Start, Pausen halten die Uhr an
    # =========================================================================
    def duration_min(self) -> int | None:
        v = self.db.get_kv("duration_min")
        if v is None:
            return self.s.game_duration_min
        return int(v) if v else None

    def set_duration(self, raw: str | None) -> ActionResult:
        """Spieldauer setzen, z.B. „3:00“ oder „180“ (Minuten). Leer = kein automatisches Ende."""
        raw = (raw or "").strip()
        if not raw:
            self.db.set_kv("duration_min", "")
            return ActionResult(True, "Spieldauer entfernt, das Spiel endet nur per Klick.")
        try:
            if ":" in raw:
                h, m = raw.split(":", 1)
                minutes = int(h) * 60 + int(m)
            else:
                minutes = int(raw)
        except ValueError:
            return ActionResult(False, "Spieldauer bitte als H:MM angeben, z.B. 3:00.")
        if minutes <= 0:
            return ActionResult(False, "Die Spieldauer muss größer als 0 sein.")
        self.db.set_kv("duration_min", str(minutes))
        self.db.set_kv("warned_30", None)
        self.db.set_kv("warned_10", None)
        end = self.end_time()
        return ActionResult(True, f"Spieldauer: {fmt_duration(minutes)}"
                                  + (f", Schluss ca. um {self.hm(end)} Uhr." if end else "."))

    def paused_seconds(self, now: datetime | None = None) -> float:
        """Summe aller Pausen seit Spielstart, eine laufende Pause eingeschlossen."""
        now = now or self.clock()
        total, since = 0.0, None
        for e in self.db.events():
            if e.type == R.GAME_PAUSED and since is None:
                since = parse_iso(e.ts)
            elif e.type == R.GAME_RESUMED and since is not None:
                total += (parse_iso(e.ts) - since).total_seconds()
                since = None
        if since is not None:
            total += (now - since).total_seconds()
        return total

    def game_elapsed(self, now: datetime | None = None) -> timedelta | None:
        """Gespielte Zeit seit Start ohne Pausen. None vor dem Start."""
        started = self.state().started_at
        if not started:
            return None
        now = now or self.clock()
        return now - parse_iso(started) - timedelta(seconds=self.paused_seconds(now))

    def game_remaining(self) -> timedelta | None:
        dur, elapsed = self.duration_min(), self.game_elapsed()
        if dur is None or elapsed is None:
            return None
        return timedelta(minutes=dur) - elapsed

    def end_time(self) -> datetime | None:
        """Voraussichtliches Spielende als Uhrzeit (verschiebt sich durch Pausen)."""
        left = self.game_remaining()
        return self.clock() + left if left is not None else None

    def at_game_time(self, minutes: int) -> datetime | None:
        """Uhrzeit, zu der die Spieluhr `minutes` erreicht (hat). None vor dem Start."""
        elapsed = self.game_elapsed()
        if elapsed is None:
            return None
        return self.clock() + (timedelta(minutes=minutes) - elapsed)

    async def start_game(self, admin: str) -> ActionResult:
        if self.phase != "setup":
            return ActionResult(False, "Das Spiel wurde schon gestartet.")
        self.add_event(admin, R.GAME_STARTED, {})
        self.db.set_kv("warned_30", None)
        self.db.set_kv("warned_10", None)
        start_text = self.t("spiel.start")
        if self.duration_min():
            start_text += "\n" + self.t("spiel.start_dauer", dauer=fmt_duration(self.duration_min()),
                                        uhrzeit=self.hm(self.end_time()))
        for t in self.teams():
            if t["chat_id"] is not None or t["id"] in self.muted_teams:
                await self.send_team(t["id"], start_text)
                await self.send_welcome_pack(t["id"])
        return ActionResult(True, "Spiel gestartet, Karte und Regeln verschickt.")

    async def pause_game(self, admin: str) -> ActionResult:
        if self.phase != "running":
            return ActionResult(False, "Pausieren geht nur, wenn das Spiel läuft.")
        self.add_event(admin, R.GAME_PAUSED, {})
        self.db.set_kv("pause_started", iso(self.clock()))
        await self.send_all(self.t("spiel.pause"))
        return ActionResult(True, "Spiel pausiert.")

    async def resume_game(self, admin: str) -> ActionResult:
        if self.phase != "paused":
            return ActionResult(False, "Das Spiel ist nicht pausiert.")
        started = self.db.get_kv("pause_started")
        with self.db.tx() as conn:
            if started:
                delta = self.clock() - parse_iso(started)
                for c in conn.execute("SELECT id, expires_at FROM checkins WHERE status='open'").fetchall():
                    conn.execute("UPDATE checkins SET expires_at=? WHERE id=?",
                                 (iso(parse_iso(c["expires_at"]) + delta), c["id"]))
            self._event(conn, admin, R.GAME_RESUMED, {})
        self.db.set_kv("pause_started", None)
        await self.send_all(self.t("spiel.weiter"))
        return ActionResult(True, "Spiel läuft wieder.")

    async def end_game(self, admin: str) -> ActionResult:
        if self.phase not in ("running", "paused"):
            return ActionResult(False, "Das Spiel läuft gerade nicht.")
        with self.db.tx() as conn:
            conn.execute("UPDATE checkins SET status='expired' WHERE status='open'")
            self._event(conn, admin, R.GAME_ENDED, {})
        await self.send_all(self.t("spiel.ende"))
        await self._maybe_final()
        return ActionResult(True, "Spiel beendet.")

    async def announce_final(self) -> ActionResult:
        st = self.state()
        names = self.team_names()
        rows = R.ranking(st.points, list(names))
        T = self.t
        lines = [T("spiel.endstand_kopf")]
        for place, tid, pts in rows:
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(place, f"{place}.")
            lines.append(T("spiel.endstand_zeile", platz=medal, team=names[tid], punkte=pts))
        winners = [names[t] for p, t, _ in rows if p == 1]
        if winners:
            lines.append("")
            if len(winners) == 1:
                lines.append(T("spiel.sieger", team=winners[0]))
            else:
                lines.append(T("spiel.sieger_gleichstand", teams=self._bold_names(winners)))
        await self.send_all("\n".join(lines))
        return ActionResult(True, "Endstand verschickt.")

    # =========================================================================
    # Check-in
    # =========================================================================
    def open_checkin(self, team_id: int):
        return self.db.one("SELECT * FROM checkins WHERE team_id=? AND status='open' ORDER BY id DESC LIMIT 1",
                           (team_id,))

    @staticmethod
    def _bold_names(names: list[str]) -> Raw:
        return Raw(" und ".join(f"<b>{esc(n)}</b>" for n in names))

    def _phase_block(self) -> str | None:
        key = {"setup": "spiel.noch_vorbereitung", "paused": "spiel.gerade_pause", "ended": "spiel.vorbei"}
        return self.t(key[self.phase]) if self.phase in key else None

    def handle_location(self, team_id: int, lat: float, lon: float) -> LocationResult:
        block = self._phase_block()
        if block:
            return LocationResult(text=block)
        near = self.nearby(lat, lon)
        if not near:
            return LocationResult(text=self.t("checkin.keine_felder"))
        within = [(sp, d) for sp, d in near if d <= self.s.checkin_radius_m]
        if not within:
            sp, d = near[0]
            return LocationResult(text=self.t("checkin.kein_feld_hier", bar=sp.name, meter=round(d)))
        if len(within) == 1:
            sp, d = within[0]
            res = self.checkin(team_id, sp.id, lat, lon)
            return LocationResult(text=res.message, buttons=res.buttons)
        return LocationResult(choices=within)

    def _cooldown_block(self, team_id: int, spot_id: str) -> str | None:
        pending = self.db.one("SELECT id FROM submissions WHERE team_id=? AND spot_id=? AND status='pending'",
                              (team_id, spot_id))
        if pending:
            return self.t("checkin.foto_in_pruefung")
        last = self.db.one("SELECT submitted_at FROM submissions WHERE team_id=? AND spot_id=? "
                           "AND status='approved' ORDER BY submitted_at DESC LIMIT 1", (team_id, spot_id))
        if last:
            ready = parse_iso(last["submitted_at"]) + timedelta(minutes=self.s.cooldown_min)
            if ready > self.clock():
                mins = max(1, round((ready - self.clock()).total_seconds() / 60))
                return self.t("checkin.abklingzeit", minuten=mins)
        return None

    def _pick_task(self, spot_id: str) -> str | None:
        row = self.db.one("SELECT text FROM tasks WHERE spot_id=?", (spot_id,))
        if row and row["text"].strip():
            return row["text"].strip()
        if self.s.random_tasks_enabled:
            pool = self.db.all("SELECT text FROM random_tasks")
            if pool:
                return random.choice(pool)["text"]
        return None

    # --- Testmodus ----------------------------------------------------------
    def test_mode(self) -> bool:
        return self.db.get_kv("test_mode") == "1"

    def set_test_mode(self, on: bool) -> None:
        self.db.set_kv("test_mode", "1" if on else "0")

    def station_letters(self) -> dict[str, str]:
        """Kürzel für Bahnhöfe aus dem ersten Buchstaben des Namens, z.B. N, O, S, W."""
        out: dict[str, str] = {}
        for sid, st in self.data.stations.items():
            letter = st.name[:1].upper()
            if letter and letter not in out:
                out[letter] = sid
        return out

    def resolve_spot(self, ref: str) -> str | None:
        """Feldnummer, Bahnhofs-Kürzel, ID oder eindeutiger Namensanfang -> Feld-ID."""
        r = ref.strip()
        if not r:
            return None
        if r.isdigit():
            n = int(r)
            return next((f.id for f in self.data.fields.values() if f.no == n), None)
        if r.upper() in self.station_letters():
            return self.station_letters()[r.upper()]
        low = r.lower()
        if self.data.is_spot(low):
            return low
        matches = [sp.id for sp in self.all_spots() if sp.name.lower().startswith(low)]
        return matches[0] if len(matches) == 1 else None

    def test_checkin(self, team_id: int, ref: str) -> ActionResult:
        if not self.test_mode():
            return ActionResult(False, self.t("testmodus.nur_im_testmodus"))
        spot_id = self.resolve_spot(ref)
        if spot_id is None:
            letters = "/".join(self.station_letters())
            return ActionResult(False, self.t("testmodus.feld_nicht_gefunden", kuerzel=letters,
                                              erstes=letters[:1] or "N"))
        sp = self.spot(spot_id)
        return self.checkin(team_id, spot_id, sp.lat, sp.lon, is_test=True)

    def reset_game(self) -> ActionResult:
        """Spielstand komplett zurücksetzen. Teams, Verknüpfungen und Aufgaben bleiben."""
        with self.db.tx() as conn:
            for table in ("events", "checkins", "submissions", "messages", "calls", "live_positions"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM kv WHERE key IN ('pause_started','warned_30','warned_10') "
                         "OR key LIKE 'photo_hint_%' OR key LIKE 'event_card_fired_%'")
        self.invalidate()
        if self.s.photo_dir.exists():
            shutil.rmtree(self.s.photo_dir, ignore_errors=True)
        self.s.photo_dir.mkdir(parents=True, exist_ok=True)
        return ActionResult(True, "Spiel zurückgesetzt. Teams und Aufgaben sind noch da, das Spiel steht "
                                  "wieder auf Vorbereitung.")

    def checkin(self, team_id: int, spot_id: str, lat: float, lon: float, is_test: bool = False) -> ActionResult:
        block = self._phase_block()
        if block:
            return ActionResult(False, block)
        T = self.t
        if not self.data.is_spot(spot_id):
            return ActionResult(False, T("checkin.feld_unbekannt"))
        sp = self.spot(spot_id)
        if not sp.active:
            return ActionResult(False, T("checkin.feld_inaktiv", bar=sp.name))
        dist = distance_m(lat, lon, sp.lat, sp.lon)
        if dist > self.s.checkin_radius_m:
            return ActionResult(False, T("checkin.feld_zu_weit", bar=sp.name, meter=round(dist)))
        st = self.state()
        owner = st.owners.get(spot_id)
        if owner == team_id:
            return ActionResult(False, T("checkin.eigene_bar", bar=sp.name))
        cd = self._cooldown_block(team_id, spot_id)
        if cd:
            return ActionResult(False, cd)

        task = self._pick_task(spot_id)
        card = self.pending_event_card(team_id) if sp.kind == "field" else None
        live = self.live_snapshot(team_id, sp)
        now = self.clock()
        expires = now + timedelta(minutes=self.s.checkin_timeout_min)
        with self.db.tx() as conn:
            conn.execute("UPDATE checkins SET status='replaced' WHERE team_id=? AND status='open'", (team_id,))
            cur = conn.execute(
                "INSERT INTO checkins(team_id,spot_id,lat,lon,distance_m,task_text,status,created_at,expires_at,"
                "is_test,event_card_id,event_text,live_distance_m,live_age_s) VALUES(?,?,?,?,?,?,'open',?,?,?,?,?,?,?)",
                (team_id, spot_id, lat, lon, round(dist, 1), task, iso(now), iso(expires), int(is_test),
                 card["id"] if card else None, card["text"] if card else None,
                 live["distance_m"] if live else None, live["age_s"] if live else None))
            checkin_id = cur.lastrowid

        cfg = self.s.rules
        mult = self.state().multiplier
        buttons = None
        lines = [T("checkin.testmodus_kopf")] if is_test else []
        kind = "bar" if sp.kind == "field" else "bahnhof"
        lines.append(T(f"checkin.eingecheckt_{kind}" + ("_test" if is_test else ""),
                       bar=sp.name, strasse=sp.group_name, meter=round(dist)))
        rivals: list[int] = []
        if owner is None:
            # Ohne anderes Team in der Bar bleibt offen, ob sie frei ist (Text standardmäßig leer).
            if sp.kind == "station":
                n = R.station_count(st, self.board, team_id) + 1
                free = T("checkin.frei_bahnhof", punkte=cfg.station_value(n) * mult, nummer=n)
            else:
                free = T("checkin.frei_bar", punkte=cfg.points_free_field * mult)
            if free:
                lines.append(free)
            # Gilt für Bars und Bahnhöfe: das zuerst hochgeladene Foto gewinnt.
            rivals = self.rivals_at(spot_id, team_id)
            if rivals:
                lines += ["", T(f"checkin.wettrennen_{kind}",
                                teams=self._bold_names([self.team_name(t) for t in rivals]))]
                buttons = [(T("allgemein.button_kurzen"), f"mode|{checkin_id}|shot")]
        else:
            # Wem die Bar gehört, verrät der Check-in standardmäßig nicht (Texte leer, in texte.toml einschaltbar).
            oname = self.team_name(owner)
            if sp.kind == "station":
                n = R.station_count(st, self.board, owner)
                info = T("checkin.besetzt_bahnhof", team=oname, punkte=cfg.station_value(n) * mult, anzahl=n)
            else:
                full = R.street_owner(st, self.board, sp.group) == owner
                val = (cfg.points_owner_street if full else cfg.points_owner) * mult
                info = T("checkin.besetzt_bar_strasse" if full else "checkin.besetzt_bar", team=oname, punkte=val)
            visitor = T("checkin.besucher_punkte", punkte=cfg.points_visitor * mult) if cfg.points_visitor else ""
            lines += [x for x in (info, visitor) if x]
        if card:
            lines += ["", T("checkin.ereigniskarte", text=card["text"])]
        if task:
            lines += ["", T("checkin.aufgabe", punkte=cfg.task_bonus * mult, aufgabe=task)]
        if mult != 1:
            lines += ["", T("checkin.multiplikator", faktor=mult)]
        lines += ["", T(f"checkin.foto_{kind}", uhrzeit=self.hm(expires))]
        if not rivals:
            # Im Wettrennen nur „Kurzen & gehen“ zeigen, damit es nicht verwirrend wird.
            buttons = (buttons or []) + self.action_buttons(team_id)
        return ActionResult(True, "\n".join(lines), buttons=buttons or None)

    # =========================================================================
    # Freie Bar, anderes Team schon drin: Wettrennen oder Kurzen & gehen
    # =========================================================================
    def rivals_at(self, spot_id: str, team_id: int) -> list[int]:
        """Andere Teams, die gerade in dieser Bar eingecheckt sind (offen oder Foto in Prüfung)."""
        rows = self.db.all("SELECT team_id, MIN(created_at) AS t FROM checkins WHERE spot_id=? AND team_id<>? "
                           "AND status IN ('open','submitted') GROUP BY team_id ORDER BY t",
                           (spot_id, team_id))
        return [r["team_id"] for r in rows]

    async def choose_mode(self, team_id: int, checkin_id: int, mode: str) -> ActionResult:
        """Kurzen & gehen: Check-in beenden, in dieser Bar zählt nichts."""
        if mode != "shot":
            return ActionResult(False, self.t("allgemein.auswahl_ungueltig"))
        ci = self.db.one("SELECT * FROM checkins WHERE id=? AND team_id=?", (checkin_id, team_id))
        if ci is None or ci["status"] != "open":
            return ActionResult(False, self.t("checkin.nicht_mehr_offen"))
        self.db.run("UPDATE checkins SET status='skipped', mode='shot' WHERE id=?", (checkin_id,))
        return ActionResult(True, self.t("checkin.kurzen_gehen", bar=self.spot(ci["spot_id"]).name))

    # =========================================================================
    # Aktionskarten
    # =========================================================================
    def action_cards(self) -> list:
        return self.db.all("SELECT * FROM action_cards ORDER BY id")

    def add_action_card(self, text: str) -> None:
        if text.strip():
            self.db.run("INSERT INTO action_cards(text) VALUES(?)", (text.strip(),))

    def delete_action_card(self, card_id: int) -> None:
        self.db.run("DELETE FROM action_cards WHERE id=?", (card_id,))

    def open_call(self, team_id: int):
        return self.db.one("SELECT * FROM calls WHERE team_id=? AND status='open' ORDER BY id LIMIT 1", (team_id,))

    def last_checkin(self, team_id: int):
        return self.db.one("SELECT * FROM checkins WHERE team_id=? ORDER BY id DESC LIMIT 1", (team_id,))

    def bar_checkin(self, team_id: int):
        """Aktueller Check-in in einer Kneipe (nicht Bahnhof), sonst None.

        Zählt, solange der Check-in offen ist oder das Foto eingereicht/freigegeben wurde,
        höchstens ACTION_WINDOW_MIN Minuten lang.
        """
        ci = self.last_checkin(team_id)
        if ci is None or ci["spot_id"] not in self.data.fields:
            return None
        if ci["status"] not in ("open", "submitted", "done"):
            return None
        if self.clock() - parse_iso(ci["created_at"]) > timedelta(minutes=self.s.action_window_min):
            return None
        return ci

    def action_buttons(self, team_id: int) -> list[tuple[str, str]]:
        """Button „Aktionskarte ziehen“: nur in einer Kneipe, im laufenden Spiel und mit übrigen Karten."""
        if self.phase != "running" or self.open_call(team_id) or not self.remaining_action_cards(team_id):
            return []
        if self.bar_checkin(team_id) is None:
            return []
        return [(self.t("allgemein.button_aktion"), "aktion")]

    def remaining_action_cards(self, team_id: int) -> list:
        used = {r["card_id"] for r in self.db.all(
            "SELECT card_id FROM calls WHERE team_id=? AND kind='action' AND status<>'cancelled'", (team_id,))}
        return [c for c in self.action_cards() if c["id"] not in used]

    async def request_action(self, team_id: int) -> ActionResult:
        block = self._phase_block()
        if block:
            return ActionResult(False, block)
        if self.open_call(team_id):
            return ActionResult(False, self.t("aktion.schon_offen"))
        if not self.action_cards():
            return ActionResult(False, self.t("aktion.keine_karten"))
        cards = self.remaining_action_cards(team_id)
        if not cards:
            return ActionResult(False, self.t("aktion.alle_gespielt"))
        last = self.bar_checkin(team_id)
        if last is None:
            return ActionResult(False, self.t("aktion.nicht_in_kneipe"))
        card = random.choice(cards)
        self.db.run("INSERT INTO calls(kind,team_id,spot_id,checkin_id,card_id,card_text,requested_at,status) "
                    "VALUES('action',?,?,?,?,?,?,'open')",
                    (team_id, last["spot_id"] if last else None, last["id"] if last else None, card["id"],
                     card["text"], iso(self.clock())))
        cfg = self.s.rules
        mult = self.state().multiplier
        where = Raw(self.t("aktion.wo_zuletzt", bar=self.spot(last["spot_id"]).name)) if last else Raw("")
        return ActionResult(True, self.t("aktion.gezogen", karte=card["text"], plus=cfg.action_success * mult,
                                         minus=cfg.action_fail * mult, wo=where, rest=len(cards) - 1))

    # =========================================================================
    # Videoanrufe für Aktionskarten prüfen
    # =========================================================================
    def open_calls(self) -> list:
        return self.db.all("SELECT * FROM calls WHERE status='open' ORDER BY requested_at, id")

    def open_calls_count(self) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM calls WHERE status='open'")["n"]

    def call_preview(self, call, success: bool) -> list[R.Entry]:
        ev = R.Event(10**9, iso(self.clock()), R.ACTION_DECIDED,
                     {"team": call["team_id"], "success": success, "requested_at": call["requested_at"]})
        return R.preview(self.state(), ev, self.board, self.s.rules)

    async def decide_call(self, call_id: int, admin: str, success: bool) -> ActionResult:
        with self.db.tx() as conn:
            call = conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
            if call is None:
                return ActionResult(False, "Anfrage nicht gefunden.")
            if call["status"] != "open":
                return ActionResult(False, f"Schon erledigt ({call['status']}) von {call['decided_by']}.")
            ev = self._event(conn, admin, R.ACTION_DECIDED, {
                "team": call["team_id"], "success": success, "requested_at": call["requested_at"],
                "call_id": call_id, "card": call["card_text"]})
            conn.execute("UPDATE calls SET status=?, decided_by=?, decided_at=?, event_id=? WHERE id=?",
                         ("success" if success else "fail", admin, iso(self.clock()), ev.id, call_id))
        await self._notify_call(call, ev, success)
        await self._maybe_final()
        return ActionResult(True, "Gespeichert.")

    async def _notify_call(self, call, ev: R.Event, success: bool) -> None:
        st = self.state()
        team = call["team_id"]
        gained = sum(e.delta for e in st.entries_for(ev.id) if e.team == team)
        head = self.t("aktion.geschafft" if success else "aktion.nicht_geschafft", punkte=f"{gained:+d}")
        await self.send_team(team, head + "\n" + self.t("allgemein.punktestand", punkte=st.points_of(team)))

    async def cancel_call(self, call_id: int, admin: str) -> ActionResult:
        with self.db.tx() as conn:
            call = conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
            if call is None or call["status"] != "open":
                return ActionResult(False, "Anfrage ist nicht mehr offen.")
            conn.execute("UPDATE calls SET status='cancelled', decided_by=?, decided_at=? WHERE id=?",
                         (admin, iso(self.clock()), call_id))
        await self.send_team(call["team_id"], self.t("aktion.abgebrochen"))
        await self._maybe_final()
        return ActionResult(True, "Abgebrochen.")

    async def _maybe_final(self) -> None:
        if self.phase == "ended" and self.pending_count() == 0 and self.open_calls_count() == 0:
            await self.announce_final()

    # =========================================================================
    # Ereigniskarten
    # =========================================================================
    def event_cards(self) -> list:
        return self.db.all("SELECT * FROM event_cards ORDER BY offset_min, id")

    def save_event_card(self, card_id: int | None, offset_min: int, kind: str, text: str) -> ActionResult:
        if kind not in EVENT_CARD_KINDS:
            return ActionResult(False, "Unbekannte Art.")
        if not text.strip() or offset_min < 0:
            return ActionResult(False, "Text und Zeit (Minuten ab Start) angeben.")
        if card_id:
            self.db.run("UPDATE event_cards SET offset_min=?, kind=?, text=? WHERE id=?",
                        (offset_min, kind, text.strip(), card_id))
        else:
            self.db.run("INSERT INTO event_cards(offset_min,kind,text) VALUES(?,?,?)",
                        (offset_min, kind, text.strip()))
        return ActionResult(True, "Ereigniskarte gespeichert.")

    def delete_event_card(self, card_id: int) -> None:
        self.db.run("DELETE FROM event_cards WHERE id=?", (card_id,))

    def card_due(self, card) -> datetime | None:
        """Uhrzeit, zu der die Karte fällig ist/war (nach Spielzeit, Pausen verschieben sie)."""
        return self.at_game_time(int(card["offset_min"]))

    def card_is_due(self, card) -> bool:
        elapsed = self.game_elapsed()
        return elapsed is not None and elapsed >= timedelta(minutes=int(card["offset_min"]))

    def card_fired(self, card) -> bool:
        return self.db.get_kv(f"event_card_fired_{card['id']}") is not None

    def pending_event_card(self, team_id: int):
        """Fällige Karte für den nächsten Kneipen-Check-in dieses Teams (älteste zuerst)."""
        for card in self.event_cards():
            if card["kind"] != "next_checkin":
                continue
            if not self.card_is_due(card):
                continue
            used = self.db.one("SELECT id FROM checkins WHERE team_id=? AND event_card_id=? "
                               "AND status IN ('open','submitted','done')", (team_id, card["id"]))
            if not used:
                return card
        return None

    async def _fire_event_cards(self) -> None:
        if self.phase not in ("running", "paused"):
            return
        now = self.clock()
        for card in self.event_cards():
            if card["kind"] == "next_checkin" or self.card_fired(card) or not self.card_is_due(card):
                continue
            due = self.card_due(card) or now
            self.db.set_kv(f"event_card_fired_{card['id']}", iso(now))
            if card["kind"] == "double_points":
                # Zeitstempel = fällige Uhrzeit: Fotos, die ab dann hochgeladen wurden, zählen doppelt.
                with self.db.tx() as conn:
                    self.db.insert_event(conn, iso(due), "system", R.MULTIPLIER_SET,
                                         {"factor": self.s.rules.double_factor, "card_id": card["id"]})
                self.invalidate()
            await self.send_all(self.t("ereignis.an_alle", text=card["text"]))

    # =========================================================================
    # Fotos
    # =========================================================================
    def submit_photo(self, team_id: int, photo_path: str | None, file_id: str | None,
                     file_unique_id: str | None) -> ActionResult | None:
        """None = still bleiben (z.B. weiteres Foto aus einem Album)."""
        ci = self.open_checkin(team_id)
        if ci is None:
            if self.db.one("SELECT id FROM checkins WHERE team_id=? AND status='submitted'", (team_id,)):
                return None
            if self.open_call(team_id):
                return None  # Aktion läuft, Fotos sind dann nur fürs Chatlog
            last_hint = self.db.get_kv(f"photo_hint_{team_id}")
            if last_hint and self.clock() - parse_iso(last_hint) < timedelta(minutes=5):
                return None
            self.db.set_kv(f"photo_hint_{team_id}", iso(self.clock()))
            if self.phase == "ended":
                return ActionResult(False, self.t("foto.spiel_vorbei"))
            return ActionResult(False, self.t("foto.ohne_checkin"))
        if self.phase == "ended":
            return ActionResult(False, self.t("foto.spiel_vorbei"))
        now = self.clock()
        if parse_iso(ci["expires_at"]) < now and self.phase == "running":
            self.db.run("UPDATE checkins SET status='expired' WHERE id=?", (ci["id"],))
            return ActionResult(False, self.t("foto.zu_spaet"))
        live = self.live_snapshot(team_id, self.spot(ci["spot_id"]))
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO submissions(checkin_id,team_id,spot_id,photo_path,file_id,file_unique_id,"
                "submitted_at,status,live_distance_m,live_age_s,live_lat,live_lon) "
                "VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?)",
                (ci["id"], team_id, ci["spot_id"], photo_path, file_id, file_unique_id, iso(now),
                 live["distance_m"] if live else None, live["age_s"] if live else None,
                 live["lat"] if live else None, live["lon"] if live else None))
            conn.execute("UPDATE checkins SET status='submitted' WHERE id=?", (ci["id"],))
        return ActionResult(True, self.t("foto.eingereicht"))

    # =========================================================================
    # Prüfung
    # =========================================================================
    def pending_count(self) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM submissions WHERE status='pending'")["n"]

    def pending_submissions(self) -> list:
        return self.db.all(
            "SELECT s.*, c.distance_m, c.task_text, c.lat, c.lon, c.is_test, c.mode, c.event_text, c.live_distance_m AS checkin_live_distance_m, c.live_age_s AS checkin_live_age_s, c.created_at AS checkin_at "
            "FROM submissions s JOIN checkins c ON c.id=s.checkin_id "
            "WHERE s.status='pending' ORDER BY s.submitted_at, s.id")

    def submission(self, sub_id: int):
        return self.db.one(
            "SELECT s.*, c.distance_m, c.task_text, c.lat, c.lon, c.is_test, c.mode, c.event_text, c.live_distance_m AS checkin_live_distance_m, c.live_age_s AS checkin_live_age_s, c.created_at AS checkin_at "
            "FROM submissions s JOIN checkins c ON c.id=s.checkin_id WHERE s.id=?", (sub_id,))

    def blocked_by(self, sub) -> object | None:
        """Älteres offenes Foto für dasselbe Feld, das zuerst geprüft werden muss."""
        return self.db.one(
            "SELECT * FROM submissions WHERE spot_id=? AND status='pending' AND id<>? "
            "AND (submitted_at < ? OR (submitted_at = ? AND id < ?)) ORDER BY submitted_at LIMIT 1",
            (sub["spot_id"], sub["id"], sub["submitted_at"], sub["submitted_at"], sub["id"]))

    def preview(self, sub, task_done: bool = False) -> list[R.Entry]:
        ev = R.Event(10**9, iso(self.clock()), R.VISIT_APPROVED,
                     {"team": sub["team_id"], "spot": sub["spot_id"], "submitted_at": sub["submitted_at"],
                      "task_done": task_done})
        return R.preview(self.state(), ev, self.board, self.s.rules)

    def describe_entry(self, e: R.Entry) -> str:
        names = self.team_names()
        spot = self.spot(e.spot).name if e.spot else ""
        group = self.data.groups[e.group].name if e.group in self.data.groups else ""
        other = names.get(e.other_team, "?") if e.other_team else ""
        return {
            R.BUY: f"kauft {spot}",
            R.RENT: f"Miete für {spot} von {other}",
            R.STATION_BUY: f"kauft Bahnhof {spot}",
            R.STATION_RENT: f"Bahnhofsgebühr für {spot} von {other}",
            R.VISITOR: f"Besuch bei {other} ({spot})",
            R.TASK: f"Aufgabe in {spot}",
            R.STREET_BONUS: f"Straße {group} komplett",
            R.STREET_LOST: f"Straße {group} nicht mehr komplett",
            R.MANUAL: f"Korrektur: {e.comment or ''}",
            R.ACTION_OK: f"Aktion geschafft: {e.comment or ''}",
            R.ACTION_FAIL: f"Aktion nicht geschafft: {e.comment or ''}",
        }.get(e.kind, e.kind)

    async def approve(self, sub_id: int, admin: str, task_done: bool = False) -> ActionResult:
        with self.db.tx() as conn:
            sub = conn.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
            if sub is None:
                return ActionResult(False, "Einreichung nicht gefunden.")
            if sub["status"] != "pending":
                return ActionResult(False, f"Schon erledigt ({sub['status']}) von {sub['reviewed_by']}.")
            older = self.blocked_by(sub)
            if older is not None:
                return ActionResult(False, f"Erst das ältere Foto von {self.team_name(older['team_id'])} "
                                           f"für dieses Feld prüfen.")
            # Aufgabenbonus nur, wenn es beim Check-in überhaupt eine Aufgabe gab.
            ci = conn.execute("SELECT task_text FROM checkins WHERE id=?", (sub["checkin_id"],)).fetchone()
            task_done = bool(task_done) and bool(ci and ci["task_text"])
            ev = self._event(conn, admin, R.VISIT_APPROVED, {
                "team": sub["team_id"], "spot": sub["spot_id"], "submitted_at": sub["submitted_at"],
                "task_done": bool(task_done), "submission_id": sub["id"]})
            conn.execute("UPDATE submissions SET status='approved', reviewed_by=?, reviewed_at=?, task_done=?, "
                         "event_id=? WHERE id=?", (admin, iso(self.clock()), int(bool(task_done)), ev.id, sub_id))
            conn.execute("UPDATE checkins SET status='done' WHERE id=?", (sub["checkin_id"],))
        await self._notify_visit(ev)
        await self._maybe_final()
        return ActionResult(True, "Freigegeben.")

    async def _notify_visit(self, ev: R.Event) -> None:
        st = self.state()
        entries = st.entries_for(ev.id)
        team = int(ev.data["team"])
        sp = self.spot(ev.data["spot"])
        names = self.team_names()
        T = self.t
        cfg = self.s.rules
        lines = [T("foto.akzeptiert", bar=sp.name)]
        owner_msgs: dict[int, list[str]] = {}
        street_done: list[tuple[int, str]] = []
        own = {R.BUY: "foto.bar_gehoert_euch", R.STATION_BUY: "foto.bahnhof_gehoert_euch",
               R.VISITOR: "foto.vorbeischauen", R.TASK: "foto.aufgabe_erfuellt"}
        for e in entries:
            gname = self.data.groups[e.group].name if e.group in self.data.groups else ""
            if e.team == team:
                if e.kind in own:
                    lines.append(T(own[e.kind], punkte=e.delta))
                elif e.kind == R.STREET_BONUS:
                    lines.append(T("foto.strasse_komplett", strasse=gname, punkte=e.delta))
                    street_done.append((team, gname))
                elif e.kind == R.STREET_LOST:
                    lines.append(T("foto.strasse_verloren", strasse=gname, punkte=e.delta))
            elif e.kind in (R.RENT, R.STATION_RENT):
                if self.hide_owners():
                    lines.append(T("foto.besitzer_kassiert_geheim", punkte=e.delta))
                else:
                    lines.append(T("foto.besitzer_kassiert", team=names.get(e.team, "?"), punkte=e.delta))
                full = (e.kind == R.RENT and e.group is not None and cfg.points_owner_street != cfg.points_owner
                        and R.street_owner(st, self.board, e.group) == e.team)
                owner_msgs.setdefault(e.team, []).append(
                    T("foto.an_besitzer_strasse" if full else "foto.an_besitzer",
                      team=names.get(team, "?"), bar=sp.name, punkte=e.delta))
        if sp.kind == "field" and st.owners.get(sp.id) == team and not any(e.kind == R.BUY for e in entries):
            lines.append(T("foto.schon_eure_bar"))
        lines += ["", T("allgemein.punktestand", punkte=st.points_of(team))]
        await self.send_team(team, "\n".join(lines))
        for owner, msgs in owner_msgs.items():
            await self.send_team(owner, "\n".join(msgs + [T("allgemein.punktestand", punkte=st.points_of(owner))]))
        if self.s.announce_street_complete and not self.hide_owners():
            for t, gname in street_done:
                await self.send_all(T("foto.strasse_info_alle", team=names.get(t, "?"), strasse=gname), exclude={t})

    async def reject(self, sub_id: int, admin: str, reason: str) -> ActionResult:
        reason = (reason or "").strip() or self.t("foto.kein_grund")
        reopen = False
        with self.db.tx() as conn:
            sub = conn.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
            if sub is None:
                return ActionResult(False, "Einreichung nicht gefunden.")
            if sub["status"] != "pending":
                return ActionResult(False, f"Schon erledigt ({sub['status']}) von {sub['reviewed_by']}.")
            conn.execute("UPDATE submissions SET status='rejected', reviewed_by=?, reviewed_at=?, reason=? "
                         "WHERE id=?", (admin, iso(self.clock()), reason, sub_id))
            other_open = conn.execute("SELECT id FROM checkins WHERE team_id=? AND status='open' AND id<>?",
                                      (sub["team_id"], sub["checkin_id"])).fetchone()
            if self.phase != "ended" and other_open is None:
                expires = self.clock() + timedelta(minutes=self.s.checkin_timeout_min)
                conn.execute("UPDATE checkins SET status='open', expires_at=? WHERE id=?",
                             (iso(expires), sub["checkin_id"]))
                reopen = True
            else:
                conn.execute("UPDATE checkins SET status='expired' WHERE id=?", (sub["checkin_id"],))
        sp = self.spot(sub["spot_id"])
        text = self.t("foto.abgelehnt", bar=sp.name, grund=reason)
        if reopen:
            ci = self.db.one("SELECT expires_at FROM checkins WHERE id=?", (sub["checkin_id"],))
            text += "\n" + self.t("foto.abgelehnt_neues_foto", uhrzeit=self.hm(ci["expires_at"]))
        elif self.phase != "ended":
            text += "\n" + self.t("foto.abgelehnt_woanders")
        await self.send_team(sub["team_id"], text)
        await self._maybe_final()
        return ActionResult(True, "Abgelehnt.")

    # =========================================================================
    # Korrekturen
    # =========================================================================
    async def _notify_correction(self, before: R.State, what: str) -> None:
        after = self.state()
        for t in self.teams():
            old, new = before.points_of(t["id"]), after.points_of(t["id"])
            if old != new:
                await self.send_team(t["id"], self.t("korrektur.punkte", was=what, alt=old, neu=new))

    def _comment(self, comment: str) -> Raw:
        return Raw(self.t("korrektur.kommentar", text=comment)) if comment else Raw("")

    def event(self, event_id: int) -> R.Event | None:
        for e in self.db.events():
            if e.id == event_id:
                return e
        return None

    def revoked_ids(self) -> set[int]:
        return {int(e.data["event_id"]) for e in self.db.events() if e.type == R.EVENT_REVOKED}

    async def revoke(self, event_id: int, admin: str, comment: str = "") -> ActionResult:
        ev = self.event(event_id)
        if ev is None:
            return ActionResult(False, "Ereignis nicht gefunden.")
        if ev.type not in R.REVOCABLE_EVENTS:
            return ActionResult(False, "Dieses Ereignis lässt sich nicht zurücknehmen.")
        if event_id in self.revoked_ids():
            return ActionResult(False, "Schon zurückgenommen.")
        before = self.state()
        with self.db.tx() as conn:
            self._event(conn, admin, R.EVENT_REVOKED, {"event_id": event_id, "comment": comment})
            if ev.type == R.VISIT_APPROVED and ev.data.get("submission_id"):
                conn.execute("UPDATE submissions SET status='revoked', reviewed_by=?, reviewed_at=?, reason=? "
                             "WHERE id=?", (admin, iso(self.clock()), comment or "zurückgenommen",
                                            ev.data["submission_id"]))
            if ev.type == R.ACTION_DECIDED and ev.data.get("call_id"):
                conn.execute("UPDATE calls SET status='revoked' WHERE id=?", (ev.data["call_id"],))
        what = self.describe_event(ev)
        await self._notify_correction(before, f"„{what}“ zurückgenommen" + (f" ({comment})" if comment else ""))
        if ev.type == R.VISIT_APPROVED:
            team = int(ev.data["team"])
            if before.points_of(team) == self.state().points_of(team):
                await self.send_team(team, self.t("korrektur.freigabe_zurueck", bar=self.spot(ev.data["spot"]).name,
                                                  kommentar=self._comment(comment)))
        return ActionResult(True, "Zurückgenommen, Stand neu berechnet.")

    async def set_owner(self, spot_id: str, team_id: int | None, admin: str, comment: str) -> ActionResult:
        if not self.data.is_spot(spot_id):
            return ActionResult(False, "Unbekanntes Feld.")
        before = self.state()
        self.add_event(admin, R.OWNER_SET, {"spot": spot_id, "team": team_id, "comment": comment})
        new_owner = self.team_name(team_id) if team_id else self.t("korrektur.niemand")
        await self._notify_correction(before, f"{self.spot(spot_id).name} → {new_owner}"
                                              + (f" ({comment})" if comment else ""))
        for t in {before.owners.get(spot_id), team_id} - {None}:
            if before.points_of(t) == self.state().points_of(t):
                await self.send_team(t, self.t("korrektur.besitzer", bar=self.spot(spot_id).name, team=new_owner,
                                               kommentar=self._comment(comment)))
        return ActionResult(True, "Besitzer geändert.")

    async def adjust_points(self, team_id: int, delta: int, admin: str, comment: str) -> ActionResult:
        if not comment.strip():
            return ActionResult(False, "Bitte einen Kommentar angeben.")
        if delta == 0:
            return ActionResult(False, "0 Punkte ändern nichts.")
        before = self.state()
        self.add_event(admin, R.POINTS_ADJUSTED, {"team": team_id, "delta": delta, "comment": comment})
        await self._notify_correction(before, f"{delta:+d} Punkte ({comment})")
        return ActionResult(True, "Punkte gebucht.")

    async def deactivate_spot(self, spot_id: str, admin: str) -> ActionResult:
        if spot_id in self.state().inactive:
            return ActionResult(False, "Ist schon deaktiviert.")
        before = self.state()
        name = self.spot(spot_id).name
        self.add_event(admin, R.FIELD_DEACTIVATED, {"spot": spot_id})
        await self.send_all(self.t("korrektur.deaktiviert", bar=name))
        await self._notify_correction(before, f"{name} deaktiviert")
        return ActionResult(True, f"{name} deaktiviert.")

    async def reactivate_spot(self, spot_id: str, admin: str) -> ActionResult:
        if spot_id not in self.state().inactive:
            return ActionResult(False, "Ist nicht deaktiviert.")
        before = self.state()
        self.add_event(admin, R.FIELD_REACTIVATED, {"spot": spot_id})
        name = self.spot(spot_id).name
        await self.send_all(self.t("korrektur.reaktiviert", bar=name))
        await self._notify_correction(before, f"{name} wieder aktiv")
        return ActionResult(True, f"{name} wieder aktiv.")

    async def replace_spot(self, spot_id: str, reserve_id: str | None, admin: str) -> ActionResult:
        if spot_id not in self.data.fields:
            return ActionResult(False, "Nur Bars lassen sich ersetzen.")
        if reserve_id and reserve_id not in self.data.reserve:
            return ActionResult(False, "Unbekannte Reserve-Bar.")
        if reserve_id:
            used = {r: s for s, r in self.state().replaced.items()}
            if reserve_id in used and used[reserve_id] != spot_id:
                return ActionResult(False, "Diese Reserve-Bar ersetzt schon ein anderes Feld.")
        old = self.spot(spot_id).name
        before = self.state()
        self.add_event(admin, R.FIELD_REPLACED, {"spot": spot_id, "reserve": reserve_id})
        new = self.spot(spot_id)
        await self.send_all(self.t("korrektur.ersetzt", alt=old, neu=new.name, adresse=new.address,
                                   strasse=new.group_name))
        await self._notify_correction(before, f"{old} ersetzt durch {new.name}")
        return ActionResult(True, f"{old} → {new.name}.")

    # =========================================================================
    # Hintergrund: Timeouts und Endzeit
    # =========================================================================
    async def sweep(self) -> None:
        await self._fire_event_cards()
        now = self.clock()
        if self.phase == "running":
            expired = self.db.all("SELECT * FROM checkins WHERE status='open' AND expires_at < ?", (iso(now),))
            for c in expired:
                self.db.run("UPDATE checkins SET status='expired' WHERE id=? AND status='open'", (c["id"],))
                await self.send_team(c["team_id"], self.t("checkin.abgelaufen", bar=self.spot(c["spot_id"]).name))
        # Ende und Warnungen nach Spielzeit. In der Pause steht die Uhr, daher nur im laufenden Spiel.
        left = self.game_remaining()
        if left is not None and self.phase == "running":
            end = self.end_time()
            if left <= timedelta(0):
                await self.end_game("system")
            elif left <= timedelta(minutes=10) and not self.db.get_kv("warned_10"):
                self.db.set_kv("warned_10", "1")
                self.db.set_kv("warned_30", "1")
                await self.send_all(self.t("spiel.noch_10_min", uhrzeit=self.hm(end)))
            elif left <= timedelta(minutes=30) and not self.db.get_kv("warned_30"):
                self.db.set_kv("warned_30", "1")
                await self.send_all(self.t("spiel.noch_30_min", uhrzeit=self.hm(end)))

    # =========================================================================
    # Texte für Spieler
    # =========================================================================
    def rules_text(self) -> str:
        c = self.s.rules
        T = self.t
        dur = self.duration_min()
        end = self.end_time() if self.phase in ("running", "paused") else None
        if dur and end:
            spielzeit = Raw(T("regeln.endzeit", dauer=fmt_duration(dur), uhrzeit=self.hm(end)))
        elif dur:
            spielzeit = Raw(T("regeln.spielzeit", dauer=fmt_duration(dur)))
        else:
            spielzeit = Raw("")
        return T(
            "regeln.text",
            frist=self.s.checkin_timeout_min, frei=c.points_free_field, miete=c.points_owner,
            miete_strasse=c.points_owner_street,
            besucher=Raw(T("regeln.besucher", punkte=c.points_visitor)) if c.points_visitor else Raw(""),
            strassenbonus=c.street_complete_bonus,
            bahnhof_punkte=", ".join(str(p) for p in c.station_points),
            aufgabe=c.task_bonus, aktion_plus=c.action_success, aktion_minus=c.action_fail,
            abklingzeit=self.s.cooldown_min,
            endzeit=spielzeit,
        )

    def help_text(self) -> str:
        text = self.t("hilfe.text")
        if self.test_mode():
            text += "\n\n" + self.t("testmodus.hilfe", kuerzel="/".join(self.station_letters()))
        return text

    def stand_text(self, team_id: int) -> str:
        T = self.t
        st = self.state()
        names = self.team_names()
        lines = []
        if self.scores_hidden() and self.phase != "ended":
            lines.append(T("stand.geheim", punkte=st.points_of(team_id)))
        else:
            lines.append(T("stand.kopf"))
            for place, tid, pts in R.ranking(st.points, list(names)):
                lines.append(T("stand.zeile_ich" if tid == team_id else "stand.zeile",
                               platz=place, team=names[tid], punkte=pts))
        mine = [self.spot(s) for s in st.spots_of(team_id)]
        lines.append("")
        if mine:
            lines.append(T("stand.eure_felder"))
            for sp in sorted(mine, key=lambda x: (x.kind, x.no or 0)):
                if sp.kind == "station":
                    art = T("stand.bahnhof")
                elif sp.group and st.street_holder.get(sp.group) == team_id:
                    art = T("stand.strasse_komplett", strasse=sp.group_name)
                else:
                    art = esc(sp.group_name)
                lines.append(T("stand.feld", feld=sp.name, art=Raw(art)))
        else:
            lines.append(T("stand.keine_felder"))
        ci = self.open_checkin(team_id)
        if ci:
            lines += ["", T("stand.offener_checkin", bar=self.spot(ci["spot_id"]).name,
                            uhrzeit=self.hm(ci["expires_at"]))]
        pend = self.db.one("SELECT COUNT(*) AS n FROM submissions WHERE team_id=? AND status='pending'", (team_id,))
        if pend["n"]:
            lines.append(T("stand.fotos_in_pruefung", anzahl=pend["n"]))
        if self.open_call(team_id):
            lines.append(T("stand.wartet_auf_anruf"))
        if self.action_cards():
            lines.append(T("stand.aktionskarten_uebrig", anzahl=len(self.remaining_action_cards(team_id))))
        if st.multiplier != 1:
            lines.append(T("stand.multiplikator", faktor=st.multiplier))
        left = self.game_remaining()
        if left is not None and self.phase in ("running", "paused"):
            lines += ["", T("stand.restzeit", rest=fmt_duration(max(0, int(left.total_seconds() // 60))),
                            uhrzeit=self.hm(self.end_time()))]
        return "\n".join(lines)

    def hide_owners(self) -> bool:
        """Bei verdecktem Punktestand bleibt bis Spielende auch geheim, wem was gehört."""
        return self.scores_hidden() and self.phase != "ended"

    def felder_text(self) -> str:
        T = self.t
        st = self.state()
        names = self.team_names()
        hidden = self.hide_owners()

        def status(spot_id: str, active: bool) -> Raw:
            if hidden:
                return Raw(T("felder.geschlossen") if not active else "")
            owner = st.owners.get(spot_id)
            s = T("felder.besitzer", team=names.get(owner, "?")) if owner else T("felder.frei")
            return Raw(s + (T("felder.geschlossen") if not active else ""))

        zeile = "felder.zeile_geheim" if hidden else "felder.zeile"
        bahnhof_zeile = "felder.bahnhof_zeile_geheim" if hidden else "felder.bahnhof_zeile"
        lines = [T("felder.kopf")]
        if hidden:
            lines.append(T("felder.geheim"))
        for g in self.data.groups.values():
            holder = None if hidden else st.street_holder.get(g.id)
            lines.append("")
            lines.append(T("felder.strasse_komplett", strasse=g.name, team=names.get(holder, "?")) if holder
                         else T("felder.strasse", strasse=g.name))
            for fid in g.field_ids:
                sp = self.spot(fid, st)
                lines.append(T(zeile, nummer=sp.no, bar=sp.name, status=status(fid, sp.active)))
        lines += ["", T("felder.bahnhoefe")]
        letter_of = {sid: letter for letter, sid in self.station_letters().items()}
        for sid in self.data.stations:
            sp = self.spot(sid, st)
            lines.append(T(bahnhof_zeile, kuerzel=letter_of.get(sid, "?"), bahnhof=sp.name,
                           status=status(sid, sp.active)))
        if self.test_mode():
            lines += ["", T("testmodus.felder")]
        return "\n".join(lines)

    # =========================================================================
    # Protokoll, Export, Datenschutz
    # =========================================================================
    def describe_event(self, ev: R.Event) -> str:
        d = ev.data
        sp = lambda: self.spot(d["spot"]).name if d.get("spot") and self.data.is_spot(d["spot"]) else d.get("spot")
        t = ev.type
        if t == R.GAME_STARTED:
            return "Spiel gestartet"
        if t == R.GAME_PAUSED:
            return "Spiel pausiert"
        if t == R.GAME_RESUMED:
            return "Spiel fortgesetzt"
        if t == R.GAME_ENDED:
            return "Spiel beendet"
        if t == R.VISIT_APPROVED:
            return f"{self.team_name(d['team'])} besucht {sp()}" + (" (Aufgabe ✓)" if d.get("task_done") else "")
        if t == R.EVENT_REVOKED:
            return f"Ereignis #{d['event_id']} zurückgenommen" + (f": {d.get('comment')}" if d.get("comment") else "")
        if t == R.OWNER_SET:
            who = self.team_name(d["team"]) if d.get("team") else "niemand"
            return f"Besitzer {sp()} → {who}" + (f" ({d.get('comment')})" if d.get("comment") else "")
        if t == R.POINTS_ADJUSTED:
            return f"{self.team_name(d['team'])} {int(d['delta']):+d} Punkte ({d.get('comment', '')})"
        if t == R.FIELD_DEACTIVATED:
            return f"{sp()} deaktiviert"
        if t == R.FIELD_REACTIVATED:
            return f"{sp()} reaktiviert"
        if t == R.MULTIPLIER_SET:
            return f"Ereigniskarte: ab jetzt {d.get('factor')}-fache Punkte"
        if t == R.ACTION_DECIDED:
            return (f"{self.team_name(d['team'])}: Aktion {'geschafft' if d.get('success') else 'nicht geschafft'}"
                    + (f" ({d.get('card')})" if d.get("card") else ""))
        if t == R.FIELD_REPLACED:
            base = self.data.fields[d["spot"]].name if d["spot"] in self.data.fields else d["spot"]
            new = self.data.reserve[d["reserve"]].name if d.get("reserve") in self.data.reserve else base
            return f"{base} ersetzt durch {new}"
        return t

    def event_rows(self) -> list[dict]:
        events = self.db.events()
        revoked = {int(e.data["event_id"]) for e in events if e.type == R.EVENT_REVOKED}
        st = self.state()
        out = []
        for e in reversed(events):
            out.append({
                "id": e.id, "ts": e.ts, "time": self.hm(e.ts), "actor": e.actor, "type": e.type,
                "text": self.describe_event(e), "revoked": e.id in revoked,
                "revocable": e.type in R.REVOCABLE_EVENTS and e.id not in revoked,
                "entries": [(self.team_name(x.team), x.delta, self.describe_entry(x)) for x in st.entries_for(e.id)],
                "data": e.data,
            })
        return out

    def recent_entries(self, limit: int = 15) -> list[dict]:
        st = self.state()
        events = {e.id: e for e in self.db.events()}
        rows = []
        for x in reversed(st.ledger[-limit:]):
            ev = events.get(x.event_id)
            rows.append({"time": self.hm(ev.ts) if ev else "", "team": self.team_name(x.team),
                         "delta": x.delta, "text": self.describe_entry(x)})
        return rows

    def standings(self) -> list[dict]:
        st = self.state()
        teams = self.teams()
        by_id = {t["id"]: t for t in teams}
        out = []
        for place, tid, pts in R.ranking(st.points, list(by_id)):
            t = by_id[tid]
            spots = st.spots_of(tid)
            out.append({
                "place": place, "id": tid, "name": t["name"], "color": t["color"], "points": pts,
                "fields": [self.spot(s).name for s in spots if s in self.data.fields],
                "stations": [self.spot(s).name for s in spots if s in self.data.stations],
                "streets": [self.data.groups[g].name for g, h in st.street_holder.items() if h == tid],
                "linked": t["chat_id"] is not None,
            })
        return out

    def export_json(self) -> str:
        st = self.state()
        payload = {
            "exported_at": iso(self.clock()),
            "phase": st.phase,
            "standings": self.standings(),
            "owners": {s: self.team_name(t) for s, t in st.owners.items()},
            "events": [{"id": e.id, "ts": e.ts, "actor": e.actor, "type": e.type, "data": e.data,
                        "text": self.describe_event(e)} for e in self.db.events()],
            "ledger": [{"event_id": x.event_id, "team": self.team_name(x.team), "delta": x.delta,
                        "kind": x.kind, "text": self.describe_entry(x)} for x in st.ledger],
            "calls": [dict(r) for r in self.db.all(
                "SELECT id, kind, team_id, spot_id, card_text, requested_at, status, decided_by, decided_at "
                "FROM calls ORDER BY id")],
            "submissions": [dict(r) for r in self.db.all(
                "SELECT id, team_id, spot_id, submitted_at, status, reviewed_by, reviewed_at, reason, task_done "
                "FROM submissions ORDER BY id")],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def export_standings_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Platz", "Team", "Punkte", "Bars", "Bahnhöfe", "Straßen komplett"])
        for r in self.standings():
            w.writerow([r["place"], r["name"], r["points"], ", ".join(r["fields"]), ", ".join(r["stations"]),
                        ", ".join(r["streets"])])
        return buf.getvalue()

    def export_events_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["ID", "Zeit (lokal)", "Admin", "Typ", "Beschreibung", "Zurückgenommen", "Buchungen", "Daten"])
        for r in reversed(self.event_rows()):
            local = self.local(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            bookings = "; ".join(f"{t} {d:+d} {txt}" for t, d, txt in r["entries"])
            w.writerow([r["id"], local, r["actor"], r["type"], r["text"], "ja" if r["revoked"] else "",
                        bookings, json.dumps(r["data"], ensure_ascii=False)])
        return buf.getvalue()

    # =========================================================================
    # Spielkarte als Bild (Telegram + Vorschau)
    # =========================================================================
    def map_spec(self) -> MapSpec:
        """Kartenbeschreibung aus den aktiven Spieldaten (inkl. ersetzter/geschlossener Bars). Ohne Besitzer."""
        st = self.state()
        letters = {sid: letter for letter, sid in self.station_letters().items()}
        groups = []
        for g in self.data.groups.values():
            fields = []
            for fid in g.field_ids:
                sp = self.spot(fid, st)
                fields.append(MapField(sp.no, sp.name, sp.lat, sp.lon, sp.address, sp.is_kneipe, sp.active))
            groups.append(MapGroup(g.name, g.color, fields))
        stations = []
        for sid in self.data.stations:
            sp = self.spot(sid, st)
            stations.append(MapStation(letters.get(sid, "?"), sp.name, sp.address, sp.lat, sp.lon, sp.active))
        # Reserve-Bars nur, wenn sie ein Feld ersetzen – dann stecken sie schon in `groups`.
        # Nicht aktive Reserve erscheint nicht auf der Karte.
        return MapSpec(self.data.game, groups, stations, [], list(self.data.boundary))

    def map_signature(self, spec: MapSpec | None = None) -> str:
        import hashlib
        from dataclasses import asdict
        raw = json.dumps(asdict(spec or self.map_spec()), ensure_ascii=False, sort_keys=True) + str(self.s.map_tiles)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def map_status(self) -> dict:
        path = self.s.map_image_path
        info = json.loads(self.db.get_kv("map_image_info") or "{}")
        current = self.map_signature()
        return {
            "exists": path.exists(),
            "signature": current,
            "outdated": path.exists() and self.db.get_kv("map_image_hash") != current,
            "generated": self.hm(info["at"]) if info.get("at") else None,
            "tiles": info.get("tiles"),
            "size": f"{info.get('width')}×{info.get('height')}" if info.get("width") else None,
        }

    async def ensure_map_image(self, force: bool = False) -> Path:
        """Kartenbild aktuell halten: nur neu zeichnen, wenn sich Kneipen/Straßen geändert haben."""
        path = self.s.map_image_path
        spec = self.map_spec()
        sig = self.map_signature(spec)
        info = json.loads(self.db.get_kv("map_image_info") or "{}")
        fresh = path.exists() and self.db.get_kv("map_image_hash") == sig
        tiles_missing = self.s.map_tiles and info.get("tiles") is False   # letzter Versuch ohne Internet
        if fresh and not force and not tiles_missing:
            return path
        result = await asyncio.to_thread(render_map, spec, path, self.s.tile_cache_dir, self.s.map_tiles)
        result["at"] = iso(self.clock())
        self.db.set_kv("map_image_hash", sig)
        self.db.set_kv("map_image_info", json.dumps(result))
        return path

    # =========================================================================
    # Setup exportieren / importieren (z.B. vom Laptop auf den Server)
    # =========================================================================
    def game_data_raw(self) -> dict:
        """Aktive Spieldaten (Kneipen, Bahnhöfe, Reserve) im Format der Spieldaten-JSON."""
        with self.s.active_game_data_path.open(encoding="utf-8") as fh:
            return json.load(fh)

    def game_data_source(self) -> str:
        p = self.s.imported_game_data_path
        if p.exists():
            when = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
            return f"importiert am {self.local(when).strftime('%d.%m.%Y %H:%M')}"
        return f"aus {self.s.game_data_path.name} (Repository)"

    def export_setup(self) -> dict:
        """Alles, was man vor dem Spiel einrichtet. Ohne Spielstand, Chats, Fotos und Geheimnisse."""
        return {
            "format": SETUP_FORMAT,
            "version": 1,
            "exported_at": iso(self.clock()),
            "teams": [{"name": t["name"], "color": t["color"], "code": t["code"]} for t in self.teams()],
            "action_cards": [c["text"] for c in self.action_cards()],
            "event_cards": [{"offset_min": c["offset_min"], "kind": c["kind"], "text": c["text"]}
                            for c in self.event_cards()],
            "tasks": self.tasks(),
            "random_tasks": [r["text"] for r in self.random_tasks()],
            "settings": {"duration_min": self.duration_min(), "scores_hidden": self.scores_hidden()},
            "spieldaten": self.game_data_raw(),
        }

    def _reload_game_data(self, data: GameData) -> None:
        self.data = data
        self.board = data.board
        self.invalidate()

    def apply_game_data(self, raw: dict) -> ActionResult:
        """Spieldaten prüfen, unter data/spieldaten.json speichern und sofort übernehmen."""
        try:
            data = parse_game_data(raw)
        except (GameDataError, KeyError, TypeError, ValueError) as e:
            return ActionResult(False, f"Spieldaten fehlerhaft: {e}")
        target = self.s.imported_game_data_path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
        self._reload_game_data(data)
        # Aufgaben zu Feldern, die es nicht mehr gibt, entfernen
        dropped = [s for s in self.tasks() if not data.is_spot(s)]
        for s in dropped:
            self.db.run("DELETE FROM tasks WHERE spot_id=?", (s,))
        return ActionResult(True, f"{len(data.fields)} Kneipen, {len(data.stations)} Bahnhöfe, "
                                  f"{len(data.reserve)} Reserve-Bars übernommen"
                                  + (f", {len(dropped)} verwaiste Aufgabe(n) entfernt" if dropped else ""))

    # --- Karten-Editor ---------------------------------------------------------
    def editor_save(self, raw: dict, admin: str) -> tuple[ActionResult, list[str]]:
        """Speichert die Spieldaten aus dem Karten-Editor. Rückgabe: (Ergebnis, Hinweise).

        Vor dem Spielstart ist alles erlaubt. Während des Spiels nur Namen, Adressen,
        Koordinaten, Typ und Farben, damit Besitz und Punkte stimmig bleiben.
        """
        try:
            raw = normalize_game_data(raw)
        except (ValueError, TypeError, KeyError) as e:
            return ActionResult(False, f"Ungültige Daten: {e}"), []
        if self.phase != "setup":
            current = normalize_game_data(self.game_data_raw())
            if structure_of(raw) != structure_of(current):
                return ActionResult(False, "Während des Spiels lassen sich nur Namen, Adressen, Koordinaten, Typ "
                                           "und Farben ändern. Hinzufügen, Löschen und Straßen umsortieren geht "
                                           "nur in der Vorbereitung."), []
        notes = []
        letters: dict[str, str] = {}
        for s in raw.get("stations", []):
            letter = s["name"][:1].upper()
            if letter in letters:
                notes.append(f"Bahnhöfe „{letters[letter]}“ und „{s['name']}“ beginnen beide mit {letter}. "
                             f"Im Testmodus (/checkin {letter}) ist nur der erste erreichbar.")
            letters.setdefault(letter, s["name"])
        empty = [g["name"] for g in raw["groups"] if not g["field_ids"]]
        if empty:
            notes.append("Straße ohne Kneipen: " + ", ".join(empty))
        self._backup_game_data()
        res = self.apply_game_data(raw)
        if res.ok:
            log.info("Karten-Editor: Spieldaten gespeichert von %s", admin)
            res = ActionResult(True, "Gespeichert: " + res.message)
        return res, notes

    def _backup_game_data(self) -> None:
        src = self.s.imported_game_data_path
        if not src.exists():
            return
        bdir = self.s.data_dir / "spieldaten-backups"
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = self.clock().astimezone(self.s.tz).strftime("%Y%m%d-%H%M%S")
        shutil.copyfile(src, bdir / f"spieldaten-{stamp}.json")
        for old in sorted(bdir.glob("spieldaten-*.json"))[:-20]:   # die letzten 20 behalten
            old.unlink(missing_ok=True)

    def reset_game_data(self) -> ActionResult:
        """Importierte Spieldaten verwerfen, wieder die Datei aus dem Repository nutzen."""
        if self.phase != "setup":
            return ActionResult(False, "Nur in der Vorbereitung möglich (vor Spielstart oder nach dem Zurücksetzen).")
        p = self.s.imported_game_data_path
        if not p.exists():
            return ActionResult(False, "Es sind keine importierten Spieldaten aktiv.")
        from .gamedata import load_game_data
        try:
            data = load_game_data(self.s.game_data_path)
        except GameDataError as e:
            return ActionResult(False, f"Repository-Spieldaten fehlerhaft: {e}")
        p.unlink()
        self._reload_game_data(data)
        return ActionResult(True, "Importierte Spieldaten verworfen, es gelten wieder die aus dem Repository.")

    def import_setup(self, payload: dict, parts: set[str]) -> ActionResult:
        """Importiert die gewählten Teile. Erlaubt nur in der Vorbereitung, damit kein Spielstand kaputtgeht.

        `payload` ist ein Setup-Export oder eine reine Spieldaten-JSON (Kneipen auf der Karte).
        Teile: spieldaten, teams, aktionskarten, ereigniskarten, aufgaben, einstellungen.
        """
        if self.phase != "setup":
            return ActionResult(False, "Import nur in der Vorbereitung: vor dem Spielstart oder nach "
                                       "„Spiel zurücksetzen“.")
        if not isinstance(payload, dict):
            return ActionResult(False, "Die Datei enthält kein gültiges Setup.")
        if payload.get("format") != SETUP_FORMAT:
            if "fields" in payload and "groups" in payload:      # reine Spieldaten-Datei
                payload = {"spieldaten": payload}
                parts = {"spieldaten"}
            else:
                return ActionResult(False, "Unbekanntes Dateiformat. Erwartet wird ein Setup-Export oder "
                                           "eine Spieldaten-JSON.")
        done: list[str] = []
        if "spieldaten" in parts and payload.get("spieldaten"):
            res = self.apply_game_data(payload["spieldaten"])
            if not res.ok:
                return res
            done.append(res.message)
        with self.db.tx() as conn:
            if "teams" in parts and "teams" in payload:
                added = updated = 0
                notes = []
                for t in payload["teams"]:
                    name = str(t.get("name", "")).strip()
                    if not name:
                        continue
                    color = t.get("color") or "#888888"
                    code = str(t.get("code") or "").strip().upper()
                    row = conn.execute("SELECT id FROM teams WHERE name=?", (name,)).fetchone()
                    clash = conn.execute("SELECT id FROM teams WHERE code=? AND name<>?", (code, name)).fetchone() \
                        if code else None
                    if clash:
                        notes.append(f"Code von {name} schon vergeben")
                        code = ""
                    if row:
                        conn.execute("UPDATE teams SET color=?" + (", code=?" if code else "") + " WHERE id=?",
                                     (color, code, row["id"]) if code else (color, row["id"]))
                        updated += 1
                    else:
                        code = code or "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))
                        conn.execute("INSERT INTO teams(name,color,code,created_at) VALUES(?,?,?,?)",
                                     (name, color, code, iso(self.clock())))
                        added += 1
                done.append(f"Teams: {added} neu, {updated} aktualisiert" + (f" ({'; '.join(notes)})" if notes else ""))
            if "aktionskarten" in parts and "action_cards" in payload:
                conn.execute("DELETE FROM action_cards")
                for text in payload["action_cards"]:
                    if str(text).strip():
                        conn.execute("INSERT INTO action_cards(text) VALUES(?)", (str(text).strip(),))
                done.append(f"{len(payload['action_cards'])} Aktionskarte(n)")
            if "ereigniskarten" in parts and "event_cards" in payload:
                conn.execute("DELETE FROM event_cards")
                conn.execute("DELETE FROM kv WHERE key LIKE 'event_card_fired_%'")
                n = 0
                for c in payload["event_cards"]:
                    if c.get("kind") in EVENT_CARD_KINDS and str(c.get("text", "")).strip():
                        conn.execute("INSERT INTO event_cards(offset_min,kind,text) VALUES(?,?,?)",
                                     (int(c.get("offset_min", 0)), c["kind"], str(c["text"]).strip()))
                        n += 1
                conn.execute("INSERT INTO kv(key,value) VALUES('event_cards_seeded','1') "
                             "ON CONFLICT(key) DO UPDATE SET value='1'")
                done.append(f"{n} Ereigniskarte(n)")
            if "aufgaben" in parts and ("tasks" in payload or "random_tasks" in payload):
                conn.execute("DELETE FROM tasks")
                kept = skipped = 0
                for spot, text in (payload.get("tasks") or {}).items():
                    if self.data.is_spot(spot) and str(text).strip():
                        conn.execute("INSERT INTO tasks(spot_id,text) VALUES(?,?)", (spot, str(text).strip()))
                        kept += 1
                    else:
                        skipped += 1
                conn.execute("DELETE FROM random_tasks")
                for text in payload.get("random_tasks") or []:
                    if str(text).strip():
                        conn.execute("INSERT INTO random_tasks(text) VALUES(?)", (str(text).strip(),))
                done.append(f"{kept} Feldaufgabe(n), {len(payload.get('random_tasks') or [])} Zufallsaufgabe(n)"
                            + (f", {skipped} ohne passendes Feld übersprungen" if skipped else ""))
        if "einstellungen" in parts and isinstance(payload.get("settings"), dict):
            st = payload["settings"]
            dur = st.get("duration_min")
            self.db.set_kv("duration_min", str(int(dur)) if dur else "")
            if "scores_hidden" in st:
                self.set_scores_hidden(bool(st["scores_hidden"]))
            done.append("Einstellungen (Spieldauer, Punktestand verdeckt)")
        if not done:
            return ActionResult(False, "Nichts importiert: keiner der gewählten Teile ist in der Datei enthalten.")
        return ActionResult(True, "Import fertig: " + "; ".join(done) + ".")

    def purge_private_data(self) -> ActionResult:
        """Löscht Fotos, Chatverläufe und Standorte. Spielstand und Protokoll bleiben."""
        with self.db.tx() as conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM live_positions")
            conn.execute("UPDATE checkins SET lat=NULL, lon=NULL, live_distance_m=NULL, live_age_s=NULL")
            conn.execute("UPDATE submissions SET photo_path=NULL, file_id=NULL, file_unique_id=NULL, "
                         "live_lat=NULL, live_lon=NULL, live_distance_m=NULL, live_age_s=NULL")
        if self.s.photo_dir.exists():
            shutil.rmtree(self.s.photo_dir, ignore_errors=True)
        self.s.photo_dir.mkdir(parents=True, exist_ok=True)
        return ActionResult(True, "Fotos, Chats und Standorte gelöscht.")

    # =========================================================================
    # Aufgaben
    # =========================================================================
    def tasks(self) -> dict[str, str]:
        return {r["spot_id"]: r["text"] for r in self.db.all("SELECT * FROM tasks")}

    def set_task(self, spot_id: str, text: str) -> None:
        text = text.strip()
        if text:
            self.db.run("INSERT INTO tasks(spot_id,text) VALUES(?,?) ON CONFLICT(spot_id) DO UPDATE SET "
                        "text=excluded.text", (spot_id, text))
        else:
            self.db.run("DELETE FROM tasks WHERE spot_id=?", (spot_id,))

    def random_tasks(self) -> list:
        return self.db.all("SELECT * FROM random_tasks ORDER BY id")

    def add_random_task(self, text: str) -> None:
        if text.strip():
            self.db.run("INSERT INTO random_tasks(text) VALUES(?)", (text.strip(),))

    def delete_random_task(self, task_id: int) -> None:
        self.db.run("DELETE FROM random_tasks WHERE id=?", (task_id,))

    # =========================================================================
    # Karte / Chats
    # =========================================================================
    def map_data(self) -> dict:
        st = self.state()
        teams = {t["id"]: t for t in self.teams()}
        spots = []
        for sp in self.all_spots(st):
            owner = st.owners.get(sp.id)
            spots.append({
                "id": sp.id, "kind": sp.kind, "name": sp.name, "lat": sp.lat, "lon": sp.lon,
                "address": sp.address, "group": sp.group_name, "color": sp.color, "active": sp.active,
                "original": sp.original_name, "kneipe": sp.is_kneipe, "no": sp.no,
                "owner": teams[owner]["name"] if owner in teams else None,
                "owner_color": teams[owner]["color"] if owner in teams else None,
            })
        positions = []
        for t in teams.values():
            row = self.db.one("SELECT lat, lon, created_at, spot_id FROM checkins WHERE team_id=? AND lat IS NOT "
                              "NULL ORDER BY id DESC LIMIT 1", (t["id"],))
            if row:
                positions.append({"team": t["name"], "color": t["color"], "lat": row["lat"], "lon": row["lon"],
                                  "time": self.hm(row["created_at"]), "spot": self.spot(row["spot_id"]).name})
        live = []
        now = self.clock()
        for row in self.db.all("SELECT * FROM live_positions"):
            t = teams.get(row["team_id"])
            if t is None:
                continue
            age = max(0, int((now - parse_iso(row["ts"])).total_seconds()))
            ended = bool(row["live_until"]) and parse_iso(row["live_until"]) < now
            live.append({"team": t["name"], "color": t["color"], "lat": row["lat"], "lon": row["lon"],
                         "accuracy": row["accuracy"], "age_s": age, "time": self.hm(row["ts"]), "ended": ended})
        return {"spots": spots, "positions": positions, "live": live,
                "boundary": [list(p) for p in self.data.boundary]}

    def messages(self, team_id: int | None = None, after_id: int = 0, limit: int = 300,
                 unlinked: bool = False) -> list:
        """Chatverlauf. `unlinked=True`: nur Chats, die (noch) keinem Team zugeordnet sind."""
        if unlinked:
            rows = self.db.all("SELECT * FROM messages WHERE team_id IS NULL AND id>? ORDER BY id DESC LIMIT ?",
                               (after_id, limit))
        elif team_id:
            rows = self.db.all("SELECT * FROM messages WHERE team_id=? AND id>? ORDER BY id DESC LIMIT ?",
                               (team_id, after_id, limit))
        else:
            rows = self.db.all("SELECT * FROM messages WHERE id>? ORDER BY id DESC LIMIT ?", (after_id, limit))
        return list(reversed(rows))
