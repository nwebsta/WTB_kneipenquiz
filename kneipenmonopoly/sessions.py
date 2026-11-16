"""Mehrere Spiele (Sessions) in einem Prozess.

Jede Session hat ihren eigenen Telegram-Bot, ihr eigenes Passwort und einen eigenen Datenordner
(data/sessions/<id>/: Datenbank, Fotos, Spieldaten, Texte). Die Liste der Sessions steht in
data/sessions.sqlite3. Sessions werden `SESSION_DAYS` Tage nach dem Anlegen samt Daten gelöscht.

Beim ersten Start nach dem Update wird ein bestehendes Einzelspiel (data/kneipenmonopoly.sqlite3,
BOT_TOKEN und ADMIN_PASSWORD aus der .env) automatisch zur ersten Session.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import re
import secrets
import shutil
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from .config import Settings
from .db import DB
from .gamedata import GameDataError, load_game_data
from .service import SETUP_PARTS, Game
from .simulation import Simulator

log = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
MIN_PASSWORD = 6
PBKDF2_ROUNDS = 200_000
HOUSEKEEPING_S = 300          # alle 5 Minuten: abgelaufene Sessions löschen, Bots prüfen

# (ok, Bot-Benutzername oder Fehlermeldung)
TokenChecker = Callable[[str], Awaitable[tuple[bool, str]]]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS)
    return salt, digest.hex()


def bot_id(token: str) -> str:
    return token.split(":", 1)[0].strip()


async def telegram_check(token: str) -> tuple[bool, str]:
    """Fragt Telegram (getMe), ob der Token gültig ist."""
    from telegram import Bot
    from telegram.error import InvalidToken, TelegramError
    try:
        async with Bot(token) as bot:
            return True, bot.username
    except InvalidToken:
        return False, "Telegram kennt diesen Bot-Token nicht. Bitte bei @BotFather nachsehen (/mybots → API Token)."
    except TelegramError as e:
        return False, f"Telegram ist gerade nicht erreichbar ({e}). Bitte gleich nochmal versuchen."
    except Exception as e:  # z.B. Netzwerk
        return False, f"Bot-Token konnte nicht geprüft werden: {e}"


@dataclass
class Result:
    ok: bool
    message: str
    sid: str | None = None


@dataclass
class Runtime:
    """Eine laufende Session: Spiel, Simulation, Telegram-Bot, Hintergrundaufgabe."""
    sid: str
    settings: Settings
    game: Game
    sim: Simulator
    tg_app: object | None = None
    sweeper: asyncio.Task | None = None
    bot_error: str | None = None
    bot_username: str | None = None


class SessionManager:
    def __init__(self, base: Settings, run_bots: bool = True, check_token: TokenChecker | None = None,
                 clock: Callable[[], datetime] = utcnow):
        self.base = base
        self.run_bots = run_bots
        self.check_token = check_token or telegram_check
        self.clock = clock
        self.runtimes: dict[str, Runtime] = {}
        self._lock = threading.Lock()
        self._create_lock = asyncio.Lock()
        self._housekeeping: asyncio.Task | None = None
        base.data_dir.mkdir(parents=True, exist_ok=True)
        self.reg = sqlite3.connect(str(base.registry_path), check_same_thread=False, isolation_level=None)
        self.reg.row_factory = sqlite3.Row
        self.reg.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                expires_at   TEXT NOT NULL,
                bot_token    TEXT NOT NULL DEFAULT '',
                bot_username TEXT,
                pw_salt      TEXT NOT NULL,
                pw_hash      TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)

    # ------------------------------------------------------------------ Registry
    def _q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.reg.execute(sql, params).fetchall()

    def _x(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self.reg.execute(sql, params)

    def rows(self) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM sessions ORDER BY created_at, id")

    def row(self, sid: str | None) -> sqlite3.Row | None:
        if not sid:
            return None
        r = self._q("SELECT * FROM sessions WHERE id=?", (sid,))
        return r[0] if r else None

    def get(self, sid: str | None) -> Runtime | None:
        return self.runtimes.get(sid) if sid else None

    def overview(self) -> list[dict]:
        """Für Startseite und Master-Bereich (ohne Token/Passwort)."""
        out = []
        now = self.clock()
        for r in self.rows():
            rt = self.runtimes.get(r["id"])
            expires = _parse(r["expires_at"])
            out.append({
                "id": r["id"], "name": r["name"],
                "created": _parse(r["created_at"]).astimezone(self.base.tz),
                "expires": expires.astimezone(self.base.tz),
                "days_left": max(0, (expires - now).days),
                "bot": rt.bot_username if rt and rt.bot_username else r["bot_username"],
                "bot_error": rt.bot_error if rt else "nicht gestartet",
                "phase": rt.game.phase if rt else None,
                "teams": len(rt.game.teams()) if rt else 0,
            })
        return out

    def find_by_name(self, name: str) -> sqlite3.Row | None:
        r = self._q("SELECT * FROM sessions WHERE lower(name)=lower(?)", (name.strip(),))
        return r[0] if r else None

    def check_password(self, sid: str, password: str) -> bool:
        r = self.row(sid)
        if r is None or not password:
            return False
        _, digest = hash_password(password, r["pw_salt"])
        return secrets.compare_digest(digest, r["pw_hash"])

    def check_master(self, password: str) -> bool:
        master = self.base.effective_master_password
        return bool(master) and bool(password) and secrets.compare_digest(password.encode(), master.encode())

    def session_dir(self, sid: str) -> Path:
        return self.base.sessions_dir / sid

    def session_settings(self, sid: str, token: str) -> Settings:
        d = self.session_dir(sid)
        return dataclasses.replace(self.base, data_dir=d, texts_path=d / "texte.toml", bot_token=token,
                                   admin_password="", shared_tile_dir=self.base.tile_cache_dir)

    # ------------------------------------------------------------------ Lebenszyklus
    async def start(self) -> None:
        self.migrate_legacy()
        await self.delete_expired()
        for r in self.rows():
            if r["id"] not in self.runtimes:
                try:
                    await self._start_runtime(self._open(r))
                except Exception:
                    log.exception("Session %s (%s) konnte nicht starten", r["name"], r["id"])
            elif self.runtimes[r["id"]].sweeper is None:
                await self._start_runtime(self.runtimes[r["id"]])
        self._housekeeping = asyncio.create_task(self._housekeeping_loop())

    async def stop(self) -> None:
        if self._housekeeping:
            self._housekeeping.cancel()
        for rt in list(self.runtimes.values()):
            await self._stop_runtime(rt)

    def adopt(self, game: Game, password: str, sid: str = "main") -> Runtime:
        """Ein schon erzeugtes Spiel als Session übernehmen (Tests, Einzelbetrieb ohne Registry-Daten)."""
        if self.row(sid) is None:
            salt, digest = hash_password(password or secrets.token_hex(16))
            now = self.clock()
            self._x("INSERT INTO sessions(id,name,created_at,expires_at,bot_token,pw_salt,pw_hash) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (sid, game.data.game, _iso(now), _iso(now + timedelta(days=self.base.session_days)),
                     game.s.bot_token or "", salt, digest))
        rt = Runtime(sid, game.s, game, Simulator(game))
        self.runtimes[sid] = rt
        return rt

    def _open(self, r: sqlite3.Row) -> Runtime:
        s = self.session_settings(r["id"], r["bot_token"] or "")
        s.data_dir.mkdir(parents=True, exist_ok=True)
        game = Game(s, load_game_data(s.active_game_data_path), DB(s.db_path))
        rt = Runtime(r["id"], s, game, Simulator(game), bot_username=r["bot_username"])
        self.runtimes[r["id"]] = rt
        return rt

    async def _start_runtime(self, rt: Runtime) -> None:
        await self._start_bot(rt)

        async def sweeper():
            while True:
                try:
                    await rt.game.sweep()
                except Exception:
                    log.exception("Fehler im Sweeper (Session %s)", rt.sid)
                await asyncio.sleep(20)

        rt.sweeper = asyncio.create_task(sweeper())

    async def _start_bot(self, rt: Runtime) -> None:
        rt.bot_error = None
        if not self.run_bots:
            return
        if not rt.settings.bot_token:
            rt.bot_error = "Kein Bot-Token eingetragen."
            return
        from telegram.error import InvalidToken

        from .bot import build_application, start_bot
        app = build_application(rt.game)
        try:
            await start_bot(app)
        except InvalidToken:
            rt.bot_error = "Telegram lehnt den Bot-Token ab (widerrufen?). Bitte in der Verwaltung einen neuen eintragen."
            log.error("Session %s: Bot-Token ungültig", rt.sid)
            return
        except Exception as e:
            rt.bot_error = f"Bot konnte nicht starten: {e}"
            log.exception("Session %s: Bot-Start fehlgeschlagen", rt.sid)
            return
        rt.tg_app = app
        rt.bot_username = app.bot.username
        self._x("UPDATE sessions SET bot_username=? WHERE id=?", (rt.bot_username, rt.sid))

    async def _stop_bot(self, rt: Runtime) -> None:
        if rt.tg_app is not None:
            from .bot import stop_bot
            await stop_bot(rt.tg_app)
            rt.tg_app = None
        from .service import NullNotifier
        rt.game.notifier = NullNotifier()

    async def _stop_runtime(self, rt: Runtime) -> None:
        try:
            await rt.sim.stop()
        except Exception:
            log.exception("Simulation stoppen (Session %s)", rt.sid)
        if rt.sweeper:
            rt.sweeper.cancel()
            rt.sweeper = None
        await self._stop_bot(rt)

    async def _housekeeping_loop(self) -> None:
        while True:
            await asyncio.sleep(HOUSEKEEPING_S)
            try:
                await self.delete_expired()
                await self.check_bots()
            except Exception:
                log.exception("Fehler bei der Session-Pflege")

    async def check_bots(self) -> None:
        """Bots, deren Token inzwischen ungültig ist, anhalten (Session bleibt, Warnung im Dashboard)."""
        from telegram.error import InvalidToken
        for rt in list(self.runtimes.values()):
            if rt.tg_app is None:
                continue
            try:
                await rt.tg_app.bot.get_me()
            except InvalidToken:
                log.error("Session %s: Bot-Token wurde ungültig, Bot angehalten", rt.sid)
                await self._stop_bot(rt)
                rt.bot_error = "Telegram lehnt den Bot-Token ab (widerrufen?). Bitte in der Verwaltung einen neuen eintragen."
            except Exception:
                pass   # Netzwerkstörung: nichts tun, der Bot versucht es selbst weiter

    # ------------------------------------------------------------------ Anlegen / Ändern / Löschen
    async def _validate_token(self, token: str, ignore_sid: str | None = None) -> tuple[bool, str]:
        token = token.strip()
        if not token:
            return False, "Bitte den Bot-Token von @BotFather eintragen."
        if not TOKEN_RE.match(token):
            return False, "Das sieht nicht wie ein Bot-Token aus (Form: 123456789:ABC…)."
        for r in self.rows():
            if r["id"] != ignore_sid and r["bot_token"] and bot_id(r["bot_token"]) == bot_id(token):
                return False, f"Dieser Bot wird schon von der Session „{r['name']}“ benutzt. Jede Session braucht einen eigenen Bot."
        return await self.check_token(token)

    async def create(self, name: str, token: str, password: str, password2: str | None = None,
                     setup: dict | None = None) -> Result:
        """Neue Session. Klappt etwas nicht (Token, Passwort, Import, Bot-Start), bleibt nichts übrig."""
        name = " ".join((name or "").split())
        token = (token or "").strip()
        if not name:
            return Result(False, "Bitte einen Namen für die Session angeben.")
        if len(name) > 40:
            return Result(False, "Der Name ist zu lang (max. 40 Zeichen).")
        if len(password or "") < MIN_PASSWORD:
            return Result(False, f"Das Passwort muss mindestens {MIN_PASSWORD} Zeichen haben.")
        if password2 is not None and password != password2:
            return Result(False, "Die beiden Passwörter stimmen nicht überein.")
        async with self._create_lock:
            if self.find_by_name(name):
                return Result(False, f"Eine Session „{name}“ gibt es schon. Bitte einen anderen Namen wählen.")
            if self.base.max_sessions and len(self.rows()) >= self.base.max_sessions:
                return Result(False, f"Es gibt schon {len(self.rows())} Sessions (Maximum {self.base.max_sessions}). "
                                     "Bitte eine alte Session löschen lassen.")
            ok, info = await self._validate_token(token)
            if not ok:
                return Result(False, info)
            sid = secrets.token_hex(4)
            while self.row(sid) or self.session_dir(sid).exists():
                sid = secrets.token_hex(4)
            salt, digest = hash_password(password)
            now = self.clock()
            self._x("INSERT INTO sessions(id,name,created_at,expires_at,bot_token,bot_username,pw_salt,pw_hash) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (sid, name, _iso(now), _iso(now + timedelta(days=self.base.session_days)), token, info,
                     salt, digest))
            try:
                rt = self._open(self.row(sid))
                if setup is not None:
                    res = rt.game.import_setup(setup, set(SETUP_PARTS))
                    if not res.ok:
                        await self.delete(sid)
                        return Result(False, f"Import fehlgeschlagen, Session nicht angelegt: {res.message}")
                await self._start_runtime(rt)
            except (GameDataError, Exception) as e:
                log.exception("Session %s anlegen fehlgeschlagen", sid)
                await self.delete(sid)
                return Result(False, f"Session konnte nicht angelegt werden: {e}")
            if rt.bot_error:
                msg = rt.bot_error
                await self.delete(sid)
                return Result(False, f"Bot startet nicht, Session wurde wieder gelöscht: {msg}")
        log.info("Session angelegt: %s (%s), Bot @%s", name, sid, info)
        return Result(True, f"Session „{name}“ angelegt. Bot: @{info}", sid)

    async def delete(self, sid: str) -> Result:
        r = self.row(sid)
        rt = self.runtimes.pop(sid, None)
        if rt is not None:
            await self._stop_runtime(rt)
            try:
                rt.game.db.close()
            except Exception:
                pass
        self._x("DELETE FROM sessions WHERE id=?", (sid,))
        d = self.session_dir(sid)
        if d.exists() and d.resolve().parent == self.base.sessions_dir.resolve():
            await asyncio.to_thread(shutil.rmtree, d, True)
        if r is None and rt is None:
            return Result(False, "Session nicht gefunden.")
        log.info("Session gelöscht: %s (%s)", r["name"] if r else "?", sid)
        return Result(True, f"Session „{r['name'] if r else sid}“ und alle Daten gelöscht.")

    async def delete_expired(self) -> list[str]:
        now = self.clock()
        gone = []
        for r in self.rows():
            if _parse(r["expires_at"]) <= now:
                await self.delete(r["id"])
                gone.append(r["name"])
        return gone

    def extend(self, sid: str, days: int | None = None) -> Result:
        r = self.row(sid)
        if r is None:
            return Result(False, "Session nicht gefunden.")
        days = days or self.base.session_days
        new = max(_parse(r["expires_at"]), self.clock()) + timedelta(days=days)
        self._x("UPDATE sessions SET expires_at=? WHERE id=?", (_iso(new), sid))
        return Result(True, f"„{r['name']}“ läuft jetzt bis {new.astimezone(self.base.tz):%d.%m.%Y %H:%M}.")

    def set_password(self, sid: str, password: str, password2: str | None = None) -> Result:
        if len(password or "") < MIN_PASSWORD:
            return Result(False, f"Das Passwort muss mindestens {MIN_PASSWORD} Zeichen haben.")
        if password2 is not None and password != password2:
            return Result(False, "Die beiden Passwörter stimmen nicht überein.")
        salt, digest = hash_password(password)
        self._x("UPDATE sessions SET pw_salt=?, pw_hash=? WHERE id=?", (salt, digest, sid))
        return Result(True, "Passwort geändert.")

    async def set_token(self, sid: str, token: str) -> Result:
        rt = self.get(sid)
        if rt is None:
            return Result(False, "Session nicht gefunden.")
        token = (token or "").strip()
        ok, info = await self._validate_token(token, ignore_sid=sid)
        if not ok:
            return Result(False, info)
        await self._stop_bot(rt)
        self._x("UPDATE sessions SET bot_token=?, bot_username=? WHERE id=?", (token, info, sid))
        rt.settings.bot_token = token
        rt.bot_username = info
        await self._start_bot(rt)
        if rt.bot_error:
            return Result(False, rt.bot_error)
        return Result(True, f"Neuer Bot läuft: @{info}. Die Teams müssen den neuen Bot in ihre Gruppen holen "
                            "und dort /join CODE schicken.")

    # ------------------------------------------------------------------ Altbestand übernehmen
    def migrate_legacy(self) -> str | None:
        """Einzelspiel von vor dem Multi-Session-Update zur ersten Session machen (einmalig)."""
        if self._q("SELECT 1 FROM meta WHERE key='legacy_checked'"):
            return None
        self._x("INSERT OR REPLACE INTO meta(key,value) VALUES('legacy_checked', ?)", (_iso(self.clock()),))
        base = self.base
        old_db = base.db_path
        if not old_db.exists() or self.rows():
            return None
        sid = secrets.token_hex(4)
        d = self.session_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        for name in ("kneipenmonopoly.sqlite3", "kneipenmonopoly.sqlite3-wal", "kneipenmonopoly.sqlite3-shm",
                     "photos", "karte.jpg", "spieldaten.json"):
            src = base.data_dir / name
            if src.exists():
                shutil.move(str(src), str(d / name))
        if base.texts_path.exists():
            shutil.copy2(base.texts_path, d / "texte.toml")
        try:
            game_name = load_game_data(Settings(game_data_path=base.game_data_path,
                                                data_dir=d).active_game_data_path).game
        except Exception:
            game_name = "Kneipenmonopoly"
        name = game_name
        password = base.admin_password or secrets.token_urlsafe(9)
        salt, digest = hash_password(password)
        now = self.clock()
        self._x("INSERT INTO sessions(id,name,created_at,expires_at,bot_token,pw_salt,pw_hash) VALUES(?,?,?,?,?,?,?)",
                (sid, name, _iso(now), _iso(now + timedelta(days=base.session_days)), base.bot_token or "",
                 salt, digest))
        log.warning("Bisheriges Spiel als Session „%s“ (%s) übernommen. Passwort = bisheriges ADMIN_PASSWORD.",
                    name, sid)
        return sid


def parse_setup_upload(raw: bytes) -> tuple[dict | None, str | None]:
    """Hochgeladene Setup-/Spieldaten-Datei lesen. (payload, Fehler)"""
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return None, f"Keine gültige JSON-Datei: {e}"
    if not isinstance(payload, dict):
        return None, "Die Datei enthält kein gültiges Setup."
    return payload, None
