"""Spiel-Recap als PDF: Punkteverlauf als Diagramm plus jede Buchung mit Zeitstempel und Saldo.

Rein zeichnend (Pillow), keine Spiellogik. Die Daten liefert `Game.recap_data()`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from PIL import Image, ImageDraw

from .mapimage import font

DPI = 150
A4_LANDSCAPE = (1754, 1240)
A4_PORTRAIT = (1240, 1754)
MARGIN = 70
INK = (30, 30, 30)
MUTED = (110, 110, 110)
GRID = (225, 225, 225)
ZEBRA = (245, 245, 245)
GOOD = (30, 130, 60)
BAD = (190, 40, 40)


@dataclass
class RecapTeam:
    id: int
    name: str
    color: str
    points: int = 0
    place: int = 0


@dataclass
class RecapRow:
    when: datetime                    # lokale Zeit der Buchung (Fotoupload bzw. Anfrage)
    game_min: float | None            # Spielzeit in Minuten (ohne Pausen), None ohne Start
    team: int
    delta: int
    text: str
    balances: dict[int, int] = field(default_factory=dict)   # Stand ALLER Teams nach der Buchung


@dataclass
class RecapData:
    title: str
    subtitle: str
    teams: list[RecapTeam]
    rows: list[RecapRow]
    start: datetime | None
    end: datetime | None
    created: datetime
    pauses: list[tuple[datetime, datetime]] = field(default_factory=list)   # lokale Zeiten


def _rgb(color: str) -> tuple[int, int, int]:
    c = (color or "#888888").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return (136, 136, 136)


def _line_color(color: str) -> tuple[int, int, int]:
    """Sehr helle Teamfarben etwas abdunkeln, damit sie auf Weiß sichtbar bleiben."""
    r, g, b = _rgb(color)
    if 0.299 * r + 0.587 * g + 0.114 * b > 200:
        return int(r * 0.7), int(g * 0.7), int(b * 0.7)
    return r, g, b


def _fit(draw: ImageDraw.ImageDraw, text: str, f, width: int) -> str:
    if draw.textlength(text, font=f) <= width:
        return text
    while text and draw.textlength(text + "…", font=f) > width:
        text = text[:-1]
    return text + "…"


def _wrap(draw: ImageDraw.ImageDraw, text: str, f, width: int, max_lines: int) -> list[str]:
    """Text auf höchstens `max_lines` Zeilen umbrechen, Rest mit … kürzen."""
    words, lines, cur = re.findall(r"[^\s-]+-?|-", text), [], ""   # Umbruch auch nach Bindestrich
    for w in words:
        cand = (cur + ("" if cur.endswith("-") or not cur else " ") + w)
        if draw.textlength(cand, font=f) <= width or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return [_fit(draw, ln, f, width) for ln in lines]


def fmt_game(minutes: float | None) -> str:
    if minutes is None:
        return "–"
    m = max(0, int(minutes))
    return f"{m // 60}:{m % 60:02d}"


def _footer(draw: ImageDraw.ImageDraw, size: tuple[int, int], data: RecapData, page: int, pages: int) -> None:
    f = font(18)
    y = size[1] - MARGIN + 25
    draw.text((MARGIN, y), f"{data.title} · erstellt {data.created:%d.%m.%Y %H:%M}", font=f, fill=MUTED)
    label = f"Seite {page} von {pages}"
    draw.text((size[0] - MARGIN - draw.textlength(label, font=f), y), label, font=f, fill=MUTED)


# --------------------------------------------------------------------------- Seite 1: Diagramm
def _overview_page(data: RecapData) -> Image.Image:
    img = Image.new("RGB", A4_LANDSCAPE, "white")
    d = ImageDraw.Draw(img)
    W, H = A4_LANDSCAPE
    d.text((MARGIN, MARGIN - 20), data.title, font=font(46, True), fill=INK)
    d.text((MARGIN, MARGIN + 38), data.subtitle, font=font(22), fill=MUTED)

    # Endstand rechts
    side_w = 420
    sx = W - MARGIN - side_w
    y = MARGIN + 110
    d.text((sx, y), "Endstand", font=font(28, True), fill=INK)
    y += 48
    for t in sorted(data.teams, key=lambda t: (t.place, t.name)):
        medal = {1: "1.", 2: "2.", 3: "3."}.get(t.place, f"{t.place}.")
        d.rounded_rectangle((sx, y + 4, sx + 22, y + 26), 5, fill=_rgb(t.color), outline=(90, 90, 90))
        d.text((sx + 34, y), medal, font=font(24, True), fill=INK)
        d.text((sx + 80, y), _fit(d, t.name, font(24), side_w - 200), font=font(24), fill=INK)
        pts = f"{t.points} P"
        d.text((sx + side_w - d.textlength(pts, font=font(24, True)), y), pts, font=font(24, True), fill=INK)
        y += 40
    y += 20
    n_book = len(data.rows)
    for line in [f"{n_book} Buchungen",
                 f"Start {data.start:%H:%M} Uhr" if data.start else "",
                 f"Ende {data.end:%H:%M} Uhr" if data.end else ""]:
        if line:
            d.text((sx, y), line, font=font(20), fill=MUTED)
            y += 30

    # Diagramm links
    x0, y0 = MARGIN + 70, MARGIN + 130
    x1, y1 = sx - 60, H - MARGIN - 90
    d.text((MARGIN, y0 - 50), "Punkteverlauf", font=font(28, True), fill=INK)
    times = [r.when for r in data.rows]
    t_start = data.start or (min(times) if times else data.created)
    t_end = max([data.end or data.created] + times) if (data.end or times) else data.created
    if data.end:
        t_end = max([data.end] + times)
    if t_end <= t_start:
        t_end = t_start + timedelta(minutes=10)
    span = (t_end - t_start).total_seconds()
    all_pts = [0] + [v for r in data.rows for v in r.balances.values()]
    lo, hi = min(all_pts), max(all_pts)
    if hi == lo:
        hi = lo + 1
    step = max(1, _nice_step((hi - lo) / 6))
    lo = (lo // step) * step
    hi = -(-hi // step) * step

    def X(t: datetime) -> float:
        return x0 + (t - t_start).total_seconds() / span * (x1 - x0)

    def Y(v: float) -> float:
        return y1 - (v - lo) / (hi - lo) * (y1 - y0)

    fs = font(18)
    for a, b in data.pauses:
        a, b = max(a, t_start), min(b, t_end)
        if b > a:
            d.rectangle((X(a), y0, X(b), y1), fill=(238, 238, 238))
            lbl = "Pause"
            if X(b) - X(a) > d.textlength(lbl, font=fs) + 6:
                d.text(((X(a) + X(b) - d.textlength(lbl, font=fs)) / 2, y0 + 6), lbl, font=fs, fill=MUTED)
    v = lo
    while v <= hi:
        d.line((x0, Y(v), x1, Y(v)), fill=GRID, width=1)
        lbl = str(v)
        d.text((x0 - 12 - d.textlength(lbl, font=fs), Y(v) - 11), lbl, font=fs, fill=MUTED)
        v += step
    tick = _time_step(span)
    t = t_start.replace(second=0, microsecond=0)
    t = t + timedelta(minutes=(-t.minute) % tick)
    while t <= t_end:
        x = X(t)
        d.line((x, y0, x, y1), fill=GRID, width=1)
        lbl = f"{t:%H:%M}"
        d.text((x - d.textlength(lbl, font=fs) / 2, y1 + 10), lbl, font=fs, fill=MUTED)
        t += timedelta(minutes=tick)
    d.line((x0, y1, x1, y1), fill=MUTED, width=2)
    d.line((x0, y0, x0, y1), fill=MUTED, width=2)
    d.text((x0, y1 + 40), "Uhrzeit (Foto-Upload bzw. Anfrage)", font=fs, fill=MUTED)

    for team in data.teams:
        col = _line_color(team.color)
        pts = [(X(t_start), Y(0))]
        val = 0
        for r in data.rows:
            nv = r.balances.get(team.id, val)
            if nv != val:
                x = X(r.when)
                pts.append((x, Y(val)))
                pts.append((x, Y(nv)))
                val = nv
        pts.append((X(t_end), Y(val)))
        d.line(pts, fill=col, width=5, joint="curve")
        ex, ey = pts[-1]
        d.ellipse((ex - 7, ey - 7, ex + 7, ey + 7), fill=col)
    return img


def _nice_step(raw: float) -> int:
    for s in (1, 2, 5, 10, 20, 25, 50, 100):
        if s >= raw:
            return s
    return int(raw) + 1


def _time_step(seconds: float) -> int:
    minutes = seconds / 60
    for s in (5, 10, 15, 30, 60, 120):
        if minutes / s <= 12:
            return s
    return 180


# --------------------------------------------------------------------------- Tabellenseiten
def _table_pages(data: RecapData) -> list[Image.Image]:
    W, H = A4_PORTRAIT
    teams = data.teams
    per_team = len(teams) <= 6
    f, fb, fh = font(19), font(19, True), font(17, True)
    row_h = 34
    head_h = 58
    cols = [("Uhrzeit", 95), ("Spielzeit", 95), ("Team", 175), ("Buchung", 0), ("+/–", 60)]
    if per_team:
        saldo_w = 92
        cols += [(t.name, saldo_w) for t in teams]
    else:
        cols += [("Saldo", 90)]
    fixed = sum(w for _, w in cols)
    cols[3] = ("Buchung", W - 2 * MARGIN - fixed)
    xs = []
    x = MARGIN
    for _, w in cols:
        xs.append(x)
        x += w

    pages: list[Image.Image] = []
    top = MARGIN + 150
    rows_per_page = (H - MARGIN - 60 - top) // row_h
    chunks = [data.rows[i:i + rows_per_page] for i in range(0, len(data.rows), rows_per_page)] or [[]]
    for n, chunk in enumerate(chunks):
        img = Image.new("RGB", A4_PORTRAIT, "white")
        d = ImageDraw.Draw(img)
        d.text((MARGIN, MARGIN - 20), "Alle Buchungen", font=font(36, True), fill=INK)
        sub = "Zeitstempel = Foto-Upload bzw. Anfrage der Aktion. " + \
              ("Rechts: Punktestand jedes Teams nach der Buchung." if per_team else "Saldo = Stand des Teams danach.")
        d.text((MARGIN, MARGIN + 28), sub, font=font(18), fill=MUTED)
        hy = top - head_h - 6
        fn = font(15, True)
        for k, ((name, w), cx) in enumerate(zip(cols, xs)):
            if per_team and k >= 5:
                t = teams[k - 5]
                d.rectangle((cx + 2, hy, cx + w - 2, hy + head_h - 4), fill=_rgb(t.color))
                r, g, b = _rgb(t.color)
                txt = (20, 20, 20) if 0.299 * r + 0.587 * g + 0.114 * b > 150 else (255, 255, 255)
                for li, line in enumerate(_wrap(d, name, fn, w - 12, 2)):
                    d.text((cx + 6, hy + 6 + li * 22), line, font=fn, fill=txt)
            else:
                d.text((cx + 4, hy + head_h - 30), _fit(d, name, fh, w - 10), font=fh, fill=INK)
        d.line((MARGIN, top - 4, W - MARGIN, top - 4), fill=MUTED, width=2)
        names = {t.id: t for t in teams}
        if not chunk:
            d.text((MARGIN, top + 10), "Keine Punktebuchungen.", font=f, fill=MUTED)
        for i, r in enumerate(chunk):
            y = top + i * row_h
            if i % 2:
                d.rectangle((MARGIN, y - 2, W - MARGIN, y + row_h - 4), fill=ZEBRA)
            team = names.get(r.team)
            cells = [f"{r.when:%H:%M:%S}", fmt_game(r.game_min), team.name if team else "?", r.text,
                     f"{r.delta:+d}"]
            for j, (val, cx) in enumerate(zip(cells, xs)):
                w = cols[j][1]
                if j == 2 and team:
                    d.rounded_rectangle((cx + 2, y + 6, cx + 16, y + 20), 3, fill=_rgb(team.color))
                    d.text((cx + 22, y + 2), _fit(d, val, f, w - 28), font=f, fill=INK)
                elif j == 4:
                    d.text((cx + w - 10 - d.textlength(val, font=fb), y + 2), val, font=fb,
                           fill=GOOD if r.delta > 0 else BAD)
                else:
                    d.text((cx + 4, y + 2), _fit(d, val, f, w - 10), font=f, fill=INK)
            if per_team:
                for k, t in enumerate(teams):
                    cx, w = xs[5 + k], cols[5 + k][1]
                    val = str(r.balances.get(t.id, 0))
                    ff = fb if t.id == r.team else f
                    d.text((cx + w - 12 - d.textlength(val, font=ff), y + 2), val, font=ff,
                           fill=INK if t.id == r.team else MUTED)
            else:
                cx, w = xs[5], cols[5][1]
                val = str(r.balances.get(r.team, 0))
                d.text((cx + w - 12 - d.textlength(val, font=fb), y + 2), val, font=fb, fill=INK)
        pages.append(img)
    return pages


def render_recap_pdf(data: RecapData, out) -> int:
    """Schreibt das PDF nach `out` (Pfad oder Datei-Objekt). Gibt die Seitenzahl zurück."""
    pages = [_overview_page(data)] + _table_pages(data)
    for i, p in enumerate(pages, start=1):
        _footer(ImageDraw.Draw(p), p.size, data, i, len(pages))
    pages[0].save(out, "PDF", resolution=DPI, save_all=True, append_images=pages[1:],
                  title=data.title, author="Kneipenmonopoly")
    return len(pages)
