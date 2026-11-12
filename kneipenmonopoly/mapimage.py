"""Spielkarte als Bild erzeugen (für Telegram und die Vorschau in der Verwaltung).

Zeichnet OpenStreetMap-Kacheln als Hintergrund, darauf die Straßen (Farbgruppen)
als Verbindungslinien, nummerierte Felder, Bahnhöfe und Reserve-Bars, rechts
eine Legende. Die Eingaben sind reine Daten, damit es ohne Datenbank testbar ist.
Kacheln werden zwischengespeichert. Ohne Internet gibt es einen schlichten Hintergrund.
"""
from __future__ import annotations

import io
import logging
import math
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

TILE = 256
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "Kneipenmonopoly-Spielkarte/1.0 (private Spielrunde)"
MAX_MAP_PX = 1900          # längste Seite der Kartenfläche
LEGEND_W = 640


# --------------------------------------------------------------------------- Eingabedaten
@dataclass
class MapField:
    no: int | None
    name: str
    lat: float
    lon: float
    address: str = ""
    kneipe: bool = False
    active: bool = True


@dataclass
class MapGroup:
    name: str
    color: str
    fields: list[MapField]
    label: str = ""          # z.B. „Rü / Kokille“


@dataclass
class MapStation:
    letter: str
    name: str
    venue: str
    lat: float
    lon: float
    active: bool = True


@dataclass
class MapReserve:
    letter: str
    name: str
    address: str
    lat: float
    lon: float


@dataclass
class MapSpec:
    title: str
    groups: list[MapGroup]
    stations: list[MapStation] = field(default_factory=list)
    reserve: list[MapReserve] = field(default_factory=list)
    boundary: list[tuple[float, float]] = field(default_factory=list)


# --------------------------------------------------------------------------- Projektion
def _px(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = TILE * (2 ** z)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def _meters_per_px(lat: float, z: int) -> float:
    return 40075016.686 * math.cos(math.radians(lat)) / (TILE * 2 ** z)


# --------------------------------------------------------------------------- Farben/Schrift
def _rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except (ValueError, IndexError):
        return 120, 120, 120


def _text_color(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    r, g, b = rgb
    return (20, 20, 20) if (0.299 * r + 0.587 * g + 0.114 * b) > 165 else (255, 255, 255)


_FONT_CACHE: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def font(size: int, bold: bool = False):
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    names = (["DejaVuSans-Bold.ttf", "arialbd.ttf", "segoeuib.ttf", "LiberationSans-Bold.ttf"] if bold else
             ["DejaVuSans.ttf", "arial.ttf", "segoeui.ttf", "LiberationSans-Regular.ttf"])
    candidates = names + [f"/usr/share/fonts/truetype/dejavu/{n}" for n in names if n.startswith("DejaVu")]
    for n in candidates:
        try:
            f = ImageFont.truetype(n, size)
            _FONT_CACHE[key] = f
            return f
        except OSError:
            continue
    f = ImageFont.load_default(size=size)
    _FONT_CACHE[key] = f
    return f


# --------------------------------------------------------------------------- Kacheln
def _tile(z: int, x: int, y: int, cache_dir: Path | None, fetch: bool) -> Image.Image | None:
    path = cache_dir / str(z) / str(x) / f"{y}.png" if cache_dir else None
    if path and path.exists():
        try:
            return Image.open(path).convert("RGB")
        except OSError:
            pass
    if not fetch:
        return None
    try:
        req = urllib.request.Request(TILE_URL.format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        img = Image.open(io.BytesIO(data)).convert("RGB")
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return img
    except Exception as e:  # Netz weg, Server überlastet, ...
        log.warning("Kartenkachel %s/%s/%s nicht geladen: %s", z, x, y, e)
        return None


def _background(x0: float, y0: float, w: int, h: int, z: int, cache_dir: Path | None, fetch: bool) -> tuple[Image.Image, bool]:
    canvas = Image.new("RGB", (w, h), (238, 235, 228))
    tx0, ty0 = int(x0 // TILE), int(y0 // TILE)
    tx1, ty1 = int((x0 + w) // TILE), int((y0 + h) // TILE)
    got_any = False
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            t = _tile(z, tx, ty, cache_dir, fetch)
            if t is not None:
                canvas.paste(t, (int(tx * TILE - x0), int(ty * TILE - y0)))
                got_any = True
    if got_any:
        # Kacheln aufhellen, damit die Spielfelder hervorstechen
        canvas = Image.blend(canvas, Image.new("RGB", (w, h), (255, 255, 255)), 0.28)
    else:
        d = ImageDraw.Draw(canvas)
        step = 100
        for gx in range(0, w, step):
            d.line([(gx, 0), (gx, h)], fill=(225, 221, 212))
        for gy in range(0, h, step):
            d.line([(0, gy), (w, gy)], fill=(225, 221, 212))
    return canvas, got_any


# --------------------------------------------------------------------------- Zeichnen
def render_map(spec: MapSpec, out: Path, tile_cache: Path | None = None, fetch_tiles: bool = True) -> dict:
    """Zeichnet die Karte nach `out` (JPEG). Rückgabe: Infos (Größe, Zoom, Kacheln geladen)."""
    pts = [(f.lat, f.lon) for g in spec.groups for f in g.fields]
    pts += [(s.lat, s.lon) for s in spec.stations] + [(r.lat, r.lon) for r in spec.reserve] + list(spec.boundary)
    if not pts:
        raise ValueError("Keine Felder für die Karte")
    lat_min, lat_max = min(p[0] for p in pts), max(p[0] for p in pts)
    lon_min, lon_max = min(p[1] for p in pts), max(p[1] for p in pts)
    pad_px = 70

    z = 18
    while z > 12:
        xa, ya = _px(lat_max, lon_min, z)
        xb, yb = _px(lat_min, lon_max, z)
        if max(xb - xa, yb - ya) + 2 * pad_px <= MAX_MAP_PX:
            break
        z -= 1
    xa, ya = _px(lat_max, lon_min, z)
    xb, yb = _px(lat_min, lon_max, z)
    x0, y0 = xa - pad_px, ya - pad_px
    mw, mh = int(xb - xa + 2 * pad_px), int(yb - ya + 2 * pad_px)
    if mw < 900:                      # zu schmal: Ausschnitt mittig verbreitern
        x0 -= (900 - mw) / 2
        mw = 900
    legend = _legend(spec, 0)
    if legend.height > mh:            # Karte so hoch wie die Legende, statt weißer Fläche
        y0 -= (legend.height - mh) / 2
        mh = legend.height

    bg, tiles_ok = _background(x0, y0, mw, mh, z, tile_cache, fetch_tiles)
    m = bg.convert("RGBA")
    d = ImageDraw.Draw(m)

    def P(lat: float, lon: float) -> tuple[float, float]:
        x, y = _px(lat, lon, z)
        return x - x0, y - y0

    # Spielfeldgrenze
    if spec.boundary:
        b = [P(*p) for p in spec.boundary]
        _dashed(d, b + [b[0]], (210, 40, 40), 3, 14, 9)

    # Bahnstrecke zwischen den Bahnhöfen (gestrichelt, im Kreis)
    if len(spec.stations) > 1:
        sp = [P(s.lat, s.lon) for s in spec.stations]
        _dashed(d, sp + [sp[0]], (110, 110, 110), 3, 12, 10)

    # Straßen: Verbindungslinien
    for g in spec.groups:
        if len(g.fields) > 1:
            line = [P(f.lat, f.lon) for f in g.fields]
            d.line(line, fill=(255, 255, 255, 255), width=11, joint="curve")
            d.line(line, fill=_rgb(g.color) + (255,), width=6, joint="curve")

    # Reserve-Bars
    for r in spec.reserve:
        x, y = P(r.lat, r.lon)
        d.ellipse([x - 10, y - 10, x + 10, y + 10], fill=(185, 185, 185, 255), outline=(110, 110, 110, 255), width=2)
        _center(d, (x, y), r.letter, font(13, True), (60, 60, 60))

    # Felder: erst die großen Kneipen, dann die kleinen Bars obendrauf (sonst verdeckt)
    ordered = sorted(((g, f) for g in spec.groups for f in g.fields), key=lambda gf: not gf[1].kneipe)
    true_pos = [P(f.lat, f.lon) for _, f in ordered]
    radii = [21 if f.kneipe else 16 for _, f in ordered]
    shown = _spread(true_pos, radii)
    # dünne Striche von der echten Position zum verschobenen Punkt
    for (tx, ty), (sx, sy) in zip(true_pos, shown):
        if math.hypot(sx - tx, sy - ty) > 4:
            d.line([(tx, ty), (sx, sy)], fill=(40, 40, 40, 255), width=2)
            d.ellipse([tx - 3, ty - 3, tx + 3, ty + 3], fill=(40, 40, 40, 255))
    for (g, f), (x, y), rad in zip(ordered, shown, radii):
        rgb = _rgb(g.color)
        fill = rgb if f.active else (200, 200, 200)
        d.ellipse([x - rad - 3, y - rad - 3, x + rad + 3, y + rad + 3], fill=(255, 255, 255, 255))
        d.ellipse([x - rad, y - rad, x + rad, y + rad], fill=fill + (255,),
                  outline=(25, 25, 25, 255) if f.kneipe else (255, 255, 255, 255), width=4 if f.kneipe else 2)
        if f.no is not None:
            _center(d, (x, y), str(f.no), font(17 if f.kneipe else 15, True), _text_color(fill))
        if not f.active:
            d.line([x - rad, y - rad, x + rad, y + rad], fill=(200, 30, 30, 255), width=4)

    # Bahnhöfe
    for s in spec.stations:
        x, y = P(s.lat, s.lon)
        d.rounded_rectangle([x - 17, y - 17, x + 17, y + 17], radius=5,
                            fill=(25, 25, 25, 255) if s.active else (150, 150, 150, 255), outline=(255, 255, 255, 255), width=3)
        _center(d, (x, y), s.letter, font(19, True), (255, 255, 255))

    # Nordpfeil, Maßstab, Quellenangabe
    _north_arrow(d, mw - 50, 60)
    _scale_bar(d, 34, 44, (lat_min + lat_max) / 2, z)
    attr = "Kartendaten © OpenStreetMap-Mitwirkende" if tiles_ok else "Ohne Kartenhintergrund (keine Kacheln geladen)"
    fa = font(14)
    tw = d.textlength(attr, font=fa)
    d.rectangle([mw - tw - 16, mh - 26, mw, mh], fill=(255, 255, 255, 220))
    d.text((mw - tw - 8, mh - 22), attr, fill=(60, 60, 60), font=fa)

    legend = _legend(spec, mh)
    W, H = mw + legend.width, mh
    out_img = Image.new("RGB", (W, H), (255, 255, 255))
    out_img.paste(m.convert("RGB"), (0, 0))
    out_img.paste(legend, (mw, 0))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.jpg")
    out_img.save(tmp, "JPEG", quality=88, optimize=True)
    tmp.replace(out)
    return {"width": W, "height": H, "zoom": z, "tiles": tiles_ok}


def _spread(points: list[tuple[float, float]], radii: list[int], gap: float = 4, rounds: int = 60):
    """Überlappende Kreise auseinanderschieben, damit alle Nummern lesbar sind."""
    pos = [list(p) for p in points]
    for _ in range(rounds):
        moved = False
        for i in range(len(pos)):
            for j in range(i + 1, len(pos)):
                dx, dy = pos[j][0] - pos[i][0], pos[j][1] - pos[i][1]
                dist = math.hypot(dx, dy)
                need = radii[i] + radii[j] + gap
                if dist >= need:
                    continue
                if dist < 0.01:                       # exakt gleiche Stelle: in feste Richtung schieben
                    dx, dy, dist = 1.0, 0.3, math.hypot(1.0, 0.3)
                push = (need - dist) / 2 + 0.5
                ux, uy = dx / dist, dy / dist
                pos[i][0] -= ux * push; pos[i][1] -= uy * push
                pos[j][0] += ux * push; pos[j][1] += uy * push
                moved = True
        if not moved:
            break
    return [tuple(p) for p in pos]


def _center(d: ImageDraw.ImageDraw, xy, text: str, f, fill) -> None:
    stroke = (0, 0, 0) if fill == (255, 255, 255) else (255, 255, 255)
    d.text(xy, text, fill=fill, font=f, anchor="mm", stroke_width=1, stroke_fill=stroke)


def _dashed(d: ImageDraw.ImageDraw, pts, color, width: int, dash: int, gap: int) -> None:
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        length = math.hypot(x2 - x1, y2 - y1)
        if length == 0:
            continue
        dx, dy = (x2 - x1) / length, (y2 - y1) / length
        pos = 0.0
        while pos < length:
            end = min(pos + dash, length)
            d.line([(x1 + dx * pos, y1 + dy * pos), (x1 + dx * end, y1 + dy * end)], fill=color, width=width)
            pos += dash + gap


def _north_arrow(d: ImageDraw.ImageDraw, x: float, y: float) -> None:
    d.polygon([(x, y - 28), (x - 13, y + 12), (x, y + 4), (x + 13, y + 12)], fill=(30, 30, 30))
    d.text((x, y + 28), "N", fill=(30, 30, 30), font=font(20, True), anchor="mm")


def _scale_bar(d: ImageDraw.ImageDraw, x: float, y: float, lat: float, z: int) -> None:
    mpp = _meters_per_px(lat, z)
    meters = 200 if 200 / mpp < 400 else 100
    px = meters / mpp
    d.rectangle([x - 8, y - 22, x + px + 70, y + 16], fill=(255, 255, 255, 220))
    d.line([(x, y), (x + px, y)], fill=(30, 30, 30), width=4)
    d.line([(x, y - 8), (x, y + 8)], fill=(30, 30, 30), width=3)
    d.line([(x + px, y - 8), (x + px, y + 8)], fill=(30, 30, 30), width=3)
    d.text((x + px + 10, y), f"{meters} m", fill=(30, 30, 30), font=font(16), anchor="lm")


def _legend(spec: MapSpec, min_h: int) -> Image.Image:
    W = LEGEND_W
    rows: list[tuple] = []          # (Art, Daten)
    n_fields = sum(len(g.fields) for g in spec.groups)
    n_kneipen = sum(1 for g in spec.groups for f in g.fields if f.kneipe)
    rows.append(("title", spec.title))
    rows.append(("sub", f"Spielplan · {len(spec.groups)} Straßen mit {n_fields} Feldern ({n_kneipen} Kneipen) · "
                        f"{len(spec.stations)} Bahnhöfe"))
    rows.append(("h", "Straßen"))
    for g in spec.groups:
        rows.append(("group", g))
        for f in g.fields:
            rows.append(("field", (g, f)))
    if spec.stations:
        rows.append(("h", "Bahnhöfe"))
        rows += [("station", s) for s in spec.stations]

    heights = {"title": 52, "sub": 46, "h": 52, "group": 42, "field": 30, "station": 30, "reserve": 26, "key": 130}
    H = max(min_h, sum(heights[k] for k, _ in rows) + 40)
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.line([(0, 0), (0, H)], fill=(215, 210, 200), width=2)
    x, y, right = 28, 24, W - 24
    for kind, data in rows:
        if kind == "title":
            d.text((x, y), data, fill=(25, 25, 25), font=font(30, True))
        elif kind == "sub":
            d.text((x, y + 4), _fit(d, data, font(16), right - x), fill=(90, 90, 90), font=font(16))
        elif kind == "h":
            d.text((x, y + 18), data, fill=(25, 25, 25), font=font(22, True))
        elif kind == "group":
            g = data
            rgb = _rgb(g.color)
            d.rectangle([x, y + 6, right, y + 36], fill=rgb)
            d.text((x + 10, y + 21), g.name.upper(), fill=_text_color(rgb), font=font(16, True), anchor="lm")
            if g.label:
                d.text((right - 10, y + 21), g.label, fill=_text_color(rgb), font=font(14), anchor="rm")
        elif kind == "field":
            g, f = data
            rgb = _rgb(g.color) if f.active else (200, 200, 200)
            cy = y + 15
            r = 11 if f.kneipe else 9
            d.ellipse([x + 12 - r, cy - r, x + 12 + r, cy + r], fill=rgb, outline=(25, 25, 25) if f.kneipe else rgb, width=2)
            if f.no is not None:
                d.text((x + 12, cy), str(f.no), fill=_text_color(rgb), font=font(11, True), anchor="mm")
            name = f.name + ("" if f.active else " (geschlossen)")
            d.text((x + 34, cy), name, fill=(25, 25, 25), font=font(16, f.kneipe), anchor="lm")
            info = short_address(f.address) + (" · Kneipe" if f.kneipe else "")
            d.text((right, cy), _fit(d, info, font(13), 240), fill=(110, 110, 110), font=font(13), anchor="rm")
        elif kind == "station":
            s = data
            cy = y + 15
            d.rounded_rectangle([x + 1, cy - 11, x + 23, cy + 11], radius=3, fill=(25, 25, 25))
            d.text((x + 12, cy), s.letter, fill=(255, 255, 255), font=font(13, True), anchor="mm")
            d.text((x + 34, cy), s.name, fill=(25, 25, 25), font=font(16, True), anchor="lm")
            d.text((right, cy), _fit(d, s.venue, font(13), 300), fill=(110, 110, 110), font=font(13), anchor="rm")
        elif kind == "reserve":
            r = data
            cy = y + 13
            d.ellipse([x + 4, cy - 8, x + 20, cy + 8], fill=(185, 185, 185))
            d.text((x + 12, cy), r.letter, fill=(60, 60, 60), font=font(11, True), anchor="mm")
            d.text((x + 34, cy), r.name, fill=(70, 70, 70), font=font(14), anchor="lm")
            d.text((right, cy), r.address, fill=(130, 130, 130), font=font(13), anchor="rm")
        elif kind == "key":
            ky = y + 10
            d.ellipse([x + 1, ky, x + 23, ky + 22], fill=(226, 35, 26), outline=(25, 25, 25), width=3)
            d.text((x + 34, ky + 11), "Kneipe (großer Punkt, schwarzer Rand)", fill=(40, 40, 40), font=font(14), anchor="lm")
            ky += 30
            d.ellipse([x + 4, ky + 3, x + 20, ky + 19], fill=(226, 35, 26))
            d.text((x + 34, ky + 11), "Bar / Gastro mit Theke", fill=(40, 40, 40), font=font(14), anchor="lm")
            ky += 30
            d.line([(x, ky + 11), (x + 24, ky + 11)], fill=(226, 35, 26), width=6)
            d.text((x + 34, ky + 11), "Verbindungslinie einer Straße", fill=(40, 40, 40), font=font(14), anchor="lm")
            ky += 30
            _dashed(d, [(x, ky + 11), (x + 24, ky + 11)], (110, 110, 110), 3, 6, 4)
            d.text((x + 34, ky + 11), "Bahnstrecke zwischen den Bahnhöfen", fill=(40, 40, 40), font=font(14), anchor="lm")
        y += heights[kind]
    return img


def short_address(address: str) -> str:
    """„Rüttenscheider Str. 201, 45130/45131 Essen“ -> „Rüttenscheider Str. 201“."""
    return re.sub(r",\s*\d{5}.*$", "", address or "").strip()


def _fit(d: ImageDraw.ImageDraw, text: str, f, max_w: float) -> str:
    if d.textlength(text, font=f) <= max_w:
        return text
    while text and d.textlength(text + "…", font=f) > max_w:
        text = text[:-1]
    return text + "…"
