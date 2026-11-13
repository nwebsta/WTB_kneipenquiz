"""Reine Punkte-Logik.

Keine Datenbank, kein Telegram, keine Uhrzeit von außen: Aus einer Liste von
Ereignissen wird der Spielstand berechnet. `apply()` verarbeitet ein Ereignis,
`replay()` berechnet den kompletten Stand aus dem Protokoll neu.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

# --- Ereignistypen -----------------------------------------------------------
GAME_STARTED = "GAME_STARTED"
GAME_PAUSED = "GAME_PAUSED"
GAME_RESUMED = "GAME_RESUMED"
GAME_ENDED = "GAME_ENDED"
VISIT_APPROVED = "VISIT_APPROVED"      # data: team, spot, submitted_at, task_done, submission_id
EVENT_REVOKED = "EVENT_REVOKED"        # data: event_id, comment
OWNER_SET = "OWNER_SET"                # data: spot, team (oder None), comment
POINTS_ADJUSTED = "POINTS_ADJUSTED"    # data: team, delta, comment
FIELD_DEACTIVATED = "FIELD_DEACTIVATED"  # data: spot
FIELD_REACTIVATED = "FIELD_REACTIVATED"  # data: spot
FIELD_REPLACED = "FIELD_REPLACED"      # data: spot, reserve (oder None = Original)
MULTIPLIER_SET = "MULTIPLIER_SET"      # data: factor, card_id  (z.B. Ereigniskarte "doppelte Punkte")
ACTION_DECIDED = "ACTION_DECIDED"      # data: team, success, requested_at, call_id, card

PHASE_EVENTS = {GAME_STARTED, GAME_PAUSED, GAME_RESUMED, GAME_ENDED}
REVOCABLE_EVENTS = {VISIT_APPROVED, OWNER_SET, POINTS_ADJUSTED, FIELD_DEACTIVATED, FIELD_REACTIVATED,
                    FIELD_REPLACED, MULTIPLIER_SET, ACTION_DECIDED}

# --- Arten von Punktebuchungen ---------------------------------------------
BUY = "buy"                    # freie Bar gekauft
RENT = "rent"                  # Besitzer bekommt Punkte für Besuch
STATION_BUY = "station_buy"
STATION_RENT = "station_rent"
VISITOR = "visitor"            # Punkte für das besuchende Team (Standard 0)
TASK = "task"
STREET_BONUS = "street_bonus"
STREET_LOST = "street_lost"
MANUAL = "manual"
ACTION_OK = "action_ok"
ACTION_FAIL = "action_fail"


@dataclass(frozen=True)
class RuleConfig:
    points_free_field: int = 1
    points_visitor: int = 0
    points_owner: int = 1
    points_owner_street: int = 2
    street_complete_bonus: int = 2
    task_bonus: int = 1
    station_points: tuple[int, ...] = (1, 2, 3, 4)
    action_success: int = 1
    action_fail: int = -1
    double_factor: int = 2

    def station_value(self, count: int) -> int:
        """Punkte, wenn man `count` Bahnhöfe besitzt."""
        if count <= 0:
            return 0
        if count <= len(self.station_points):
            return self.station_points[count - 1]
        return self.station_points[-1] if self.station_points else 0


@dataclass(frozen=True)
class Board:
    groups: dict[str, tuple[str, ...]]  # Gruppe -> Feld-IDs
    stations: tuple[str, ...]

    def group_of(self, spot: str) -> str | None:
        for gid, fids in self.groups.items():
            if spot in fids:
                return gid
        return None


TIMED_BY = {VISIT_APPROVED: "submitted_at", ACTION_DECIDED: "requested_at"}


@dataclass(frozen=True)
class Event:
    id: int
    ts: str          # ISO-Zeit (UTC), wann das Ereignis protokolliert wurde
    type: str
    data: dict[str, Any]
    actor: str = "system"

    @property
    def sort_key(self) -> tuple[str, int]:
        # Besuche zählen ab Einreichung des Fotos, Videoanrufe ab Anfrage, nicht ab Entscheidung.
        key = TIMED_BY.get(self.type)
        if key and self.data.get(key):
            return (self.data[key], self.id)
        return (self.ts, self.id)


@dataclass(frozen=True)
class Entry:
    """Eine Punktebuchung."""
    event_id: int
    team: int
    delta: int
    kind: str
    spot: str | None = None
    group: str | None = None
    other_team: int | None = None
    comment: str | None = None


@dataclass
class State:
    phase: str = "setup"   # setup | running | paused | ended
    started_at: str | None = None
    multiplier: int = 1
    owners: dict[str, int] = field(default_factory=dict)          # Feld/Bahnhof -> Team
    points: dict[int, int] = field(default_factory=dict)
    inactive: set[str] = field(default_factory=set)
    replaced: dict[str, str] = field(default_factory=dict)        # Feld -> Reserve-ID
    street_holder: dict[str, int] = field(default_factory=dict)   # Gruppe -> Team mit Bonus
    street_amount: dict[str, int] = field(default_factory=dict)   # Gruppe -> gutgeschriebener Bonus
    ledger: list[Entry] = field(default_factory=list)

    def copy(self) -> "State":
        return copy.deepcopy(self)

    def points_of(self, team: int) -> int:
        return self.points.get(team, 0)

    def spots_of(self, team: int) -> list[str]:
        return [s for s, t in self.owners.items() if t == team]

    def entries_for(self, event_id: int) -> list[Entry]:
        return [e for e in self.ledger if e.event_id == event_id]


# --- Hilfsfunktionen ---------------------------------------------------------

def station_count(state: State, board: Board, team: int) -> int:
    return sum(1 for s in board.stations if state.owners.get(s) == team)


def street_owner(state: State, board: Board, group: str) -> int | None:
    """Team, dem die ganze Straße gehört, sonst None.

    Deaktivierte Felder ohne Besitzer zählen nicht mit, damit eine geschlossene
    Bar eine Straße nicht unvollendbar macht.
    """
    relevant = [f for f in board.groups.get(group, ()) if not (f in state.inactive and f not in state.owners)]
    if not relevant:
        return None
    owners = {state.owners.get(f) for f in relevant}
    if len(owners) == 1:
        (o,) = owners
        return o
    return None


def _book(state: State, entry: Entry) -> None:
    """Bucht genau den angegebenen Wert (für Korrekturen)."""
    if entry.delta == 0:
        return
    state.points[entry.team] = state.points.get(entry.team, 0) + entry.delta
    state.ledger.append(entry)


def _earn(state: State, entry: Entry) -> None:
    """Bucht Spielpunkte, multipliziert mit dem aktuellen Faktor (z.B. doppelte Punkte)."""
    if state.multiplier != 1:
        entry = Entry(entry.event_id, entry.team, entry.delta * state.multiplier, entry.kind, entry.spot,
                      entry.group, entry.other_team, entry.comment)
    _book(state, entry)


def _update_streets(state: State, board: Board, cfg: RuleConfig, event_id: int) -> None:
    for gid in board.groups:
        new = street_owner(state, board, gid)
        old = state.street_holder.get(gid)
        if new == old:
            continue
        if old is not None:
            _book(state, Entry(event_id, old, -state.street_amount.get(gid, cfg.street_complete_bonus),
                               STREET_LOST, group=gid))
            del state.street_holder[gid]
            state.street_amount.pop(gid, None)
        if new is not None:
            amount = cfg.street_complete_bonus * state.multiplier
            _book(state, Entry(event_id, new, amount, STREET_BONUS, group=gid))
            state.street_holder[gid] = new
            state.street_amount[gid] = amount


def _rent(state: State, board: Board, cfg: RuleConfig, ev_id: int, spot: str, owner: int, visitor: int) -> None:
    """Besitzer bekommt Punkte für den Besuch eines fremden Teams."""
    if spot in board.stations:
        n = station_count(state, board, owner)
        _earn(state, Entry(ev_id, owner, cfg.station_value(n), STATION_RENT, spot=spot, other_team=visitor))
    else:
        group = board.group_of(spot)
        full = group is not None and street_owner(state, board, group) == owner
        value = cfg.points_owner_street if full else cfg.points_owner
        _earn(state, Entry(ev_id, owner, value, RENT, spot=spot, group=group, other_team=visitor))


def _apply_visit(state: State, board: Board, cfg: RuleConfig, ev: Event) -> None:
    team = int(ev.data["team"])
    spot = ev.data["spot"]
    owner = state.owners.get(spot)
    is_station = spot in board.stations
    group = None if is_station else board.group_of(spot)

    if owner is None:
        state.owners[spot] = team
        if is_station:
            n = station_count(state, board, team)
            _earn(state, Entry(ev.id, team, cfg.station_value(n), STATION_BUY, spot=spot))
        else:
            _earn(state, Entry(ev.id, team, cfg.points_free_field, BUY, spot=spot, group=group))
    elif owner != team:
        _rent(state, board, cfg, ev.id, spot, owner, team)
        _earn(state, Entry(ev.id, team, cfg.points_visitor, VISITOR, spot=spot, other_team=owner))
    # eigenes Feld: keine Punkte

    if ev.data.get("task_done"):
        _earn(state, Entry(ev.id, team, cfg.task_bonus, TASK, spot=spot))


def apply(state: State, ev: Event, board: Board, cfg: RuleConfig) -> State:
    """Wendet ein Ereignis an und gibt einen NEUEN Zustand zurück."""
    s = state.copy()
    t = ev.type
    if t == GAME_STARTED:
        s.phase = "running"
        s.started_at = s.started_at or ev.ts
    elif t == GAME_PAUSED:
        s.phase = "paused"
    elif t == GAME_RESUMED:
        s.phase = "running"
    elif t == GAME_ENDED:
        s.phase = "ended"
    elif t == VISIT_APPROVED:
        _apply_visit(s, board, cfg, ev)
    elif t == ACTION_DECIDED:
        ok = bool(ev.data.get("success"))
        _earn(s, Entry(ev.id, int(ev.data["team"]), cfg.action_success if ok else cfg.action_fail,
                       ACTION_OK if ok else ACTION_FAIL, comment=ev.data.get("card")))
    elif t == MULTIPLIER_SET:
        s.multiplier = int(ev.data.get("factor", 1))
    elif t == OWNER_SET:
        spot = ev.data["spot"]
        team = ev.data.get("team")
        if team is None:
            s.owners.pop(spot, None)
        else:
            s.owners[spot] = int(team)
    elif t == POINTS_ADJUSTED:
        _book(s, Entry(ev.id, int(ev.data["team"]), int(ev.data["delta"]), MANUAL, comment=ev.data.get("comment")))
    elif t == FIELD_DEACTIVATED:
        s.inactive.add(ev.data["spot"])
    elif t == FIELD_REACTIVATED:
        s.inactive.discard(ev.data["spot"])
    elif t == FIELD_REPLACED:
        spot = ev.data["spot"]
        if ev.data.get("reserve"):
            s.replaced[spot] = ev.data["reserve"]
        else:
            s.replaced.pop(spot, None)
        s.inactive.discard(spot)
    elif t == EVENT_REVOKED:
        pass  # wird in replay() behandelt
    else:
        raise ValueError(f"Unbekannter Ereignistyp: {t}")
    _update_streets(s, board, cfg, ev.id)
    return s


def replay(events: list[Event], board: Board, cfg: RuleConfig) -> State:
    """Berechnet den Spielstand komplett aus dem Protokoll."""
    revoked = {int(e.data["event_id"]) for e in events if e.type == EVENT_REVOKED}
    state = State()
    for ev in sorted(events, key=lambda e: e.sort_key):
        if ev.type == EVENT_REVOKED or ev.id in revoked:
            continue
        state = apply(state, ev, board, cfg)
    return state


def preview(state: State, ev: Event, board: Board, cfg: RuleConfig) -> list[Entry]:
    """Welche Buchungen würde dieses Ereignis auf dem aktuellen Stand auslösen?

    Achtung: rechnet am ENDE des Verlaufs. Für die Prüf-Vorschau `preview_in_history` nehmen.
    """
    return apply(state, ev, board, cfg).entries_for(ev.id)


def preview_in_history(events: list[Event], ev: Event, board: Board, cfg: RuleConfig) -> list[Entry]:
    """Buchungen des Ereignisses genau so, wie die echte Freigabe sie bucht.

    Das Ereignis wird wie bei `replay` an seiner zeitlichen Stelle einsortiert (Uploadzeit des Fotos,
    Anfragezeit der Aktion). So stimmt die Vorschau auch, wenn sich seit dem Upload etwas geändert hat,
    z.B. doppelte Punkte oder eine Besitz-Korrektur.
    """
    return replay(list(events) + [ev], board, cfg).entries_for(ev.id)


def ranking(points: dict[int, int], teams: list[int]) -> list[tuple[int, int, int]]:
    """(Platz, Team, Punkte), gleiche Punkte = gleicher Platz. Kein Tiebreak."""
    rows = sorted(((points.get(t, 0), t) for t in teams), key=lambda r: (-r[0], r[1]))
    out: list[tuple[int, int, int]] = []
    for i, (p, t) in enumerate(rows):
        place = out[-1][0] if out and out[-1][2] == p else i + 1
        out.append((place, t, p))
    return out
