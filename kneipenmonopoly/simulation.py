"""Live-Simulation: angelegte Teams spielen automatisch, die Spielleitung schaut zu.

Läuft im selben Prozess wie Bot und Webseite. Die Teams laufen über die Karte
(Live-Standort), checken in Bars ein, schicken erzeugte Bilder, ziehen
Aktionskarten und geraten auch mal in ein Wettrennen. Alles geht durch die
normale Spiellogik, Prüfen/Karte/Chats/Dashboard zeigen es also wie im echten
Spiel. Bot-Nachrichten gehen nur an Telegram, wenn das ausdrücklich gewählt ist.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import struct
import uuid
import zlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .gamedata import distance_m
from .service import Game

log = logging.getLogger(__name__)

SIM_SENDER = "Simulation"

# Tempo-Stufen: Laufgeschwindigkeit (m/s) und Faktor für Wartezeiten (Trinken, Prüfen, Anruf).
# Bei Faktor 1 trinkt ein Team 6–18 s, eine automatische Freigabe kommt nach 15–25 s.
SPEEDS = {
    "realistisch": {"label": "Realistisch (zu Fuß, Bier dauert Minuten)", "mps": 1.4, "scale": 20.0},
    "langsam": {"label": "Langsam (Fahrrad, kurze Pausen)", "mps": 5.0, "scale": 4.0},
    "normal": {"label": "Normal (gut zum Zuschauen)", "mps": 12.0, "scale": 1.0},
    "schnell": {"label": "Schnell", "mps": 30.0, "scale": 0.4},
    "turbo": {"label": "Turbo (alles in Sekunden)", "mps": 80.0, "scale": 0.15},
}
DEFAULT_SPEED = "normal"


# --------------------------------------------------------------------------- Bilder
def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def _hex(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except (ValueError, IndexError):
        return 120, 120, 120


def make_photo(path: Path, team_color: str, seed: int, beers: int = 4) -> None:
    """Erzeugt ein „Beweisfoto“: Farbverlauf in Teamfarbe, Theke und Biergläser."""
    rnd = random.Random(seed)
    w, h = 480, 360
    r, g, b = _hex(team_color)
    dark = (r // 3, g // 3, b // 3)
    beer_color, foam_color, bar_color = (232, 170, 30), (250, 250, 245), (90, 55, 30)
    glasses = []
    slot = w // (beers + 1)
    for i in range(beers):
        gw, gh = rnd.randint(34, 46), rnd.randint(80, 110)
        gx = slot * (i + 1) - gw // 2 + rnd.randint(-12, 12)
        glasses.append((gx, 250 - gh, gw, gh, rnd.randint(12, 22)))
    confetti = [(rnd.randrange(w), rnd.randrange(150), rnd.randint(4, 9),
                 (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))) for _ in range(25)]
    rows = []
    for y in range(h):
        t = y / h
        base = bytes((int(r * (1 - t) + dark[0] * t), int(g * (1 - t) + dark[1] * t), int(b * (1 - t) + dark[2] * t)))
        row = bytearray(base * w)
        if y >= 250:                                  # Theke
            row[:] = bytes(bar_color) * w
        for cx, cy, s, col in confetti:
            if cy <= y < cy + s:
                row[cx * 3:min(w, cx + s) * 3] = bytes(col) * (min(w, cx + s) - cx)
        for gx, gy, gw, gh, foam in glasses:
            if gy <= y < gy + gh:
                color = foam_color if y < gy + foam else beer_color
                x0, x1 = max(0, gx), min(w, gx + gw)
                row[x0 * 3:x1 * 3] = bytes(color) * (x1 - x0)
        rows.append(bytes(row))
    path.write_bytes(_png(w, h, rows))


# --------------------------------------------------------------------------- Teams
@dataclass
class SimTeam:
    id: int
    name: str
    color: str
    lat: float
    lon: float
    target: str | None = None
    state: str = "walk"          # walk | drink | review | action
    until: float = 0.0
    sub_id: int | None = None
    retries: int = 0
    members: int = field(default_factory=lambda: random.randint(3, 5))


class Simulator:
    def __init__(self, game: Game):
        self.game = game
        self.task: asyncio.Task | None = None
        self.teams: dict[int, SimTeam] = {}
        self.events: deque[tuple[str, str, str]] = deque(maxlen=60)   # (Uhrzeit, Team, Text)
        self.send_to_groups = False
        self.auto_approve = False
        self.tick = 2.0
        self.speed = DEFAULT_SPEED
        self.speed_mps = SPEEDS[DEFAULT_SPEED]["mps"]      # Laufgeschwindigkeit in m/s
        self.time_scale = SPEEDS[DEFAULT_SPEED]["scale"]   # Wartezeiten multiplizieren (Tests: 0)
        self.started_at: datetime | None = None

    def set_speed(self, name: str) -> str:
        """Tempo setzen, auch während die Simulation läuft. Laufende Wartezeiten bleiben, neue nutzen das Tempo."""
        if name not in SPEEDS:
            return "Unbekanntes Tempo."
        self.speed = name
        self.speed_mps = SPEEDS[name]["mps"]
        self.time_scale = SPEEDS[name]["scale"]
        self.note(None, f"Tempo: {SPEEDS[name]['label']}")
        return f"Tempo: {SPEEDS[name]['label']}."

    @property
    def speed_label(self) -> str:
        return SPEEDS.get(self.speed, {}).get("label", f"{self.speed_mps:g} m/s")

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def note(self, team: SimTeam | None, text: str) -> None:
        self.events.appendleft((self.game.hm(self.game.clock()), team.name if team else "–", text))

    # ------------------------------------------------------------------ Steuerung
    async def start(self, team_ids: list[int] | None = None, send_to_groups: bool = False,
                    auto_approve: bool = False, speed: str | None = None) -> str:
        if self.running:
            return "Die Simulation läuft schon."
        if speed:
            self.set_speed(speed)
        g = self.game
        teams = [t for t in g.teams() if team_ids is None or t["id"] in team_ids]
        if not teams:
            return "Keine Teams ausgewählt. Lege zuerst Teams an."
        self.send_to_groups, self.auto_approve = send_to_groups, auto_approve
        if not send_to_groups:
            g.muted_teams |= {t["id"] for t in teams}
        # Alle starten zentral: Mitte aller aktiven Felder, nur ein paar Meter auseinander.
        clat, clon = self.center()
        self.teams = {}
        for t in teams:
            lat, lon = _move(clat, clon, random.uniform(-5, 5), random.uniform(-5, 5))
            self.teams[t["id"]] = SimTeam(t["id"], t["name"], t["color"], lat, lon)
        if g.phase == "setup":
            await g.start_game(SIM_SENDER)
            self.note(None, "Spiel gestartet")
        self.started_at = g.clock()
        self.note(None, f"Simulation gestartet mit {', '.join(t.name for t in self.teams.values())}, "
                        f"Tempo {self.speed_label}" + (" (Nachrichten gehen an Telegram)" if send_to_groups else ""))
        self.task = asyncio.create_task(self._run())
        return f"Simulation läuft mit {len(self.teams)} Team(s)."

    async def stop(self) -> str:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        self.task = None
        self.game.muted_teams -= set(self.teams)
        if self.teams:
            self.note(None, "Simulation gestoppt")
        return "Simulation gestoppt."

    # ------------------------------------------------------------------ Ablauf
    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            if self.game.phase == "ended":
                self.note(None, "Spiel ist beendet, Simulation hält an")
                self.game.muted_teams -= set(self.teams)
                return
            if self.game.phase == "running":
                for st in list(self.teams.values()):
                    try:
                        await self.step(st, loop.time())
                    except Exception:
                        log.exception("Simulationsschritt für %s fehlgeschlagen", st.name)
            await asyncio.sleep(self.tick)

    async def step(self, st: SimTeam, now: float) -> None:
        g = self.game
        if st.state == "walk":
            if st.target is None or not g.spot(st.target).active:
                st.target = self._pick_target(st)
                if st.target is None:
                    return
                self.note(st, f"läuft zu {g.spot(st.target).name}")
            sp = g.spot(st.target)
            d = distance_m(st.lat, st.lon, sp.lat, sp.lon)
            step = self.speed_mps * self.tick
            if d > 12:
                f = min(1.0, step / d)
                st.lat += (sp.lat - st.lat) * f
                st.lon += (sp.lon - st.lon) * f
                st.lat, st.lon = _move(st.lat, st.lon, random.uniform(-2, 2), random.uniform(-2, 2))
            g.update_live(st.id, st.lat, st.lon, random.uniform(4, 15))
            if d <= 12:
                await self._arrive(st, now)
        elif st.state == "drink" and now >= st.until:
            await self._send_photo(st, now)
        elif st.state == "review":
            await self._watch_review(st, now)
        elif st.state == "action":
            call = g.open_call(st.id)
            if call is None:
                st.state = "walk"
            elif self.auto_approve and now >= st.until:
                ok = random.random() < 0.7
                await g.decide_call(call["id"], SIM_SENDER, ok)
                self.note(st, f"Aktion {'geschafft' if ok else 'nicht geschafft'} (automatisch)")
                st.state = "walk"

    def center(self) -> tuple[float, float]:
        spots = [sp for sp in self.game.all_spots() if sp.active]
        return sum(s.lat for s in spots) / len(spots), sum(s.lon for s in spots) / len(spots)

    def _pick_target(self, st: SimTeam) -> str | None:
        """Rein zufällig ein Feld, das nicht schon dem Team gehört und nicht in der Abklingzeit ist."""
        g = self.game
        state = g.state()
        options = [sp for sp in g.all_spots() if sp.active and state.owners.get(sp.id) != st.id
                   and not g._cooldown_block(st.id, sp.id)]
        if not options:
            return None
        return random.choice(options).id

    async def _arrive(self, st: SimTeam, now: float) -> None:
        g = self.game
        sp = g.spot(st.target)
        lat, lon = _move(sp.lat, sp.lon, random.uniform(-18, 18), random.uniform(-18, 18))
        team = g.team(st.id)
        g.log_message(team_id=st.id, chat_id=team["chat_id"] if team else None, direction="in", kind="location",
                      text="Standort", lat=lat, lon=lon, sender=SIM_SENDER)
        res = g.checkin(st.id, st.target, lat, lon)
        await g.send_team(st.id, res.message, res.buttons)
        if not res.ok:
            self.note(st, f"Check-in {sp.name} abgelehnt")
            st.target, st.state = None, "walk"
            return
        self.note(st, f"eingecheckt in {sp.name}")
        datas = [d for _, d in (res.buttons or [])]
        shot = next((d for d in datas if d.startswith("mode|")), None)
        if shot and random.random() < 0.35:
            r = await g.choose_mode(st.id, int(shot.split("|")[1]), "shot")
            await g.send_team(st.id, r.message)
            self.note(st, f"Kurzen & gehen in {sp.name}")
            st.target, st.state = None, "walk"
            return
        if shot:
            self.note(st, f"Wettrennen in {sp.name}!")
        st.state = "drink"
        st.until = now + random.uniform(6, 18) * self.time_scale
        if "aktion" in datas and random.random() < 0.25:
            r = await g.request_action(st.id)
            await g.send_team(st.id, r.message)
            if r.ok:
                self.note(st, "zieht eine Aktionskarte")

    async def _send_photo(self, st: SimTeam, now: float) -> None:
        g = self.game
        uid = f"sim_{uuid.uuid4().hex[:12]}"
        path = g.s.photo_dir / f"{uid}.png"
        await asyncio.to_thread(make_photo, path, st.color, random.randrange(10**9), st.members)
        team = g.team(st.id)
        g.log_message(team_id=st.id, chat_id=team["chat_id"] if team else None, direction="in", kind="photo",
                      photo_path=str(path), file_unique_id=uid, sender=SIM_SENDER)
        res = g.submit_photo(st.id, str(path), None, uid)
        if res is not None:
            await g.send_team(st.id, res.message)
        if res is None or not res.ok:
            self.note(st, "Foto zählt nicht (Check-in vorbei)")
            st.target, st.state = None, "walk"
            return
        row = g.db.one("SELECT id FROM submissions WHERE file_unique_id=?", (uid,))
        st.sub_id = row["id"] if row else None
        st.state, st.until = "review", now + random.uniform(15, 25) * self.time_scale
        self.note(st, f"Foto aus {g.spot(st.target).name} eingereicht")

    async def _watch_review(self, st: SimTeam, now: float) -> None:
        g = self.game
        sub = g.submission(st.sub_id) if st.sub_id else None
        status = sub["status"] if sub else "approved"
        if status == "pending":
            if self.auto_approve and now >= st.until:
                # Aufgabe nur „erfüllt“, wenn es eine gab, und dann in 2 von 3 Fällen.
                done = bool(sub["task_text"]) and random.random() < 0.66
                res = await g.approve(st.sub_id, SIM_SENDER, task_done=done)
                if res.ok:
                    self.note(st, "Foto automatisch freigegeben")
            return
        if status == "rejected" and st.retries < 1 and g.open_checkin(st.id):
            st.retries += 1
            st.state, st.until = "drink", now + 5 * self.time_scale
            self.note(st, "Foto abgelehnt, schickt ein neues")
            return
        st.retries, st.sub_id, st.target = 0, None, None
        st.state = "action" if g.open_call(st.id) else "walk"
        if st.state == "action":
            st.until = now + random.uniform(10, 20) * self.time_scale

    def status_rows(self) -> list[dict]:
        g = self.game
        rows = []
        for st in self.teams.values():
            label = {"walk": "unterwegs", "drink": "trinkt", "review": "wartet auf Prüfung",
                     "action": "wartet auf Videoanruf"}[st.state]
            rows.append({"team": st.name, "color": st.color, "state": label,
                         "target": g.spot(st.target).name if st.target else "–"})
        return rows


def _move(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    dlat = north_m / 111_320
    dlon = east_m / (111_320 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon
