# Beispiel-Spieldaten für andere Städte

Fertige Spielfelder für Kneipenmonopoly, jeweils in einem Kasten von höchstens **1,5 × 1,5 km**:
22 Felder in 8 Straßen, 4 Kioske als Bahnhöfe (Nord/Ost/Süd/West) und 6–8 Reserve-Bars.

| Datei | Stadt | Viertel |
|---|---|---|
| `spieldaten_koeln-zuelpicher.json` | Köln | Kwartier Latäng (Zülpicher Platz, Rudolfplatz, Belgisches Viertel) |
| `spieldaten_duesseldorf-altstadt.json` | Düsseldorf | Altstadt |
| `spieldaten_muenster-altstadt.json` | Münster | Kuhviertel & Altstadt |
| `spieldaten_bochum-bermudadreieck.json` | Bochum | Bermudadreieck |
| `spieldaten_hamburg-st-pauli.json` | Hamburg | St. Pauli (Kiez, Reeperbahn) |
| `spieldaten_berlin-friedrichshain.json` | Berlin | Friedrichshain (Simon-Dach-Kiez) |

## Benutzen

- **Neue Session:** Auf der Startseite unter „Neue Session anlegen“ die Datei bei *Setup übernehmen* hochladen.
- **Bestehende Session:** Verwaltung → Export / Import → Datei hochladen (nur in der Vorbereitung).
- Danach im **Karten-Editor** nachbessern und unter Verwaltung → Spielkarte die **Vorschau** prüfen.

## Bitte vor dem Spiel prüfen

Die Daten stammen automatisch aus OpenStreetMap (© OpenStreetMap-Mitwirkende, ODbL) und sind **nicht vor Ort
geprüft**:

- Gibt es die Kneipe noch, und hat sie am Spieltag offen? Geschlossene durch eine Reserve ersetzen.
- Passt der Punkt auf der Karte? Bei Bedarf im Karten-Editor verschieben.
- Haben die Kioske (Bahnhöfe) offen? Alle sind als „nicht bestätigt“ markiert.
- Liegen die Straßen sinnvoll? Sie sind automatisch im Uhrzeigersinn um die Mitte gebildet.
- „Kneipe“ oder „Bar“ kommt aus OSM (`pub` oder `bar`) und hat keinen Einfluss auf die Punkte.
- Fehlende Adressen lassen sich im Karten-Editor nachtragen.

## Neu erzeugen oder weitere Städte

```powershell
.venv\Scripts\python beispiele\erzeugen.py beispiele                    # alle
.venv\Scripts\python beispiele\erzeugen.py beispiele hamburg-st-pauli   # nur eine
```

Neue Städte trägst du oben in `CITIES` ein: Name, Viertel und Mittelpunkt (Breite, Länge). Das Skript fragt
die Overpass-API von OpenStreetMap ab und braucht dafür Internet. Kneipen wählt es gut verteilt aus, mit
mindestens 45 m Abstand zwischen zwei Feldern. Shisha-Bars, Spielhallen, Hotels und Ähnliches lässt es weg.
