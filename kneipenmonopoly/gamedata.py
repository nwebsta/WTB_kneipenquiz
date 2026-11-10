"""Spieldaten aus der JSON-Datei laden und prüfen. Nichts hart codiert."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path


class GameDataError(ValueError):
    pass


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    color: str
    field_ids: tuple[str, ...]


@dataclass(frozen=True)
class Field:
    id: str
    no: int
    name: str
    type: str
    group: str
    address: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    venue: str
    lat: float
    lon: float
    verified_open: bool


@dataclass(frozen=True)
class Reserve:
    id: str
    name: str
    address: str
    lat: float
    lon: float


@dataclass(frozen=True)
class GameData:
    game: str
    city: str
    groups: dict[str, Group]
    fields: dict[str, Field]
    stations: dict[str, Station]
    reserve: dict[str, Reserve]
    boundary: tuple[tuple[float, float], ...] = ()

    def is_station(self, spot_id: str) -> bool:
        return spot_id in self.stations

    def is_spot(self, spot_id: str) -> bool:
        return spot_id in self.fields or spot_id in self.stations

    @property
    def board(self):
        from .rules import Board

        return Board(
            groups={g.id: g.field_ids for g in self.groups.values()},
            stations=tuple(self.stations),
        )


def _req(obj: dict, key: str, where: str):
    if key not in obj:
        raise GameDataError(f"{where}: Feld '{key}' fehlt")
    return obj[key]


def load_game_data(path: str | Path) -> GameData:
    path = Path(path)
    if not path.exists():
        raise GameDataError(f"Spieldaten-Datei nicht gefunden: {path}")
    with path.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    return parse_game_data(raw)


def parse_game_data(raw: dict) -> GameData:
    groups: dict[str, Group] = {}
    for g in _req(raw, "groups", "root"):
        gid = _req(g, "id", "group")
        if gid in groups:
            raise GameDataError(f"Gruppe doppelt: {gid}")
        groups[gid] = Group(gid, _req(g, "name", gid), _req(g, "color", gid), tuple(_req(g, "field_ids", gid)))

    ids_seen: set[str] = set()

    def _unique(i: str):
        if i in ids_seen:
            raise GameDataError(f"ID doppelt vergeben: {i}")
        ids_seen.add(i)

    fields: dict[str, Field] = {}
    for f in _req(raw, "fields", "root"):
        fid = _req(f, "id", "field")
        _unique(fid)
        fields[fid] = Field(
            id=fid,
            no=int(_req(f, "no", fid)),
            name=_req(f, "name", fid),
            type=f.get("type", "bar"),
            group=_req(f, "group", fid),
            address=f.get("address", ""),
            lat=float(_req(f, "lat", fid)),
            lon=float(_req(f, "lon", fid)),
        )

    stations: dict[str, Station] = {}
    for s in raw.get("stations", []):
        sid = _req(s, "id", "station")
        _unique(sid)
        stations[sid] = Station(
            id=sid,
            name=_req(s, "name", sid),
            venue=s.get("venue", ""),
            lat=float(_req(s, "lat", sid)),
            lon=float(_req(s, "lon", sid)),
            verified_open=bool(s.get("verified_open", True)),
        )

    reserve: dict[str, Reserve] = {}
    for r in raw.get("reserve", []):
        rid = _req(r, "id", "reserve")
        _unique(rid)
        reserve[rid] = Reserve(
            id=rid,
            name=_req(r, "name", rid),
            address=r.get("address", ""),
            lat=float(_req(r, "lat", rid)),
            lon=float(_req(r, "lon", rid)),
        )

    # Konsistenz Gruppen <-> Felder
    for g in groups.values():
        for fid in g.field_ids:
            if fid not in fields:
                raise GameDataError(f"Gruppe {g.id} verweist auf unbekanntes Feld {fid}")
            if fields[fid].group != g.id:
                raise GameDataError(f"Feld {fid} steht in Gruppe {g.id}, hat aber group={fields[fid].group}")
    for f in fields.values():
        if f.group not in groups:
            raise GameDataError(f"Feld {f.id} hat unbekannte Gruppe {f.group}")
        if f.id not in groups[f.group].field_ids:
            raise GameDataError(f"Feld {f.id} fehlt in field_ids der Gruppe {f.group}")

    boundary = tuple((float(p[0]), float(p[1])) for p in raw.get("boundary", []))

    return GameData(
        game=raw.get("game", "Kneipenmonopoly"),
        city=raw.get("city", ""),
        groups=groups,
        fields=fields,
        stations=stations,
        reserve=reserve,
        boundary=boundary,
    )


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Entfernung in Metern (Haversine)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
