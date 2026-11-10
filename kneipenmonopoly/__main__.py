"""Start: python -m kneipenmonopoly"""
from __future__ import annotations

import logging
import secrets
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

import uvicorn

from .config import load_settings
from .gamedata import GameDataError, load_game_data


def main() -> int:
    s = load_settings(".env")
    s.data_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    filelog = RotatingFileHandler(s.data_dir / "kneipenmonopoly.log", maxBytes=5_000_000, backupCount=3,
                                  encoding="utf-8")
    filelog.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(filelog)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    problems = []
    if not Path(".env").exists():
        problems.append(".env fehlt: .env.example nach .env kopieren und ausfuellen")
    if not s.admin_password:
        problems.append("ADMIN_PASSWORD fehlt in .env (Datei gespeichert?)")
    if not s.session_secret:
        # Einmal erzeugen und merken, damit Admins nach einem Neustart angemeldet bleiben.
        secret_file = s.data_dir / "session_secret"
        if not secret_file.exists():
            secret_file.write_text(secrets.token_hex(32), encoding="utf-8")
        s.session_secret = secret_file.read_text(encoding="utf-8").strip()
    if not s.bot_token:
        logging.warning("BOT_TOKEN fehlt: Es läuft nur die Webseite, ohne Telegram-Bot.")
    try:
        load_game_data(s.game_data_path)
    except GameDataError as e:
        problems.append(str(e))
    if not s.map_pdf_path.exists():
        logging.warning("Karten-PDF nicht gefunden: %s", s.map_pdf_path)
    if problems:
        for p in problems:
            logging.error(p)
        return 1

    from .web.app import create_app

    app = create_app(s)
    logging.info("Admin-Seite: http://%s:%s/", "localhost" if s.host == "0.0.0.0" else s.host, s.port)
    uvicorn.run(app, host=s.host, port=s.port, log_level="warning", proxy_headers=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
