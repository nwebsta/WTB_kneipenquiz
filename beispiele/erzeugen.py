"""Beispiel-Spieldaten aus OpenStreetMap erzeugen: 22 Felder in 8 Straßen, 4 Bahnhöfe (Kioske), Reserve.

Aufruf: .venv/Scripts/python beispiele/erzeugen.py beispiele [stadt-slug ...]
"""
import json
import math
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

OVERPASS = "https://overpass-api.de/api/interpreter"
UA = "kneipenmonopoly-beispieldaten/1.0"
HALF_KM = 0.75          # Kasten 1,5 x 1,5 km
SIZES = [2, 3, 3, 3, 3, 3, 3, 2]
GROUPS = [("braun", "Braun", "#8b5a2b"), ("hellblau", "Hellblau", "#7cc8ec"), ("pink", "Pink", "#d6338f"),
          ("orange", "Orange", "#f28c1c"), ("rot", "Rot", "#e2231a"), ("gelb", "Gelb", "#f5d90a"),
          ("gruen", "Grün", "#1a9e52"), ("dunkelblau", "Dunkelblau", "#0b5fa8")]
MIN_DIST = 45           # Meter zwischen zwei Feldern (Check-in-Radius 50 m)
SKIP = re.compile(r"shisha|hookah|casino|spielhalle|wettb|tipico|hotel|lounge bar des|sportsbar|strip|table ?dance"
                  r"|nachtclub|bordell|erotik", re.I)

CITIES = {
    "koeln-zuelpicher": ("Köln", "Kwartier Latäng", 50.9318, 6.9405),
    "duesseldorf-altstadt": ("Düsseldorf", "Altstadt", 51.2262, 6.7745),
    "muenster-altstadt": ("Münster", "Kuhviertel & Altstadt", 51.9628, 7.6250),
    "bochum-bermudadreieck": ("Bochum", "Bermudadreieck", 51.4790, 7.2185),
    "hamburg-st-pauli": ("Hamburg", "St. Pauli", 53.5525, 9.9615),
    "leipzig-suedvorstadt": ("Leipzig", "Südvorstadt (Karli)", 51.3240, 12.3745),
    "berlin-friedrichshain": ("Berlin", "Friedrichshain", 52.5095, 13.4570),
}


def dist(a, b):
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def slug(text):
    t = text.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-") or "feld"


def box(lat, lon):
    dlat = HALF_KM / 111.32
    dlon = HALF_KM / (111.32 * math.cos(math.radians(lat)))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def overpass(bbox):
    s, w, n, e = bbox
    q = f"""[out:json][timeout:90];
(
  nwr["amenity"~"^(pub|bar|biergarten)$"]["name"]({s},{w},{n},{e});
  nwr["shop"="kiosk"]({s},{w},{n},{e});
  nwr["amenity"="fuel"]({s},{w},{n},{e});
);
out center tags;"""
    req = urllib.request.Request(OVERPASS, data=urllib.parse.urlencode({"data": q}).encode(),
                                 headers={"User-Agent": UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)["elements"]
        except Exception as ex:  # Überlastung: kurz warten
            print("  Overpass-Fehler, neuer Versuch:", ex)
            time.sleep(10 * (attempt + 1))
    raise SystemExit("Overpass nicht erreichbar")


def point(el):
    if "lat" in el:
        return el["lat"], el["lon"]
    c = el.get("center")
    return (c["lat"], c["lon"]) if c else None


def address(tags):
    street, no = tags.get("addr:street"), tags.get("addr:housenumber")
    if street:
        street = street.replace("straße", "str.").replace("Straße", "Str.")
        return f"{street} {no}".strip() if no else street
    return ""


def main():
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = sys.argv[2:] or list(CITIES)
    for key in wanted:
        city, quarter, clat, clon = CITIES[key]
        bb = box(clat, clon)
        print(f"{city} – {quarter}")
        els = overpass(bb)
        bars, kiosks = [], []
        seen = set()
        for el in els:
            p = point(el)
            tags = el.get("tags", {})
            if p is None or not (bb[0] <= p[0] <= bb[2] and bb[1] <= p[1] <= bb[3]):
                continue
            if any(k.startswith(("disused", "was:", "abandoned")) for k in tags):
                continue
            name = (tags.get("name") or "").strip()
            am, shop = tags.get("amenity"), tags.get("shop")
            if am in ("pub", "bar", "biergarten"):
                if not name or SKIP.search(name) or name.lower() in seen:
                    continue
                seen.add(name.lower())
                bars.append({"name": name, "type": "kneipe" if am in ("pub", "biergarten") else "bar",
                             "address": address(tags), "lat": round(p[0], 6), "lon": round(p[1], 6)})
            elif shop == "kiosk" or am == "fuel":
                label = name or ("Kiosk" if shop == "kiosk" else "Tankstelle")
                if am == "fuel" and tags.get("brand"):
                    label = f"{tags['brand']}-Tankstelle"
                kiosks.append({"venue": f"{label}, {address(tags)}".rstrip(", "), "lat": round(p[0], 6),
                               "lon": round(p[1], 6), "kiosk": shop == "kiosk"})
        print(f"  {len(bars)} Kneipen/Bars, {len(kiosks)} Kioske/Tankstellen")
        if len(bars) < 26 or len(kiosks) < 4:
            print("  zu wenig Daten, übersprungen")
            continue
        center = (clat, clon)

        # Felder: gut verteilt (Farthest-Point-Sampling), Kneipen leicht bevorzugt, Mindestabstand
        pool = sorted(bars, key=lambda b: (b["type"] != "kneipe", dist(center, (b["lat"], b["lon"]))))
        chosen = [pool[0]]
        while len(chosen) < 22:
            best, best_d = None, -1
            for b in pool:
                if b in chosen:
                    continue
                d = min(dist((b["lat"], b["lon"]), (c["lat"], c["lon"])) for c in chosen)
                if d < MIN_DIST:
                    continue
                score = d * (1.15 if b["type"] == "kneipe" else 1.0)
                if score > best_d:
                    best, best_d = b, score
            if best is None:
                break
            chosen.append(best)
        if len(chosen) < 22:
            print("  nicht genug Felder mit Mindestabstand, übersprungen")
            continue

        # Reihenfolge wie ein Monopoly-Brett: im Uhrzeigersinn um die Mitte, Start im Südwesten
        def angle(b):
            a = math.atan2(b["lat"] - clat, (b["lon"] - clon) * math.cos(math.radians(clat)))
            return (-(a - math.radians(-135))) % (2 * math.pi)
        chosen.sort(key=angle)

        groups, fields, used_ids = [], [], set()
        i = 0
        for (gid, gname, color), size in zip(GROUPS, SIZES):
            ids = []
            for b in chosen[i:i + size]:
                fid = slug(b["name"])
                while fid in used_ids:
                    fid += "-2"
                used_ids.add(fid)
                ids.append(fid)
                fields.append({"id": fid, "no": len(fields) + 1, "name": b["name"], "type": b["type"],
                               "group": gid, "address": b["address"], "lat": b["lat"], "lon": b["lon"]})
            groups.append({"id": gid, "name": gname, "color": color, "field_ids": ids})
            i += size

        # Bahnhöfe: je Himmelsrichtung der äußerste Kiosk (Kioske vor Tankstellen)
        stations = []
        taken = []
        for sid, name, vec in (("bahnhof-nord", "Nordbahnhof", (1, 0)), ("bahnhof-ost", "Ostbahnhof", (0, 1)),
                               ("bahnhof-sued", "Südbahnhof", (-1, 0)), ("bahnhof-west", "Westbahnhof", (0, -1))):
            def proj(k):
                dy = (k["lat"] - clat) * 111320
                dx = (k["lon"] - clon) * 111320 * math.cos(math.radians(clat))
                along = dy * vec[0] + dx * vec[1]
                side = abs(dy * vec[1] - dx * vec[0])
                return along - 0.5 * side + (80 if k["kiosk"] else 0)
            cands = [k for k in kiosks if k not in taken
                     and all(dist((k["lat"], k["lon"]), (f["lat"], f["lon"])) >= MIN_DIST for f in fields)
                     and all(dist((k["lat"], k["lon"]), (t["lat"], t["lon"])) >= 300 for t in taken)]
            if not cands:
                cands = [k for k in kiosks if k not in taken]
            k = max(cands, key=proj)
            taken.append(k)
            stations.append({"id": sid, "name": name, "venue": k["venue"], "lat": k["lat"], "lon": k["lon"],
                             "verified_open": False})

        # Reserve: die nächsten 8 Bars, die nicht zu nah an Feldern liegen
        rest = [b for b in bars if b not in chosen]
        rest.sort(key=lambda b: dist(center, (b["lat"], b["lon"])))
        reserve = []
        for b in rest:
            pts = [(f["lat"], f["lon"]) for f in fields] + [(r["lat"], r["lon"]) for r in reserve]
            if all(dist((b["lat"], b["lon"]), q) >= MIN_DIST for q in pts):
                rid = slug(b["name"])
                while rid in used_ids:
                    rid += "-2"
                used_ids.add(rid)
                reserve.append({"id": rid, "name": b["name"], "address": b["address"], "lat": b["lat"],
                                "lon": b["lon"]})
            if len(reserve) == 8:
                break

        s, w, n, e = bb
        data = {
            "game": f"Kneipenmonopoly {quarter}",
            "city": city,
            "coordinates": "WGS84, aus OpenStreetMap (© OpenStreetMap-Mitwirkende, ODbL), automatisch ausgewählt, "
                           f"Stand {time.strftime('%d.%m.%Y')}. Nicht vor Ort geprüft!",
            "boundary": [[round(s, 6), round(w, 6)], [round(s, 6), round(e, 6)], [round(n, 6), round(e, 6)],
                         [round(n, 6), round(w, 6)]],
            "groups": groups, "fields": fields, "stations": stations, "reserve": reserve,
        }
        path = out_dir / f"spieldaten_{key}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  -> {path.name}: {len(fields)} Felder, {len(stations)} Bahnhöfe, {len(reserve)} Reserve")
        time.sleep(5)


if __name__ == "__main__":
    main()
