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
    if not s.effective_master_password:
        logging.warning("MASTER_PASSWORD fehlt in .env: Der Master-Bereich (alle Sessions verwalten) ist aus.")
    elif not s.master_password:
        logging.warning("MASTER_PASSWORD fehlt in .env: Für den Master-Bereich gilt vorerst ADMIN_PASSWORD.")
    if not s.session_secret:
        # Einmal erzeugen und merken, damit Admins nach einem Neustart angemeldet bleiben.
        secret_file = s.data_dir / "session_secret"
        if not secret_file.exists():
            secret_file.write_text(secrets.token_hex(32), encoding="utf-8")
        s.session_secret = secret_file.read_text(encoding="utf-8").strip()
    try:
        load_game_data(s.active_game_data_path)
    except GameDataError as e:
        problems.append(str(e))
    if not s.map_pdf_path.exists():
        logging.warning("Karten-PDF nicht gefunden: %s", s.map_pdf_path)
    if bool(s.ssl_certfile) != bool(s.ssl_keyfile):
        problems.append("Für HTTPS müssen SSL_CERTFILE und SSL_KEYFILE beide gesetzt sein")
    for label, f in (("SSL_CERTFILE", s.ssl_certfile), ("SSL_KEYFILE", s.ssl_keyfile)):
        if f and not f.exists():
            problems.append(f"{label} nicht gefunden: {f}")
    if problems:
        for p in problems:
            logging.error(p)
        return 1

    from .web.app import create_app

    app = create_app(s)
    scheme = "https" if s.https else "http"
    logging.info("Startseite (Sessions): %s://%s:%s/", scheme, "localhost" if s.host == "0.0.0.0" else s.host, s.port)
    ssl = {"ssl_certfile": str(s.ssl_certfile), "ssl_keyfile": str(s.ssl_keyfile)} if s.https else {}
    uvicorn.run(app, host=s.host, port=s.port, log_level="warning", proxy_headers=True, **ssl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
