"""SQLite-Zugriff. Eine Verbindung, WAL-Modus, alles in einem Prozess."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .rules import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    color       TEXT NOT NULL DEFAULT '#888888',
    code        TEXT NOT NULL UNIQUE,
    chat_id     INTEGER UNIQUE,
    chat_title  TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    spot_id     TEXT PRIMARY KEY,
    text        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS random_tasks (
    id          INTEGER PRIMARY KEY,
    text        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkins (
    id          INTEGER PRIMARY KEY,
    team_id     INTEGER NOT NULL,
    spot_id     TEXT NOT NULL,
    lat         REAL,
    lon         REAL,
    distance_m  REAL,
    task_text   TEXT,
    status      TEXT NOT NULL,          -- open | submitted | done | expired | replaced
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    is_test     INTEGER NOT NULL DEFAULT 0,  -- Check-in per Nummer im Testmodus
    mode        TEXT NOT NULL DEFAULT 'normal',  -- normal | shot (Kurzen & gehen)
    event_card_id INTEGER,                    -- Ereigniskarte, die bei diesem Check-in kam
    event_text  TEXT
);
CREATE INDEX IF NOT EXISTS ix_checkins_team ON checkins(team_id, status);
CREATE TABLE IF NOT EXISTS submissions (
    id              INTEGER PRIMARY KEY,
    checkin_id      INTEGER NOT NULL,
    team_id         INTEGER NOT NULL,
    spot_id         TEXT NOT NULL,
    photo_path      TEXT,
    file_id         TEXT,
    file_unique_id  TEXT,
    submitted_at    TEXT NOT NULL,
    status          TEXT NOT NULL,      -- pending | approved | rejected | revoked
    reviewed_by     TEXT,
    reviewed_at     TEXT,
    reason          TEXT,
    task_done       INTEGER NOT NULL DEFAULT 0,
    event_id        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_sub_status ON submissions(status, submitted_at);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    type    TEXT NOT NULL,
    data    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    ts              TEXT NOT NULL,
    team_id         INTEGER,
    chat_id         INTEGER,
    tg_message_id   INTEGER,
    sender          TEXT,
    direction       TEXT NOT NULL,      -- in | out
    kind            TEXT NOT NULL,      -- text | photo | location | other
    text            TEXT,
    photo_path      TEXT,
    file_unique_id  TEXT,
    lat             REAL,
    lon             REAL
);
CREATE INDEX IF NOT EXISTS ix_msg_team ON messages(team_id, id);
CREATE TABLE IF NOT EXISTS event_cards (
    id          INTEGER PRIMARY KEY,
    offset_min  INTEGER NOT NULL,       -- Minuten nach Spielstart
    kind        TEXT NOT NULL,          -- next_checkin | double_points | broadcast
    text        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_cards (
    id          INTEGER PRIMARY KEY,
    text        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (           -- Videoanrufe für Aktionskarten
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,        -- action
    team_id       INTEGER NOT NULL,
    spot_id       TEXT,                 -- Kneipe des letzten Check-ins bei der Anfrage
    checkin_id    INTEGER,
    card_id       INTEGER,
    card_text     TEXT,
    requested_at  TEXT NOT NULL,
    status        TEXT NOT NULL,        -- open | success | fail | cancelled | revoked
    decided_by    TEXT,
    decided_at    TEXT,
    event_id      INTEGER
);
CREATE INDEX IF NOT EXISTS ix_calls_status ON calls(status, requested_at);
CREATE TABLE IF NOT EXISTS live_positions (  -- nur die LETZTE Live-Position je Team, kein Verlauf
    team_id     INTEGER PRIMARY KEY,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    accuracy    REAL,
    ts          TEXT NOT NULL,
    live_until  TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
"""


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.lock = threading.RLock()

    def _migrate(self) -> None:
        """Spalten nachrüsten, die in älteren Datenbanken fehlen."""
        wanted = {
            "checkins": [
                ("is_test", "INTEGER NOT NULL DEFAULT 0"),
                ("mode", "TEXT NOT NULL DEFAULT 'normal'"),
                ("event_card_id", "INTEGER"),
                ("event_text", "TEXT"),
                ("live_distance_m", "REAL"),   # Live-Standort beim Check-in: Entfernung zur Bar
                ("live_age_s", "INTEGER"),     # ... und wie alt die Live-Position da war
            ],
            "messages": [
                ("chat_title", "TEXT"),        # Gruppenname, v.a. für noch nicht verknüpfte Chats
            ],
            "submissions": [
                ("live_distance_m", "REAL"),   # Live-Standort beim Foto-Upload
                ("live_age_s", "INTEGER"),
                ("live_lat", "REAL"),
                ("live_lon", "REAL"),
            ],
        }
        for table, columns in wanted.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Schreibtransaktion (BEGIN IMMEDIATE)."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def run(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    # --- kv ------------------------------------------------------------------
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    def set_kv(self, key: str, value: str | None) -> None:
        if value is None:
            self.run("DELETE FROM kv WHERE key=?", (key,))
        else:
            self.run("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))

    # --- Ereignisse ----------------------------------------------------------
    @staticmethod
    def insert_event(conn: sqlite3.Connection, ts: str, actor: str, type_: str, data: dict[str, Any]) -> Event:
        cur = conn.execute("INSERT INTO events(ts,actor,type,data) VALUES(?,?,?,?)",
                           (ts, actor, type_, json.dumps(data, ensure_ascii=False)))
        return Event(cur.lastrowid, ts, type_, data, actor)

    def events(self) -> list[Event]:
        rows = self.all("SELECT id, ts, actor, type, data FROM events ORDER BY id")
        return [Event(r["id"], r["ts"], r["type"], json.loads(r["data"]), r["actor"]) for r in rows]
