#!/usr/bin/env bash
# Startet Bot und Admin-Seite (Linux/Raspberry Pi). Legt beim ersten Mal das venv an.
set -u
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi
if [ ! -f .env ]; then
  echo "Bitte zuerst .env.example nach .env kopieren und ausfüllen." >&2
  exit 1
fi
while true; do
  .venv/bin/python -m kneipenmonopoly
  code=$?
  [ "$code" -eq 1 ] && exit 1   # Konfigurationsfehler
  echo "Prozess beendet (Code $code), Neustart in 3 Sekunden ..."
  sleep 3
done
