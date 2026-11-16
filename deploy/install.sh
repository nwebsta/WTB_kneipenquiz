#!/usr/bin/env bash
# =============================================================================
# Kneipenmonopoly – Installation UND Update auf einem Linux-Server
# (Debian, Ubuntu, Raspberry Pi OS)
#
# Erstinstallation (Code liegt per git clone in /opt/kneipenmonopoly):
#     sudo bash /opt/kneipenmonopoly/deploy/install.sh
# Update (holt neuen Code per git pull, Daten/.env/texte.toml bleiben):
#     sudo bash /opt/kneipenmonopoly/deploy/install.sh
#
# Optionen:
#     --configure   Master-Passwort, Port und HTTP/HTTPS (auch HAProxy + Let's Encrypt) neu abfragen
#     --no-pull     keinen git pull machen
#     --no-tests    Tests vor dem Neustart überspringen
#     --tailscale   Tailscale (VPN) installieren und die Seite nur dort anbieten
#
# Ohne Terminal (z.B. automatisiert) lassen sich die Antworten vorgeben:
#     PORT=8443 PROTOCOL=https-selbst MASTER_PASSWORD=... sudo -E bash deploy/install.sh
#     PROTOCOL: http | https-eigen | https-selbst | haproxy
#       https-eigen: zusätzlich CERT_FILE und KEY_FILE
#       haproxy:     zusätzlich DOMAINS ("a.de b.de"), LE_EMAIL, HAPROXY_IPS ("10.0.0.5"),
#                    PUBLIC_HTTP_PORT (80), PUBLIC_HTTPS_PORT (443)
# =============================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="kneipenmonopoly"
APP_USER="${APP_USER:-kneipe}"
ENV_FILE="$APP_DIR/.env"
CERT_DIR="$APP_DIR/certs"

DO_PULL=1; DO_TESTS=1; DO_TAILSCALE=0; CONFIGURE=0
for arg in "$@"; do
  case "$arg" in
    --no-pull) DO_PULL=0 ;;
    --no-tests) DO_TESTS=0 ;;
    --tailscale) DO_TAILSCALE=1 ;;
    --configure) CONFIGURE=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $arg (siehe --help)"; exit 1 ;;
  esac
done

say()  { echo; echo "==> $*"; }
warn() { echo "!!  $*"; }
die()  { echo; echo "FEHLER: $*" >&2; exit 1; }
interactive() { [ -t 0 ]; }

[ "$(id -u)" -eq 0 ] || die "Bitte mit sudo starten:  sudo bash $APP_DIR/deploy/install.sh"
[ -f "$APP_DIR/requirements.txt" ] || die "Das Skript muss im Projektordner liegen (deploy/install.sh)."

# --- .env lesen/schreiben (Werte mit Sonderzeichen sicher) ---------------------
env_get() {  # env_get KEY  -> Wert oder leer
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- || true
}
env_set() {  # env_set KEY VALUE
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  if grep -qE "^$key=" "$ENV_FILE"; then
    # Wert über ENVIRON statt -v: so bleiben Backslashes und andere Sonderzeichen unverändert.
    KM_K="$key" KM_V="$value" awk 'BEGIN{FS="="} $1==ENVIRON["KM_K"] {print ENVIRON["KM_K"] "=" ENVIRON["KM_V"]; next} {print}' \
      "$ENV_FILE" > "$tmp"
  else
    cp "$ENV_FILE" "$tmp"; printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  cat "$tmp" > "$ENV_FILE"; rm -f "$tmp"
}
ask() {  # ask "Frage" "Vorgabe" -> Antwort
  local answer
  if interactive; then
    read -r -p "$1 [$2]: " answer </dev/tty || true
  fi
  echo "${answer:-$2}"
}

# --- 1. Code aktualisieren (vor allem anderen, danach mit neuem Skript weiter) ---
if [ "$DO_PULL" -eq 1 ] && [ -d "$APP_DIR/.git" ] && [ -z "${KM_PULLED:-}" ]; then
  say "Neuen Code holen (git pull)"
  command -v git >/dev/null || { apt-get update -qq; apt-get install -y -qq git >/dev/null; }
  git config --global --add safe.directory "$APP_DIR" || true
  git -C "$APP_DIR" pull --ff-only || die "git pull fehlgeschlagen. Lokale Änderungen auf dem Server? (git -C $APP_DIR status)"
  export KM_PULLED=1
  exec bash "$APP_DIR/deploy/install.sh" "$@"   # neue Skriptversion ausführen
fi

# --- 2. Systempakete ------------------------------------------------------------
say "Systempakete installieren"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv git curl ca-certificates openssl fonts-dejavu-core >/dev/null
echo "    ok"

# --- 3. Python 3.11+ (sonst über uv herunterladen) -------------------------------
say "Python prüfen"
PY="python3"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "    System-Python ist $(python3 --version 2>&1), gebraucht wird 3.11+. Lade Python 3.12 über uv."
  if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
  fi
  export UV_PYTHON_INSTALL_DIR=/opt/uv-python
  uv python install 3.12
  PY="$(uv python find 3.12)"
fi
echo "    $("$PY" --version)  ($PY)"

# --- 4. Dienst-Benutzer ----------------------------------------------------------
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  say "Benutzer $APP_USER anlegen"
  useradd --system --home-dir "$APP_DIR" --no-create-home --shell /usr/sbin/nologin "$APP_USER"
fi

# --- 5. Virtuelle Umgebung + Pakete ----------------------------------------------
say "Python-Pakete installieren (venv)"
if [ -x "$APP_DIR/.venv/bin/python" ] && ! "$APP_DIR/.venv/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  rm -rf "$APP_DIR/.venv"
fi
[ -x "$APP_DIR/.venv/bin/python" ] || "$PY" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install -q --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -q -r "$APP_DIR/requirements.txt"
echo "    ok"

# --- 6. Konfiguration (.env) -------------------------------------------------------
FIRST_RUN=0
if [ ! -f "$ENV_FILE" ]; then
  FIRST_RUN=1
  cp "$APP_DIR/.env.example" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

if [ "$FIRST_RUN" -eq 1 ] || [ "$CONFIGURE" -eq 1 ]; then
  say "Konfiguration"
  # Bot-Token und Passwort gehören jetzt zu jeder Session und werden auf der Startseite der
  # Webseite beim Anlegen einer Session eingegeben. Hier nur das Master-Passwort (alle Sessions verwalten).
  PW="${MASTER_PASSWORD:-$(env_get MASTER_PASSWORD)}"
  if interactive; then
    read -r -s -p "Master-Passwort (Sessions verwalten/löschen) [${PW:+behalten}]: " NEW </dev/tty || true; echo
    PW="${NEW:-$PW}"
  fi
  if [ -z "$PW" ]; then
    PW="$(openssl rand -base64 18 | tr -d '/+=')"
    echo "    Kein Passwort eingegeben, erzeugt: $PW  (bitte notieren)"
  fi
  env_set MASTER_PASSWORD "$PW"

  # Port
  CUR_PORT="$(env_get PORT)"; CUR_PORT="${CUR_PORT:-8000}"
  NEW_PORT="${PORT:-$(ask 'Port der Admin-Seite' "$CUR_PORT")}"
  [[ "$NEW_PORT" =~ ^[0-9]+$ ]] && [ "$NEW_PORT" -ge 1 ] && [ "$NEW_PORT" -le 65535 ] || die "Ungültiger Port: $NEW_PORT"
  env_set PORT "$NEW_PORT"

  # HTTP oder HTTPS
  CUR_PROTO="http"; [ -n "$(env_get SSL_CERTFILE)" ] && CUR_PROTO="https"
  if [ -z "${PROTOCOL:-}" ]; then
    if interactive; then
      echo
      echo "Wie soll die Admin-Seite erreichbar sein?"
      echo "  1) HTTP  (unverschlüsselt, z.B. nur über VPN)"
      echo "  2) HTTPS mit eigenem Zertifikat (Pfade zu Zertifikat und Schlüssel angeben)"
      echo "  3) HTTPS mit selbst erzeugtem Zertifikat (ohne Domain; Browser warnt einmal)"
      echo "  4) Hinter HAProxy (TCP, send-proxy auf HTTP und HTTPS) mit Let's-Encrypt-Zertifikat"
      echo "     (installiert nginx + certbot; die App lauscht dann nur noch auf 127.0.0.1)"
      DEF=1; [ "$CUR_PROTO" = "https" ] && DEF=2; [ "$(env_get PROXY_MODE)" = "haproxy" ] && DEF=4
      CHOICE="$(ask 'Auswahl' "$DEF")"
    else
      CHOICE=1; { [ "$CUR_PROTO" = "https" ] || [ "$(env_get PROXY_MODE)" = "haproxy" ]; } && CHOICE=keep
    fi
    case "$CHOICE" in
      1) PROTOCOL=http ;; 2) PROTOCOL=https-eigen ;; 3) PROTOCOL=https-selbst ;; 4) PROTOCOL=haproxy ;;
      keep) PROTOCOL=keep ;;
      *) die "Ungültige Auswahl: $CHOICE" ;;
    esac
  fi
  mkdir -p "$CERT_DIR"
  # Wechsel weg vom HAProxy-Betrieb: nginx-Seite abschalten, App wieder direkt erreichbar
  if [ "$PROTOCOL" != "haproxy" ] && [ "$PROTOCOL" != "keep" ] && [ "$(env_get PROXY_MODE)" = "haproxy" ]; then
    rm -f /etc/nginx/sites-enabled/kneipenmonopoly
    systemctl reload nginx 2>/dev/null || true
    env_set PROXY_MODE ""; env_set PUBLIC_HTTPS "false"
    [ "$(env_get HOST)" = "127.0.0.1" ] && env_set HOST "0.0.0.0"
  fi
  case "$PROTOCOL" in
    http)
      env_set SSL_CERTFILE ""; env_set SSL_KEYFILE "" ;;
    https-eigen)
      CERT_SRC="${CERT_FILE:-$(ask 'Pfad zum Zertifikat (PEM, ggf. mit Zwischenzertifikaten)' "$(env_get SSL_CERTFILE)")}"
      KEY_SRC="${KEY_FILE:-$(ask 'Pfad zum privaten Schlüssel (PEM)' "$(env_get SSL_KEYFILE)")}"
      [ -f "$CERT_SRC" ] || die "Zertifikat nicht gefunden: $CERT_SRC"
      [ -f "$KEY_SRC" ] || die "Schlüssel nicht gefunden: $KEY_SRC"
      openssl x509 -in "$CERT_SRC" -noout >/dev/null 2>&1 || die "$CERT_SRC ist kein gültiges PEM-Zertifikat"
      [ "$(realpath "$CERT_SRC")" = "$CERT_DIR/cert.pem" ] || cp "$CERT_SRC" "$CERT_DIR/cert.pem"
      [ "$(realpath "$KEY_SRC")" = "$CERT_DIR/key.pem" ] || cp "$KEY_SRC" "$CERT_DIR/key.pem"
      env_set SSL_CERTFILE "$CERT_DIR/cert.pem"; env_set SSL_KEYFILE "$CERT_DIR/key.pem"
      echo "    Zertifikat gültig bis: $(openssl x509 -in "$CERT_DIR/cert.pem" -noout -enddate | cut -d= -f2)" ;;
    https-selbst)
      HOSTNAMES="$(hostname)"; IPS="$(hostname -I 2>/dev/null || true)"
      SAN="DNS:localhost,DNS:$(hostname),IP:127.0.0.1"
      for ip in $IPS; do [[ "$ip" == *:* ]] || SAN="$SAN,IP:$ip"; done
      openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=$HOSTNAMES" \
        -addext "subjectAltName=$SAN" -keyout "$CERT_DIR/key.pem" -out "$CERT_DIR/cert.pem" 2>/dev/null
      env_set SSL_CERTFILE "$CERT_DIR/cert.pem"; env_set SSL_KEYFILE "$CERT_DIR/key.pem"
      echo "    Selbst erzeugtes Zertifikat für: $SAN" ;;
    haproxy)
      CUR_DOMAINS="$(env_get PUBLIC_DOMAINS)"
      DOMAINS_IN="${DOMAINS:-$(ask 'Domain(s), mit Leerzeichen getrennt (z.B. kneipe.example.de)' "$CUR_DOMAINS")}"
      [ -n "$DOMAINS_IN" ] || die "Mindestens eine Domain angeben (DNS muss auf HAProxy zeigen)."
      for d in $DOMAINS_IN; do
        [[ "$d" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || die "Keine gültige Domain: $d"
      done
      EMAIL_IN="${LE_EMAIL:-$(ask "E-Mail für Let's Encrypt (Ablauf-Warnungen)" "$(env_get LE_EMAIL)")}"
      [[ "$EMAIL_IN" == *@*.* ]] || die "Bitte eine gültige E-Mail-Adresse für Let's Encrypt angeben."
      IPS_IN="${HAPROXY_IPS:-$(ask 'IP(s) des HAProxy-Servers, mit Leerzeichen getrennt (für echte Besucher-IPs)' "$(env_get HAPROXY_IPS)")}"
      HP="${PUBLIC_HTTP_PORT:-$(ask 'Port hier, auf den HAProxy HTTP weiterleitet (send-proxy)' "$(env_get PUBLIC_HTTP_PORT | grep . || echo 80)")}"
      SP="${PUBLIC_HTTPS_PORT:-$(ask 'Port hier, auf den HAProxy HTTPS weiterleitet (send-proxy)' "$(env_get PUBLIC_HTTPS_PORT | grep . || echo 443)")}"
      for x in "$HP" "$SP"; do
        [[ "$x" =~ ^[0-9]+$ ]] && [ "$x" -ge 1 ] && [ "$x" -le 65535 ] || die "Ungültiger Port: $x"
      done
      [ "$HP" != "$SP" ] || die "HTTP- und HTTPS-Port müssen verschieden sein."
      APP_PORT="$(env_get PORT)"; APP_PORT="${APP_PORT:-8000}"
      if [ "$APP_PORT" = "$HP" ] || [ "$APP_PORT" = "$SP" ]; then
        die "Der Port der App ($APP_PORT) ist derselbe wie ein nginx-Port. Bitte für die App z.B. 8000 wählen."
      fi
      env_set PUBLIC_DOMAINS "$DOMAINS_IN"; env_set LE_EMAIL "$EMAIL_IN"; env_set HAPROXY_IPS "$IPS_IN"
      env_set PUBLIC_HTTP_PORT "$HP"; env_set PUBLIC_HTTPS_PORT "$SP"
      env_set PROXY_MODE "haproxy"; env_set PUBLIC_HTTPS "true"
      env_set SSL_CERTFILE ""; env_set SSL_KEYFILE ""
      env_set HOST "127.0.0.1"
      echo "    nginx nimmt auf $HP/$SP das PROXY-Protokoll an, die App läuft intern auf 127.0.0.1:$APP_PORT" ;;
    keep) ;;
    *) die "PROTOCOL muss http, https-eigen, https-selbst oder haproxy sein" ;;
  esac
  [ -f "$CERT_DIR/key.pem" ] && chmod 600 "$CERT_DIR/key.pem"
fi

# Update von einer Version ohne Sessions: Master-Passwort = bisheriges Admin-Passwort
if [ -z "$(env_get MASTER_PASSWORD)" ]; then
  OLD_PW="$(env_get ADMIN_PASSWORD)"
  if [ -n "$OLD_PW" ]; then
    env_set MASTER_PASSWORD "$OLD_PW"
    warn "MASTER_PASSWORD war leer und ist jetzt das bisherige ADMIN_PASSWORD. Ändern: --configure"
  else
    NEW_PW="$(openssl rand -base64 18 | tr -d '/+=')"
    env_set MASTER_PASSWORD "$NEW_PW"
    warn "Master-Passwort erzeugt: $NEW_PW  (bitte notieren, steht in $ENV_FILE)"
  fi
fi

# --- 7. Optional: Tailscale (VPN) --------------------------------------------------
if [ "$DO_TAILSCALE" -eq 1 ]; then
  say "Tailscale einrichten"
  command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
  tailscale status >/dev/null 2>&1 || tailscale up
  TS_IP="$(tailscale ip -4 | head -n1)"
  [ -n "$TS_IP" ] || die "Keine Tailscale-IP gefunden"
  env_set HOST "$TS_IP"
  echo "    Admin-Seite nur noch über Tailscale erreichbar: $TS_IP"
fi

# --- 8. Rechte ---------------------------------------------------------------------
mkdir -p "$APP_DIR/data"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$ENV_FILE"

# --- 9. Tests ------------------------------------------------------------------------
if [ "$DO_TESTS" -eq 1 ]; then
  say "Tests laufen lassen"
  if ! runuser -u "$APP_USER" -- env HOME="$APP_DIR" bash -c "cd '$APP_DIR' && .venv/bin/python -m pytest -q -p no:cacheprovider" ; then
    die "Tests fehlgeschlagen. Der Dienst wurde NICHT neu gestartet (läuft ggf. mit dem alten Stand weiter)."
  fi
fi

# --- 10. Datenbanken sichern (alle Sessions + Altbestand) ------------------------------
DBS="$(ls -1 "$APP_DIR"/data/kneipenmonopoly.sqlite3 "$APP_DIR"/data/sessions.sqlite3 \
        "$APP_DIR"/data/sessions/*/kneipenmonopoly.sqlite3 2>/dev/null || true)"
if [ -n "$DBS" ]; then
  say "Datenbanken sichern"
  STAMP="$(date +%Y%m%d-%H%M%S)"
  BACKUP_DIR="$APP_DIR/data/backups/$STAMP"
  mkdir -p "$BACKUP_DIR"
  for DB in $DBS; do
    REL="${DB#"$APP_DIR"/data/}"; NAME="$(echo "$REL" | tr '/' '_')"
    "$APP_DIR/.venv/bin/python" - "$DB" "$BACKUP_DIR/$NAME" <<'PYEOF'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PYEOF
  done
  # nur die letzten 10 Sicherungen behalten (alte Einzeldateien und Ordner)
  ls -1dt "$APP_DIR"/data/backups/* | tail -n +11 | xargs -r rm -rf
  chown -R "$APP_USER:$APP_USER" "$APP_DIR/data/backups"
  echo "    $BACKUP_DIR"
fi

# --- 11. systemd-Dienst ----------------------------------------------------------------
say "Dienst einrichten und (neu) starten"
PORT_NOW="$(env_get PORT)"; PORT_NOW="${PORT_NOW:-8000}"
CAPS=""
[ "$PORT_NOW" -lt 1024 ] && CAPS="AmbientCapabilities=CAP_NET_BIND_SERVICE"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Kneipenmonopoly (Telegram-Bot + Admin-Webseite)
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m kneipenmonopoly
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1
$CAPS

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"

# --- 12. Prüfen ------------------------------------------------------------------------
SCHEME="http"; [ -n "$(env_get SSL_CERTFILE)" ] && SCHEME="https"
HOST_NOW="$(env_get HOST)"; [ -z "$HOST_NOW" ] || [ "$HOST_NOW" = "0.0.0.0" ] && HOST_NOW="127.0.0.1"
OK=0
for _ in $(seq 1 30); do
  if curl -fsk "$SCHEME://$HOST_NOW:$PORT_NOW/healthz" >/dev/null 2>&1; then OK=1; break; fi
  sleep 1
done
if [ "$OK" -ne 1 ]; then
  journalctl -u "$SERVICE" -n 30 --no-pager || true
  die "Der Dienst antwortet nicht. Log oben, live mit: journalctl -u $SERVICE -f"
fi

# --- 13. nginx + Let's Encrypt (nur im HAProxy-Betrieb) -------------------------------
nginx_conf() {  # nginx_conf MIT_TLS(0/1)
  local domains http_port https_port app_port ips real_ip="" ip
  domains="$(env_get PUBLIC_DOMAINS)"; http_port="$(env_get PUBLIC_HTTP_PORT)"; https_port="$(env_get PUBLIC_HTTPS_PORT)"
  app_port="$(env_get PORT)"; app_port="${app_port:-8000}"; ips="$(env_get HAPROXY_IPS)"
  for ip in $(echo "$ips" | tr ',' ' '); do real_ip="$real_ip    set_real_ip_from $ip;"$'\n'; done
  [ -n "$real_ip" ] && real_ip="$real_ip    real_ip_header proxy_protocol;"$'\n'
  cat <<NGX
# Erzeugt von deploy/install.sh – Änderungen werden beim nächsten Lauf überschrieben.
# HAProxy (TCP-Modus, send-proxy) -> nginx (PROXY-Protokoll, TLS) -> App auf 127.0.0.1:$app_port
server {
    listen $http_port proxy_protocol;
    server_name $domains;
$real_ip
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/certbot;
        default_type text/plain;
    }
    location / {
        return 301 https://\$host\$request_uri;
    }
}
NGX
  [ "$1" = "1" ] || return 0
  cat <<NGX

server {
    listen $https_port ssl proxy_protocol;
    server_name $domains;
$real_ip
    ssl_certificate     /etc/letsencrypt/live/kneipenmonopoly/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/kneipenmonopoly/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:kneipe_ssl:10m;
    add_header Strict-Transport-Security "max-age=31536000" always;
    client_max_body_size 10m;

    location / {
        proxy_pass http://127.0.0.1:$app_port;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$proxy_protocol_addr;
        proxy_set_header X-Forwarded-For \$proxy_protocol_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 120s;
    }
}
NGX
}

if [ "$(env_get PROXY_MODE)" = "haproxy" ]; then
  say "nginx und Let's-Encrypt-Zertifikat (HAProxy mit send-proxy)"
  apt-get install -y -qq nginx certbot >/dev/null
  mkdir -p /var/www/certbot
  rm -f /etc/nginx/sites-enabled/default        # lauscht sonst ohne PROXY-Protokoll auf Port 80
  SITE=/etc/nginx/sites-available/kneipenmonopoly
  LIVE=/etc/letsencrypt/live/kneipenmonopoly
  WANT="$(env_get PUBLIC_DOMAINS)"
  HAVE=""
  [ -f "$LIVE/cert.pem" ] && HAVE="$(openssl x509 -in "$LIVE/cert.pem" -noout -ext subjectAltName 2>/dev/null \
                                     | grep -o 'DNS:[^,]*' | sed 's/DNS://' | sort | tr '\n' ' ')"
  WANT_SORTED="$(echo $WANT | tr ' ' '\n' | sort | tr '\n' ' ')"
  if [ ! -f "$LIVE/fullchain.pem" ] || [ "$HAVE" != "$WANT_SORTED" ]; then
    # Erst nur HTTP (für die Prüfung von Let's Encrypt), dann Zertifikat holen
    nginx_conf 0 > "$SITE"
    ln -sf "$SITE" /etc/nginx/sites-enabled/kneipenmonopoly
    nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx-Konfiguration fehlerhaft"; }
    systemctl enable --now nginx >/dev/null 2>&1; systemctl reload nginx
    DARGS=""; for d in $WANT; do DARGS="$DARGS -d $d"; done
    # shellcheck disable=SC2086
    certbot certonly --webroot -w /var/www/certbot --cert-name kneipenmonopoly $DARGS \
      --email "$(env_get LE_EMAIL)" --agree-tos --non-interactive --keep-until-expiring --expand \
      || die "Zertifikat konnte nicht geholt werden. Prüfen: DNS von $WANT zeigt auf HAProxy, HAProxy leitet Port 80 \
per TCP mit send-proxy auf Port $(env_get PUBLIC_HTTP_PORT) dieses Servers weiter."
  fi
  nginx_conf 1 > "$SITE"
  ln -sf "$SITE" /etc/nginx/sites-enabled/kneipenmonopoly
  nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx-Konfiguration fehlerhaft"; }
  systemctl enable --now nginx >/dev/null 2>&1; systemctl reload nginx
  # Automatische Erneuerung: certbot.timer + nginx neu laden
  mkdir -p /etc/letsencrypt/renewal-hooks/deploy
  printf '#!/bin/sh\nsystemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/kneipenmonopoly-nginx.sh
  chmod 755 /etc/letsencrypt/renewal-hooks/deploy/kneipenmonopoly-nginx.sh
  systemctl enable --now certbot.timer >/dev/null 2>&1 || warn "certbot.timer nicht gefunden: Erneuerung per Cron einrichten (certbot renew)"
  echo "    Zertifikat gültig bis: $(openssl x509 -in "$LIVE/cert.pem" -noout -enddate | cut -d= -f2)"
fi

say "Fertig"
VERSION="$(git -C "$APP_DIR" log -1 --format='%h %cd' --date=format:'%d.%m.%Y %H:%M' 2>/dev/null || echo '?')"
echo "    Version:      $VERSION"
if [ "$(env_get PROXY_MODE)" = "haproxy" ]; then
  MAIN_DOMAIN="$(env_get PUBLIC_DOMAINS | awk '{print $1}')"
  echo "    Startseite:   https://$MAIN_DOMAIN/   (Sessions anlegen/anmelden)"
  echo "    Master:       https://$MAIN_DOMAIN/master"
  echo "    HAProxy:      TCP-Modus, 'send-proxy' auf beiden Backends (HTTP -> :$(env_get PUBLIC_HTTP_PORT), HTTPS -> :$(env_get PUBLIC_HTTPS_PORT))"
  echo "    Erneuerung:   automatisch (certbot.timer), Test: sudo certbot renew --dry-run"
else
  echo "    Startseite:   $SCHEME://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT_NOW/   (Sessions anlegen/anmelden)"
  echo "    Master:       $SCHEME://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT_NOW/master"
fi
[ "$SCHEME" = "https" ] && [ "${PROTOCOL:-}" = "https-selbst" ] && \
  echo "    Hinweis:      Selbst erzeugtes Zertifikat – der Browser warnt einmal, „Trotzdem fortfahren“."
if [ "$(env_get HOST)" = "0.0.0.0" ] && [ "$DO_TAILSCALE" -eq 0 ]; then
  warn "Die Seite lauscht auf allen Netzwerken. Auf einem Server im Internet ist sie damit öffentlich"
  warn "erreichbar (nur durch das Passwort geschützt). Besser: --tailscale oder eine Firewall (ufw)."
fi
echo "    Status:       systemctl status $SERVICE"
echo "    Log:          journalctl -u $SERVICE -f"
echo "    Update:       sudo bash $APP_DIR/deploy/install.sh"
