"""Konfiguration aus Umgebungsvariablen bzw. .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .rules import RuleConfig


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "ja", "on"}


def _int(env: dict, key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    return int(raw)


@dataclass
class Settings:
    bot_token: str = ""                # pro Session; in der .env nur noch für die Übernahme des Altbestands
    admin_password: str = ""           # dito; zusätzlich Ersatz für ein fehlendes MASTER_PASSWORD
    session_secret: str = ""
    master_password: str = ""          # Master-Bereich: alle Sessions sehen, löschen, verlängern
    max_sessions: int = 10
    session_days: int = 30             # Sessions werden so viele Tage nach dem Anlegen gelöscht
    shared_tile_dir: Path | None = None   # gemeinsamer Kachel-Cache aller Sessions
    game_data_path: Path = Path("kneipenmonopoly_spieldaten.json")
    map_pdf_path: Path = Path("Kneipenmonopoly_Spielplan.pdf")
    texts_path: Path = Path("texte.toml")
    data_dir: Path = Path("data")
    host: str = "0.0.0.0"
    port: int = 8000
    ssl_certfile: Path | None = None   # gesetzt = HTTPS (Zertifikat, PEM)
    ssl_keyfile: Path | None = None    # privater Schlüssel (PEM)
    public_https: bool = False         # hinter nginx/HAProxy mit HTTPS (App selbst spricht HTTP auf 127.0.0.1)

    @property
    def https(self) -> bool:
        return bool(self.ssl_certfile and self.ssl_keyfile)
    checkin_radius_m: int = 50
    checkin_timeout_min: int = 20
    cooldown_min: int = 30
    game_duration_min: int | None = None   # Standard-Spieldauer, in der Verwaltung änderbar
    map_tiles: bool = True                 # Kartenbild mit OpenStreetMap-Hintergrund (braucht Internet)
    random_tasks_enabled: bool = True
    announce_street_complete: bool = True
    scores_hidden_default: bool = True     # Gesamtstand für Spieler verdeckt (Standard)
    timezone: str = "Europe/Berlin"
    rules: RuleConfig = field(default_factory=RuleConfig)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "kneipenmonopoly.sqlite3"

    @property
    def photo_dir(self) -> Path:
        return self.data_dir / "photos"

    @property
    def map_image_path(self) -> Path:
        return self.data_dir / "karte.jpg"

    @property
    def tile_cache_dir(self) -> Path:
        return self.shared_tile_dir or self.data_dir / "tiles"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def registry_path(self) -> Path:
        return self.data_dir / "sessions.sqlite3"

    @property
    def effective_master_password(self) -> str:
        return self.master_password or self.admin_password

    @property
    def imported_game_data_path(self) -> Path:
        """Über die Admin-Seite importierte Spieldaten (liegen bei den Daten, nicht im Git-Repository)."""
        return self.data_dir / "spieldaten.json"

    @property
    def active_game_data_path(self) -> Path:
        """Importierte Spieldaten haben Vorrang vor der Datei aus dem Repository."""
        p = self.imported_game_data_path
        return p if p.exists() else self.game_data_path


def load_settings(env_file: str | os.PathLike | None = ".env", env: dict | None = None) -> Settings:
    """Liest Einstellungen. `env` überschreibt (für Tests)."""
    if env is None:
        if env_file and Path(env_file).exists():
            load_dotenv(env_file, override=False)
        env = dict(os.environ)

    station_raw = env.get("STATION_POINTS", "1,2,3,4") or "1,2,3,4"
    station_points = tuple(int(x) for x in station_raw.split(",") if x.strip())

    rules = RuleConfig(
        points_free_field=_int(env, "POINTS_FREE_FIELD", 1),
        points_visitor=_int(env, "POINTS_VISITOR", 0),
        points_owner=_int(env, "POINTS_OWNER", 1),
        points_owner_street=_int(env, "POINTS_OWNER_STREET", 2),
        street_complete_bonus=_int(env, "POINTS_STREET_COMPLETE_BONUS", 2),
        task_bonus=_int(env, "POINTS_TASK_BONUS", 1),
        station_points=station_points,
        action_success=_int(env, "POINTS_ACTION_SUCCESS", 1),
        action_fail=_int(env, "POINTS_ACTION_FAIL", -1),
        double_factor=_int(env, "DOUBLE_POINTS_FACTOR", 2),
    )
    return Settings(
        bot_token=env.get("BOT_TOKEN", "") or "",
        admin_password=env.get("ADMIN_PASSWORD", "") or "",
        session_secret=env.get("SESSION_SECRET", "") or "",
        master_password=env.get("MASTER_PASSWORD", "") or "",
        max_sessions=_int(env, "MAX_SESSIONS", 10),
        session_days=_int(env, "SESSION_DAYS", 30),
        game_data_path=Path(env.get("GAME_DATA_PATH") or "kneipenmonopoly_spieldaten.json"),
        map_pdf_path=Path(env.get("MAP_PDF_PATH") or "Kneipenmonopoly_Spielplan.pdf"),
        texts_path=Path(env.get("TEXTS_PATH") or "texte.toml"),
        data_dir=Path(env.get("DATA_DIR") or "data"),
        host=env.get("HOST") or "0.0.0.0",
        port=_int(env, "PORT", 8000),
        ssl_certfile=Path(env["SSL_CERTFILE"]) if env.get("SSL_CERTFILE") else None,
        ssl_keyfile=Path(env["SSL_KEYFILE"]) if env.get("SSL_KEYFILE") else None,
        public_https=_bool(env.get("PUBLIC_HTTPS"), False),
        checkin_radius_m=_int(env, "CHECKIN_RADIUS_M", 50),
        checkin_timeout_min=_int(env, "CHECKIN_TIMEOUT_MIN", 20),
        cooldown_min=_int(env, "COOLDOWN_MIN", 30),
        game_duration_min=_int(env, "GAME_DURATION_MIN", 0) or None,
        map_tiles=_bool(env.get("MAP_TILES"), True),
        random_tasks_enabled=_bool(env.get("RANDOM_TASKS_ENABLED"), True),
        announce_street_complete=_bool(env.get("ANNOUNCE_STREET_COMPLETE"), True),
        scores_hidden_default=_bool(env.get("SCORES_HIDDEN"), True),
        timezone=env.get("TIMEZONE") or "Europe/Berlin",
        rules=rules,
    )
