# Kneipenmonopoly Rüttenscheid

Telegram-Bot für die Teams und Admin-Webseite für die Spielleitung, in **einem** Python-Prozess.
Der Spielstand wird ausschließlich aus dem Ereignisprotokoll berechnet. Dadurch lässt sich jede
Freigabe und jede Korrektur zurücknehmen, und nach einem Absturz geht es mit demselben Stand weiter.

## Inhalt

1. [Spielregeln, wie sie umgesetzt sind](#1-spielregeln-wie-sie-umgesetzt-sind)
2. [Einrichtung](#2-einrichtung)
3. [Telegram-Bot bei BotFather anlegen](#3-telegram-bot-bei-botfather-anlegen)
4. [Starten](#4-starten)
5. [Zugriff von unterwegs über VPN](#5-zugriff-von-unterwegs-über-vpn)
6. [Ablauf am Abend](#6-ablauf-am-abend)
7. [Tests](#7-tests)
8. [Generalprobe-Checkliste](#8-generalprobe-checkliste)
9. [Technik und Aufbau](#9-technik-und-aufbau)

---

## 1. Spielregeln, wie sie umgesetzt sind

Alle Zahlen stehen in `.env` und sind änderbar.

| Situation | Wirkung nach Freigabe des Fotos |
|---|---|
| Freie Bar | Team bekommt die Bar, **+1**. Abschlussfoto mit allen Bieren |
| Bar eines anderen Teams | Besitzer **+1**, mit kompletter Straße **+2**. Besucher **0** |
| Freie Bar oder freier Bahnhof, anderes Team schon drin | **Wettrennen:** Wer zuerst das Foto hochlädt, bekommt die Bar. Ein späteres Foto zählt als Besuch, und der Besitzer kassiert. Oder **Kurzen & gehen:** Check-in beenden, keine Punkte |
| Aktionskarte (Button „Aktionskarte ziehen“) | per Videoanruf geprüft: geschafft **+1**, nicht geschafft **−1** |
| Ereigniskarte „doppelte Punkte“ | ab 2:30 nach Start zählen alle Punkte doppelt, nach Uploadzeit des Fotos bzw. Zeit der Anfrage |
| Freier Bahnhof | Team bekommt ihn, Punkte = Anzahl eigener Bahnhöfe danach (1, 2, 3, 4) |
| Bahnhof eines anderen Teams | Besitzer bekommt Punkte = Anzahl seiner Bahnhöfe |
| Straße komplett | einmalig **+2**. Zerfällt die Straße durch Korrektur, wird der Bonus wieder abgezogen |
| Aufgabe erfüllt | **+1**, vom Admin beim Prüfen angehakt. Keine Pflicht für die Freigabe |
| Eigene Bar | Check-in wird abgelehnt, keine Punkte |

Weitere Regeln:

- **Besitz** wechselt nie durch Besuch, nur durch Admin-Korrektur.
- **Wettrennen:** Ist ein anderes Team schon in einer freien Bar oder an einem freien Bahnhof eingecheckt, sagt der Bot das beim Check-in.
  Er zeigt dazu den Button „Kurzen & gehen“. Wer ein Foto schickt, ist im Wettrennen. Ein Videoanruf ist dafür nicht nötig.
  Den Button „Aktionskarte ziehen“ zeigt der Bot im Wettrennen bewusst nicht.
  Läuft beim ersten Team gerade eine Aktion, bekommt es die Aktionspunkte trotzdem. Die Bar geht aber an das Team mit dem ersten Foto.
- **Ereigniskarten** (Standard, in der Verwaltung änderbar), gerechnet ab Spielstart:
  - **0:45:** „0,5 statt 0,33“, angezeigt beim nächsten Kneipen-Check-in jedes Teams.
  - **1:30:** „Shot statt 0,33“, ebenso.
  - **2:30:** Doppelte Punkte bis Spielende. Die Karte geht sofort an alle. Es zählt die Uploadzeit der Fotos.
  - Pro Team zeigt der Bot höchstens eine Karte pro Check-in. Verfällt der Check-in ohne Foto, kommt die Karte beim nächsten wieder.
    Bahnhöfe zählen dafür nicht. In der Prüf-Warteschlange steht die Karte beim Foto.
- **Aktionskarten:** Jede Karte kann ein Team höchstens einmal ziehen. Es gibt so viele, wie in der Verwaltung angelegt sind.
- **Abklingzeit:** Dieselbe Bar bzw. derselbe Bahnhof zählt für ein Team erst nach 30 Minuten wieder.
- **Check-in-Frist:** 20 Minuten bis zum Foto, sonst verfällt der Check-in.
- **Abgelehntes Foto:** Das Team darf ohne neuen Check-in ein neues schicken. Die Frist beginnt neu.
- **Gleichzeitig in derselben freien Bar:** Das zuerst *eingereichte* Foto gewinnt. Die Webseite sperrt
  „Gültig“ für ein jüngeres Foto, bis das ältere geprüft ist.
- **Pause:** keine Check-ins, laufende Fristen werden angehalten.
- **Spielzeit:** Eine Spieldauer, z.B. 3:00, startet mit dem Spiel. Eine Pause hält die Spieluhr an und verlängert das Spiel. Warnungen kommen 30 und 10 Minuten vor Ende. Ereigniskarten richten sich ebenfalls nach der Spielzeit.
  Danach eingereichte Fotos zählen nicht. Schon eingereichte werden noch geprüft, danach kommt der Endstand.
- **Sieger:** das Team mit den meisten Punkten. Bei Gleichstand teilen sich die Teams den Platz.
- **Reserve-Bars** sind nur Ersatz. Ein Ersatzfeld übernimmt Nummer, Straße und Besitzer.
- **Geschlossene Bar ohne Besitzer** zählt für „Straße komplett“ nicht mehr mit.

---

## 2. Einrichtung

Voraussetzung: Python 3.11 oder neuer. Alles läuft in einem **venv** im Projektordner.

**Windows (PowerShell):**

```powershell
cd D:\Projekte\WTB_kneipenquiz
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
notepad .env
```

**Linux / Raspberry Pi:**

```bash
cd ~/kneipenmonopoly
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env
```

In `.env` mindestens eintragen:

- `BOT_TOKEN`: von BotFather, siehe nächster Abschnitt.
- `ADMIN_PASSWORD`: gemeinsames Passwort aller Admins.
- `SESSION_SECRET` ist optional. Bleibt er leer, wird er beim ersten Start erzeugt und in `data/session_secret` gespeichert.

**Die Datei nach dem Ausfüllen speichern.** Ohne `ADMIN_PASSWORD` startet der Server nicht.

Die Spieldaten kommen aus `kneipenmonopoly_spieldaten.json`, die Karte aus `Kneipenmonopoly_Spielplan.pdf`.
Beide liegen im Projektordner. Andere Pfade lassen sich in `.env` setzen.

Fotos, Datenbank und Log landen im Ordner `data/`.

---

## 3. Telegram-Bot bei BotFather anlegen

1. In Telegram **@BotFather** öffnen und `/newbot` schicken. Namen und Benutzernamen wählen.
2. Den Token in `.env` als `BOT_TOKEN` eintragen.
3. **Privacy Mode ausschalten**, sonst sieht der Bot in Gruppen keine Fotos und Standorte:
   `/mybots` → Bot wählen → *Bot Settings* → *Group Privacy* → **Turn off**.
4. Optional die Befehlsliste setzen: `/mybots` → Bot → *Edit Bot* → *Edit Commands* und einfügen:

   ```
   join - Chat mit eurem Team verknüpfen
   stand - Punktestand und eure Felder
   felder - Wem gehört was?
   karte - Karte nochmal schicken
   regeln - Die Regeln
   hilfe - Hilfe
   ```

> **Wichtig:** Wurde der Bot *vor* dem Ausschalten des Privacy Mode in eine Gruppe aufgenommen, muss man ihn
> aus der Gruppe entfernen und neu hinzufügen. Alternativ macht man den Bot in der Gruppe zum Admin.
>
> Dabei wandelt Telegram die Gruppe oft in eine „Supergruppe“ mit neuer Chat-Nummer um. Der Bot erkennt das und
> übernimmt die neue Nummer automatisch. Ein neues `/join` ist nicht nötig.
> Admins sehen in Telegram immer alle Nachrichten.

**Pro Team:** Ein Teammitglied erstellt eine Telegram-Gruppe, fügt den Bot und die anderen Mitglieder hinzu
und schickt dort `/join CODE`. Den Code legt die Spielleitung in der Verwaltung an.

---

## 4. Starten

Unter Windows am einfachsten **`start.cmd` doppelklicken**. Das umgeht auch die Windows-Sperre für
PowerShell-Skripte. Aus einer offenen PowerShell geht auch:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

Bei einem Konfigurationsfehler bleibt das Fenster offen und zeigt die Meldung an.

```bash
./start.sh           # Linux / Raspberry Pi
```

Beide Skripte legen das venv beim ersten Mal selbst an, gleichen die Pakete bei jedem Start ab und starten
den Prozess nach einem Absturz neu. Fehlt unter Windows Python 3.11+, installiert `start.ps1` Python 3.12 über
`winget`. Direkt geht es auch mit `.venv\Scripts\python -m kneipenmonopoly`.

Die Admin-Seite läuft auf **http://localhost:8000/**. Der Bot nutzt Long-Polling und braucht nur
ausgehendes Internet. Es muss kein Port nach außen geöffnet werden.

Auf einem Linux-Server oder Raspberry Pi als Dienst mit Autostart: siehe Abschnitt **Auf einem Server installieren**.

**Neustartfest:** Alles steht in SQLite (`data/kneipenmonopoly.sqlite3`). Nach einem Neustart sind
Spielstand, offene Check-ins, Fristen und die Prüf-Warteschlange wieder da. Telegram liefert Nachrichten,
die während des Neustarts ankamen, nach.

**Laptop als Server:** Energiesparmodus und Ruhezustand ausschalten, Netzteil dranlassen.
Ein mobiler Hotspot reicht als Internet.

### Auf einem Server installieren (Linux)

Für Debian, Ubuntu und Raspberry Pi OS gibt es `deploy/install.sh`. Das Skript macht **Installation und Update**:

- **Pakete:** Es installiert alle Systempakete per `apt`. Ist das System-Python älter als 3.11, etwa auf Ubuntu 22.04,
  lädt es Python 3.12 über `uv` herunter.
- **Programm:** Es legt das venv an, installiert die Python-Pakete und den Dienst-Benutzer `kneipe`.
- **Beim ersten Lauf** fragt es nach Bot-Token, Admin-Passwort, **Port** und **HTTP oder HTTPS**:
  1. HTTP ohne Verschlüsselung, etwa nur über VPN
  2. HTTPS mit **eigenem Zertifikat**: Pfade zu Zertifikat und Schlüssel im PEM-Format angeben
  3. HTTPS mit **selbst erzeugtem Zertifikat**: ohne Domain, der Browser warnt einmal
- **Vor jedem Neustart** laufen die Tests, und die Datenbank wird gesichert. Es bleiben die letzten 10 Sicherungen in `data/backups/`.
- **Dienst:** Es richtet einen systemd-Dienst mit Autostart ein, startet ihn und prüft, ob die Seite antwortet.

**1. Code nach GitHub pushen** (auf deinem Windows-Rechner; das Repository ist `github.com/nwebsta/WTB_kneipenquiz`):

```powershell
cd D:\Projekte\WTB_kneipenquiz
git rm --cached texte.toml        # nur einmal nötig: Texte nicht mehr im Repository verfolgen
git add .
git commit -m "Spielzeit, HTTPS, Server-Installation"
git push
```

`.env`, `data/`, Zertifikate und `texte.toml` landen nicht im Repository (siehe `.gitignore`).
Die Texte legt der Server beim ersten Start aus den Standardtexten an. Angepasste Texte kopierst du einmalig
mit `scp texte.toml BENUTZER@SERVER:/tmp/` und `sudo cp /tmp/texte.toml /opt/kneipenmonopoly/` hinüber.

**2. Auf dem Server initial holen und installieren:**

```bash
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/nwebsta/WTB_kneipenquiz.git /opt/kneipenmonopoly
sudo bash /opt/kneipenmonopoly/deploy/install.sh
```

Bei einem **privaten Repository** fragt `git clone` nach Benutzername und Passwort. Als Passwort gibst du ein
GitHub-Token mit Leserecht ein (GitHub → Settings → Developer settings → Fine-grained tokens, Recht „Contents: Read“).

**3. Updates:** Neuen Stand vom Windows-Rechner pushen (`git add .`, `git commit -m "…"`, `git push`), dann auf dem Server:

```bash
sudo bash /opt/kneipenmonopoly/deploy/install.sh
```

Das holt den Code per `git pull`, aktualisiert die Pakete, testet, sichert die Datenbank und startet neu.
Spielstand, `.env` und `texte.toml` bleiben erhalten.

**4. Einrichtung vom Laptop auf den Server übertragen:** Spielstand und Einrichtung liegen in `data/` und gehen
nicht über Git. Dafür gibt es unter Verwaltung → **Export / Import**:

1. Auf dem Laptop **Setup exportieren** klicken. Die heruntergeladene JSON-Datei enthält Teams, Aktionskarten,
   Ereigniskarten, Aufgaben, Spieldauer und alle Kneipen, Bahnhöfe und Reserve-Bars.
2. Auf dem Server dieselbe Datei unter **Importieren** hochladen und die gewünschten Teile anhaken.

Importieren geht nur in der Vorbereitung, also vor dem Start oder nach „Spiel zurücksetzen“.
Telegram-Verknüpfungen, Spielstand, Chats, Fotos und Passwörter werden nicht übertragen.
Mit **Nur Kneipen/Bahnhöfe exportieren** bekommst du die Karte im Format der Spieldaten-Datei, etwa um einen Kiosk
zu verschieben. Nach dem Bearbeiten lädst du sie genauso wieder hoch. Importierte Kneipen liegen in
`data/spieldaten.json` und gelten vor der Datei aus dem Repository. **Import verwerfen** stellt den Repository-Stand
wieder her. Die Spielkarte für die Teams wird automatisch aus den importierten Daten neu gezeichnet (siehe **Spielkarte**).

| Option | Wirkung |
|---|---|
| `--configure` | Port, HTTP/HTTPS, Token und Passwort neu abfragen |
| `--tailscale` | Tailscale installieren und die Seite nur im VPN anbieten |
| `--no-tests` | Tests überspringen |
| `--no-pull` | keinen `git pull` machen |

Nützliche Befehle auf dem Server:

```bash
systemctl status kneipenmonopoly        # läuft der Dienst?
journalctl -u kneipenmonopoly -f        # Log live
sudo nano /opt/kneipenmonopoly/.env     # Einstellungen, danach: sudo systemctl restart kneipenmonopoly
sudo nano /opt/kneipenmonopoly/texte.toml   # Texte, wirken ohne Neustart
```

**Achtung:** Auf einem Server im Internet ist die Seite mit `HOST=0.0.0.0` öffentlich erreichbar und nur durch
das Passwort geschützt. Besser ist `--tailscale`, oder du lässt den Port in der Firewall nur aus dem VPN zu.
Mit HTTPS sind Passwort und Fotos zumindest verschlüsselt unterwegs.

---

## 5. Zugriff von unterwegs über VPN

Die Admin-Seite ist bewusst **nicht öffentlich**. Admins erreichen sie über ein VPN.

### Variante A: Tailscale (am einfachsten)

1. Auf dem Server-Rechner und auf allen Admin-Handys bzw. -Laptops die App **Tailscale** installieren und mit
   demselben Konto oder Tailnet anmelden.
2. In der Tailscale-App die Adresse des Servers ablesen, z.B. `100.101.102.103` oder den Gerätenamen.
3. Am Handy aufrufen: **http://100.101.102.103:8000/**

Der Verkehr ist durch das VPN verschlüsselt, auch ohne HTTPS. Wer zusätzlich HTTPS möchte, kann auf dem Server
`tailscale serve --bg 8000` ausführen und die Seite dann über `https://<gerätename>.<tailnet>.ts.net/` öffnen.

### Variante B: vorhandenes VPN, z.B. FritzBox oder WireGuard

Läuft der Server im Heimnetz, verbinden sich die Admins per VPN ins Heimnetz und rufen
**http://<LAN-IP-des-Servers>:8000/** auf.

### Firewall

- **Windows:** Beim ersten Start fragt Windows, ob Python Netzwerkzugriff bekommen soll. Für **private**
  Netzwerke erlauben. Sonst manuell freigeben:

  ```powershell
  New-NetFirewallRule -DisplayName "Kneipenmonopoly" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
  ```

- **Nur VPN zulassen:** In `.env` `HOST` auf die VPN-Adresse des Servers setzen, z.B. `HOST=100.101.102.103`.
  Dann ist die Seite im lokalen WLAN nicht sichtbar.

---

## 6. Ablauf am Abend

**Vorher:**

1. Verwaltung → **Teams anlegen**: Name, Farbe, Code. Ein leerer Code wird zufällig erzeugt.
2. Codes an die Teams geben. Jedes Team schickt in seiner Gruppe `/join CODE`.
   Im Dashboard verschwindet dann „kein Chat“.
3. Optional **Aufgaben** pro Feld und **Zufallsaufgaben** pflegen.
4. **Aktionskarten** anlegen und **Ereigniskarten** prüfen.
5. Alle Admins, die Videoanrufe machen, treten den Team-Gruppen in Telegram bei.
6. **Spieldauer** (H:MM, z.B. 3:00) eintragen. Beim Start bekommen die Teams „Ihr habt 3 Stunden Spielzeit, Schluss ca. um …“.

**Start:** Verwaltung → **Spiel starten**. Alle Teams bekommen die Spielkarte als Bild und die Regeln.

**Währenddessen:**

- **Prüfen:** Älteste Fotos zuerst. Die Seite aktualisiert sich alle 4 Sekunden, aber nicht, während man
  einen Ablehnungsgrund tippt. Zu jedem Foto stehen Team, Feld, Entfernung beim Check-in, Aufgabe
  und die **Wirkung** der Freigabe. Liegt die Entfernung über 35 Metern, ist sie orange markiert.
  Standorte lassen sich in Telegram fälschen. Foto und Entfernung sind die eigentliche Kontrolle.
- **Live-Standort beim Prüfen:** Teilt ein Team seinen Live-Standort, steht beim Foto, wie weit es von der Bar
  entfernt war, und zwar beim Check-in, beim Hochladen des Fotos und jetzt gerade.
  Grün heißt innerhalb des Check-in-Radius, orange bis doppelter Radius, rot weiter weg.
  Dahinter steht, wie alt die Position ist. Telegram schickt nur bei Bewegung neue Positionen, sitzt ein Team still, wird sie älter.
  Die Spieler sehen davon nichts.
- Klicken zwei Admins gleichzeitig, gewinnt der erste. Der zweite sieht „Schon erledigt von …“.
- **Videoanrufe für Aktionskarten** stehen in derselben Warteschlange, orange markiert. Angezeigt werden Team,
  Name der Telegram-Gruppe, Aufgabe und die Kneipe des letzten Check-ins.
  Ruf die Gruppe in Telegram per Video an und prüfe dabei, ob das Team wirklich dort ist.
  Danach klickst du **Geschafft** oder **Nicht geschafft**. **Abbrechen** gibt keine Punkte und legt die Karte zurück.
  **Wer anruft, muss Mitglied der Telegram-Gruppe des Teams sein.** Also vorher in alle Team-Gruppen eintreten.
- **Ereigniskarten** laufen automatisch. Das Dashboard zeigt, wann welche fällig ist.
- **Karte:** Felder in Straßenfarbe, Besitzer als dicker Rand, Bahnhöfe als Quadrate,
  letzter Check-in je Team als 🚩. Teilt ein Team seinen Live-Standort, erscheint es als pulsierender 📡-Punkt
  mit Alter der Position und Genauigkeitskreis. Ist die Position älter als 15 Minuten oder das Teilen beendet,
  wird der Punkt blass. Oben rechts lassen sich Live-Standorte und Check-ins ein- und ausblenden.
  Die Karte aktualisiert sich alle 10 Sekunden.
- **Chats:** Alle Nachrichten, Fotos und Standorte, filterbar nach Team. Dort kann man auch an ein Team oder an alle schreiben.
  Der Filter **nicht verknüpfte Chats** zeigt Gruppen, die dem Bot schreiben, aber noch keinem Team zugeordnet sind,
  etwa vor dem `/join` oder nach einem falschen Code.
- **Bar hat zu?** Verwaltung → Felder → **Ersetzen** durch eine Reserve-Bar. Besitz bleibt, alle Teams
  werden informiert. Alternativ **Deaktivieren**.
- **Fehler korrigieren:** Verwaltung → Korrekturen (Besitzer setzen, Punkte buchen, immer mit Kommentar).
  Protokoll → **↩️** nimmt eine Freigabe oder Korrektur zurück. Der Stand wird komplett neu berechnet,
  inklusive Straßenbonus. Betroffene Teams bekommen eine Nachricht.

**Ende:** automatisch nach Ablauf der Spielzeit oder per **Spiel beenden**. Sobald keine Fotos mehr offen sind, geht der
Endstand an alle Teams. Ein Klick auf „Endstand nochmal senden“ wiederholt das.

**Danach:** Protokoll → **Export** (CSV/JSON) sichern. Dann Verwaltung → **Datenschutz** →
`LÖSCHEN` eintippen. Das entfernt Fotos, Chats und Standorte. Spielstand und Protokoll bleiben.

### Live-Simulation

Verwaltung → **Live-Simulation** → Teams auswählen → **Simulation starten**. Die Teams spielen dann automatisch:

- **Start:** Alle Teams starten zentral in der Mitte des Spielfelds. Jedes sucht sich rein zufällig eine Bar oder einen Bahnhof aus.
- **Tempo:** Von „Realistisch“ bis „Turbo“ wählbar, auch während die Simulation läuft.
- **Unterwegs:** Sie laufen mit Live-Standort über die Karte.
- **In der Bar:** Sie checken ein, schicken erzeugte Fotos in Teamfarbe mit Biergläsern, geraten in Wettrennen und ziehen Aktionskarten.

Das Dashboard zeigt, was gerade passiert. Karte, Prüfen und Chats zeigen alles wie im echten Spiel.

- **Fotos und Aktionen automatisch freigeben:** Sonst prüfst du selbst unter Prüfen.
- **Bot-Nachrichten wirklich senden:** Ohne diesen Haken bleiben die Telegram-Gruppen ruhig. Die Antworten des Bots stehen dann nur in den Chats der Admin-Seite.
- Die Simulation schreibt in den echten Spielstand. Danach **Spiel zurücksetzen**, das stoppt sie auch.

### Spielkarte

Die Teams bekommen die Karte beim Spielstart und mit `/karte` als **Bild**. Der Server zeichnet es selbst aus
den aktiven Spieldaten, also auch aus importierten eigenen Kneipen:

- **Hintergrund:** OpenStreetMap
- **Straßen:** farbige Verbindungslinien
- **Felder:** nummerierte Punkte, Kneipen größer mit schwarzem Rand, Bahnhöfe als Quadrate mit Buchstaben, Reserve-Bars grau
- **Legende:** rechts daneben, wie in der PDF

Liegen Felder sehr dicht beieinander, werden die Punkte leicht auseinandergerückt. Ein kurzer Strich zeigt dann
auf die echte Stelle. Ersetzte oder geschlossene Bars sind berücksichtigt. Besitzer stehen nie auf der Karte.

Verwaltung → **Spielkarte**: **Vorschau erzeugen** zeichnet das Bild sofort und zeigt es an, zum Prüfen oder
Herunterladen. Sonst wird es automatisch neu gezeichnet, sobald sich Kneipen oder Straßen geändert haben.
Die Kartenkacheln werden in `data/tiles/` gespeichert. Ohne Internet entsteht ein Bild ohne Stadtplan-Hintergrund,
und die Verwaltung weist darauf hin. Klappt das Bild gar nicht, schickt der Bot ersatzweise die PDF.
`MAP_TILES=false` in der `.env` schaltet den Hintergrund ab.

### Testmodus für Demo und Probelauf

Verwaltung → **Testmodus an**. Dann können Teams im Telegram-Chat ohne Standort einchecken:

```
/checkin 15     Feld 15 (Die Eule)
/checkin N      Nordbahnhof (N, O, S, W)
/c 15           Kurzform
```

Danach läuft alles wie im echten Spiel: Aufgabe, Foto, Prüfung, Punkte, Straßenbonus.
Die Regeln gelten weiter, also auch eigene Bar, Abklingzeit und Pause. Die Feldnummern zeigt `/felder`.
Oben auf jeder Admin-Seite steht ein gelbes Banner. In der Prüf-Warteschlange sind solche Check-ins markiert.

Nach der Demo: Verwaltung → **Spiel zurücksetzen** (`RESET` eintippen). Das löscht Spielstand, Check-ins,
Fotos und Chats. Teams, Telegram-Verknüpfungen, Aufgaben und Einstellungen bleiben, und das Spiel steht
wieder auf Vorbereitung. **Vor dem echten Abend den Testmodus ausschalten.**

### Hinweise zu den Spieldaten

- **Grün** (Wirtshaus Rü, Zizou, Zucca) und **Dunkelblau** liegen nur 20 bis 60 Meter auseinander. Der Bot
  fragt dort per Button, welche Bar gemeint ist. Das ist so gewollt.
- **Fritzpatrick's und Hudson's** haben dieselbe Adresse, aber Punkte etwa 50 Meter auseinander. Vor Ort prüfen.
- Der **Nordbahnhof** (Shell) ist als „nicht bestätigt geöffnet“ markiert. Vorher prüfen, sonst ersetzen oder deaktivieren.
- Die Koordinaten sind auf 10 bis 30 Meter genau. Wenn Check-ins zu oft scheitern, `CHECKIN_RADIUS_M` erhöhen
  und neu starten.

### Datenschutz

- Beim `/join` bekommen die Spieler den Hinweis, dass Nachrichten, Fotos, Check-in-Standorte und ein geteilter
  Live-Standort für die Spielleitung sichtbar sind.
- **Standorte** werden beim Check-in gespeichert.
- **Live-Standort** löst keinen Check-in aus. Teams starten ihn zu Spielbeginn oder bei Problemen. Zum Einchecken
  schicken sie zusätzlich einmal ihren normalen Standort, darauf weist der Bot beim Start des Live-Teilens hin.
- Pro Team wird nur die jeweils letzte Live-Position gespeichert und bei jedem Update überschrieben.
  Es entsteht kein Bewegungsverlauf. Zusätzlich merkt sich der Bot beim Check-in und beim Foto die Entfernung zur Bar.
- Nach dem Spiel löscht der Datenschutz-Button alle personenbezogenen Daten, auch die Live-Positionen.

### Texte anpassen

Alle Texte, die der Bot an die Teams schickt, stehen in **`texte.toml`** im Projektordner.
Datei öffnen, Text ändern, speichern. Der Bot übernimmt das sofort, ohne Neustart.

```toml
[checkin]
# {bar}, {meter}
feld_zu_weit = "📍 {bar} ist ca. {meter} m weg. Etwas näher ran!"
```

- **Platzhalter** in `{geschweiften Klammern}` setzt der Bot ein. Welche es gibt, steht im Kommentar über dem Text.
- **Formatierung** erfolgt mit `<b>fett</b>` und `<i>kursiv</i>`. Ein echtes `&` schreibst du als `&amp;`.
- **Mehrzeilige Texte** stehen zwischen `'''` und `'''`.
- **Bei Tippfehlern** in der Datei läuft der Bot mit dem Standardtext weiter. Oben auf der Admin-Seite erscheint eine rote Warnung.
- Die Originaltexte liegen in `kneipenmonopoly/texte_standard.toml`. Fehlt `texte.toml`, legt der Bot sie beim Start als Kopie an.

Meldungen der Admin-Webseite, etwa „Freigegeben“, stehen nicht in der Datei. Sie sehen nur Admins.

---

## 7. Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

| Datei | Prüft |
|---|---|
| `tests/test_rules.py` | Punkte-Logik als reine Funktion: alle Fälle aus der Regeltabelle, Straßenbonus, Bahnhöfe, Rückgängig, Reihenfolge nach Einreichzeit, Ersatz und Deaktivierung, Rangliste |
| `tests/test_service.py` | Join, Radius, Mehrfachauswahl, eigene Bar, Abklingzeit, doppelte Freigabe, Sperre bei gleichzeitigem Check-in, Ablehnung, Timeout, Pause, Spielzeit, Export, Löschen |
| `tests/test_web.py` | Login-Schutz, alle Seiten, Freigabe über die Webseite, Korrekturen, Protokoll, Export |

Manuell ohne Telegram: `BOT_TOKEN` leer lassen. Dann läuft nur die Webseite.

---

## 8. Generalprobe-Checkliste

Probelauf mit **2 Test-Teams** und mindestens **2 Admins**, idealerweise ein paar Tage vorher auf der Rü.
Für den Probelauf eine eigene Datenbank nehmen, z.B. `DATA_DIR=data-probe` in `.env`.
Danach wieder auf `data` umstellen.

**Technik**

- [ ] Server startet mit `start.ps1` bzw. `start.sh` ohne Fehler im Log.
- [ ] Admin-Seite ist über das **VPN vom Handy** erreichbar, im Mobilfunknetz und ohne WLAN.
- [ ] Login funktioniert für beide Admins mit verschiedenen Namen.
- [ ] Laptop geht nicht in den Ruhezustand. Akku und Hotspot halten mindestens 4 Stunden.

**Telegram**

- [ ] Privacy Mode ist aus. Ein Text in der Gruppe taucht unter **Chats** auf.
- [ ] `/join` mit falschem Code liefert eine freundliche Fehlermeldung, mit richtigem Code den Datenschutzhinweis.
- [ ] Beide Test-Teams sind verknüpft. Im Dashboard steht kein „kein Chat“ mehr.
- [ ] Verwaltung → Spielkarte → **Vorschau erzeugen**: Bild prüfen (alle Nummern lesbar, Bahnhöfe richtig).

**Check-in und Foto**

- [ ] Standort weit weg: Der Bot nennt die nächste Bar und die Entfernung.
- [ ] Standort in einer freistehenden Bar, z.B. Die Eule: Check-in mit Status „frei“, Aufgabe und Frist.
- [ ] Standort am Rü-Stern bei Zucca oder Zizou: Buttons zur Auswahl erscheinen.
- [ ] **Live-Standort** teilen: Der Bot bestätigt kurz und checkt **nicht** ein. Danach den normalen Standort schicken, der checkt ein.
- [ ] Check-in in einer Bar ohne anderes Team: Kein „frei“-Hinweis, nur Foto-Anleitung.
      Unter Prüfen steht beim Foto die Live-Entfernung. Ein Stück weggehen, dann wird „jetzt“ orange oder rot.
- [ ] Einen Text in `texte.toml` ändern und speichern. Die nächste Bot-Nachricht nutzt den neuen Text ohne Neustart.
- [ ] Foto ohne Check-in: Hinweis „erst einchecken“.
- [ ] Foto nach Check-in: „Foto eingereicht, wird geprüft.“ Es erscheint in **Prüfen**.

**Prüfen**

- [ ] **Gültig** mit Häkchen „Aufgabe erfüllt“: Das Team bekommt Bar, Punkte und Nachricht.
- [ ] Beide Admins klicken gleichzeitig auf dasselbe Foto. Nur einer ist erfolgreich.
- [ ] **Ungültig** mit Grund: Das Team bekommt den Grund und darf ohne neuen Check-in ein neues Foto schicken.
- [ ] Team B besucht die Bar von Team A: A bekommt eine Nachricht und einen Punkt.
- [ ] Beide Teams checken in derselben freien Bar ein. Das zweite Foto ist gesperrt, bis das erste geprüft ist.
- [ ] Ein Team checkt in seiner eigenen Bar ein und wird abgelehnt.
- [ ] Dieselbe fremde Bar sofort nochmal: Hinweis auf die Abklingzeit.
- [ ] Check-in ohne Foto verfällt nach Ablauf der Frist mit Nachricht. Für den Test vorher `CHECKIN_TIMEOUT_MIN=2` setzen.
- [ ] Ein Team holt eine komplette Straße, z.B. Braun: Bonus und Info an alle anderen Teams.

**Neue Karten und Videoanrufe**

- [ ] Beide Teams checken in derselben freien Bar ein. Das zweite Team sieht den Hinweis aufs Wettrennen und den
      Button „Kurzen & gehen“. Wer zuerst das Foto hochlädt, bekommt die Bar. Das spätere Foto zahlt Miete.
- [ ] „Kurzen & gehen“ beendet den Check-in ohne Punkte.
- [ ] Button „🎬 Aktionskarte ziehen“ unter einer Check-in-Bestätigung: Der Bot zeigt die Karte und „wartet auf Anruf“. Unter Prüfen steht die Kneipe des letzten Check-ins.
      Geschafft ergibt +1, nicht geschafft −1. Abbrechen legt die Karte zurück.
- [ ] Ereigniskarten zum Testen auf kurze Zeiten setzen, z.B. 0:02, 0:04 und 0:06:
      Die 0,5-Karte kommt beim nächsten Kneipen-Check-in, die Doppelt-Karte sofort an alle.
      Danach zählt ein Foto doppelt. Anschließend die Zeiten auf 0:45, 1:30 und 2:30 zurückstellen.

**Korrekturen**

- [ ] Freigabe im **Protokoll** zurücknehmen: Punkte, Besitz und Straßenbonus stimmen danach wieder.
- [ ] Punkte von Hand buchen und Besitzer ändern, jeweils mit Kommentar.
- [ ] Ein Feld durch eine Reserve-Bar ersetzen: Alle Teams werden informiert, der Check-in klappt an der neuen Adresse.
- [ ] **Pause:** Check-in wird abgelehnt. Nach **Weiter** ist die Frist verlängert.

**Absturz und Ende**

- [ ] Prozess mitten im Spiel hart beenden und neu starten: gleicher Stand, Warteschlange noch da.
- [ ] Spieldauer auf 0:35 setzen und Spiel starten: Nach 5 Minuten kommt die 30-Minuten-Warnung. Mit Pause prüfen, dass die Uhr steht. Am Ende kommt der Endstand automatisch.
- [ ] Export CSV und JSON herunterladen und öffnen.
- [ ] **Datenschutz-Löschen** ausprobieren, nur in der Probe-Datenbank.

**Vor dem echten Abend**

- [ ] `DATA_DIR` zurück auf `data` stellen oder Verwaltung → **Spiel zurücksetzen**.
- [ ] **Testmodus aus.** Oben auf der Admin-Seite darf kein gelbes Banner mehr stehen.
- [ ] Echte Teams und Codes anlegen, Spieldauer setzen, Nordbahnhof prüfen.
- [ ] `CHECKIN_TIMEOUT_MIN` und `CHECKIN_RADIUS_M` auf die Abendwerte zurücksetzen.

---

## 9. Technik und Aufbau

```
kneipenmonopoly/
  config.py      .env lesen, alle Punktwerte und Zeiten
  gamedata.py    Spieldaten-JSON laden und prüfen
  rules.py       reine Punkte-Logik: Zustand + Ereignis → neuer Zustand, replay(), Rangliste
  texts.py       lädt texte.toml, setzt Platzhalter ein, lädt bei Änderungen neu
  texte_standard.toml  Standardtexte (Vorlage für texte.toml)
  db.py          SQLite im WAL-Modus, Schema
  service.py     Check-in, Foto, Freigabe, Korrekturen, Benachrichtigungen, Export
  bot.py         python-telegram-bot v21, Handler und Long-Polling
  web/           FastAPI + Jinja2 + HTMX, Leaflet-Karte
  __main__.py    Start, Logging, Prüfung der Konfiguration
tests/           pytest
deploy/          install.sh: Installation und Update auf Linux-Servern
```

- **Ein Prozess:** FastAPI startet beim Hochfahren den Telegram-Bot und eine Hintergrundschleife für
  Fristen, Ereigniskarten und Spielzeit. Beides läuft in derselben asyncio-Schleife.
- **Ereignisse:** `GAME_STARTED`, `GAME_PAUSED`, `GAME_RESUMED`, `GAME_ENDED`, `VISIT_APPROVED`, `EVENT_REVOKED`,
  `OWNER_SET`, `POINTS_ADJUSTED`, `FIELD_DEACTIVATED`, `FIELD_REACTIVATED`, `FIELD_REPLACED`, `MULTIPLIER_SET`,
  `ACTION_DECIDED`.
  Punkte und Besitz werden nie direkt gespeichert, sondern immer per `rules.replay()` berechnet.
- **Reihenfolge:** Besuche werden nach Einreichzeit des Fotos verrechnet, Videoanrufe nach Zeit der Anfrage, alle anderen
  Ereignisse nach Protokollzeit. Die Doppelt-Karte trägt ihre fällige Uhrzeit. Dadurch zählt die Uploadzeit.
- **Doppelte Freigabe:** Die Freigabe läuft in einer Transaktion und setzt den Status nur, wenn er noch „offen“ ist.
- **Fotos:** liegen in `data/photos/`. Die Telegram-`file_unique_id` verhindert doppeltes Herunterladen.
  Das Kartenbild wird nur einmal hochgeladen, danach nutzt der Bot die gemerkte `file_id`, bis sich die Karte ändert.
- **Zeitzone:** Gespeichert wird in UTC, angezeigt in `Europe/Berlin`.
- **Optionale Spielfeldgrenze:** Ein Schlüssel `"boundary": [[lat, lon], ...]` in der Spieldaten-JSON wird auf der
  Admin-Karte als gestricheltes Polygon gezeichnet. Die mitgelieferte JSON enthält ihn noch nicht.
