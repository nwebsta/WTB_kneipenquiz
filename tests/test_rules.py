import pytest

from kneipenmonopoly import rules as R
from kneipenmonopoly.rules import Board, Event, RuleConfig, replay

BOARD = Board(
    groups={"braun": ("b1", "b2"), "pink": ("p1", "p2", "p3")},
    stations=("n", "o", "s", "w"),
)
CFG = RuleConfig()
A, B, C = 1, 2, 3


class Log:
    """Kleiner Helfer, um Ereignislisten mit fortlaufender ID/Zeit zu bauen."""

    def __init__(self):
        self.events: list[Event] = []

    def add(self, type_, actor="test", ts=None, **data) -> Event:
        i = len(self.events) + 1
        ev = Event(i, ts or f"2026-10-13T20:{i:02d}:00+00:00", type_, data, actor)
        self.events.append(ev)
        return ev

    def visit(self, team, spot, task=False, submitted_at=None) -> Event:
        i = len(self.events) + 1
        return self.add(R.VISIT_APPROVED, team=team, spot=spot, task_done=task,
                        submitted_at=submitted_at or f"2026-10-13T20:{i:02d}:00+00:00")

    def state(self, cfg=CFG):
        return replay(self.events, BOARD, cfg)


def test_free_bar_gives_ownership_and_point():
    log = Log()
    log.visit(A, "p1")
    s = log.state()
    assert s.owners["p1"] == A
    assert s.points_of(A) == 1


def test_foreign_bar_owner_gets_point_visitor_nothing():
    log = Log()
    log.visit(A, "p1")
    log.visit(B, "p1")
    s = log.state()
    assert s.owners["p1"] == A
    assert s.points_of(A) == 2
    assert s.points_of(B) == 0


def test_visitor_points_configurable():
    log = Log()
    log.visit(A, "p1")
    log.visit(B, "p1")
    s = log.state(RuleConfig(points_visitor=1))
    assert s.points_of(B) == 1


def test_own_bar_gives_nothing():
    log = Log()
    log.visit(A, "p1")
    log.visit(A, "p1")
    assert log.state().points_of(A) == 1


def test_street_complete_one_time_bonus():
    log = Log()
    log.visit(A, "b1")
    log.visit(A, "b2")
    s = log.state()
    # 1 + 1 + Bonus 2
    assert s.points_of(A) == 4
    assert s.street_holder["braun"] == A
    kinds = [e.kind for e in s.ledger]
    assert kinds.count(R.STREET_BONUS) == 1


def test_foreign_bar_with_full_street_gives_double():
    log = Log()
    log.visit(A, "b1")
    log.visit(A, "b2")
    log.visit(B, "b1")
    s = log.state()
    assert s.points_of(A) == 4 + 2
    assert s.points_of(B) == 0


def test_foreign_bar_without_full_street_single():
    log = Log()
    log.visit(A, "p1")
    log.visit(A, "p2")
    log.visit(B, "p3")
    log.visit(C, "p1")
    s = log.state()
    assert s.points_of(A) == 1 + 1 + 1


@pytest.mark.parametrize("n,expected_total", [(1, 1), (2, 3), (3, 6), (4, 10)])
def test_free_stations_count_up(n, expected_total):
    log = Log()
    for st in ["n", "o", "s", "w"][:n]:
        log.visit(A, st)
    assert log.state().points_of(A) == expected_total


def test_foreign_station_pays_owner_station_count():
    log = Log()
    log.visit(A, "n")
    log.visit(A, "o")
    log.visit(A, "s")
    before = log.state().points_of(A)
    log.visit(B, "o")
    s = log.state()
    assert s.points_of(A) - before == 3
    assert s.points_of(B) == 0


def test_stations_do_not_count_as_street():
    log = Log()
    for st in ["n", "o", "s", "w"]:
        log.visit(A, st)
    assert log.state().street_holder == {}


def test_task_bonus():
    log = Log()
    log.visit(A, "p1", task=True)
    log.visit(B, "p1", task=True)
    s = log.state()
    assert s.points_of(A) == 1 + 1 + 1  # Kauf, Aufgabe, Miete von B
    assert s.points_of(B) == 1          # nur Aufgabe


def test_simultaneous_free_bar_first_submitted_wins():
    log = Log()
    # B wird zuerst freigegeben, aber A hat früher eingereicht
    log.visit(B, "p1", submitted_at="2026-10-13T20:10:05+00:00")
    log.visit(A, "p1", submitted_at="2026-10-13T20:10:00+00:00")
    s = log.state()
    assert s.owners["p1"] == A
    assert s.points_of(A) == 2  # Kauf + Miete von B
    assert s.points_of(B) == 0


def test_revoke_visit_restores_everything():
    log = Log()
    log.visit(A, "b1")
    v = log.visit(A, "b2")
    log.visit(B, "b1")  # doppelte Miete, weil Straße komplett
    assert log.state().points_of(A) == 6
    log.add(R.EVENT_REVOKED, event_id=v.id)
    s = log.state()
    assert "b2" not in s.owners
    assert "braun" not in s.street_holder
    # nur Kauf b1 + einfache Miete
    assert s.points_of(A) == 2


def test_owner_set_breaks_street_and_removes_bonus():
    log = Log()
    log.visit(A, "b1")
    log.visit(A, "b2")
    log.add(R.OWNER_SET, spot="b2", team=B, comment="Fehler")
    s = log.state()
    assert s.owners["b2"] == B
    assert s.points_of(A) == 2  # Bonus wieder weg
    assert s.points_of(B) == 0  # Besitzwechsel bringt keine Punkte
    assert "braun" not in s.street_holder


def test_owner_set_none_frees_field():
    log = Log()
    log.visit(A, "p1")
    log.add(R.OWNER_SET, spot="p1", team=None)
    s = log.state()
    assert "p1" not in s.owners
    assert s.points_of(A) == 1  # Punkte bleiben


def test_manual_points_and_revoke():
    log = Log()
    adj = log.add(R.POINTS_ADJUSTED, team=A, delta=-3, comment="Schummeln")
    assert log.state().points_of(A) == -3
    log.add(R.EVENT_REVOKED, event_id=adj.id)
    assert log.state().points_of(A) == 0


def test_deactivated_unowned_field_does_not_block_street():
    log = Log()
    log.visit(A, "p1")
    log.visit(A, "p2")
    log.add(R.FIELD_DEACTIVATED, spot="p3")
    s = log.state()
    assert "p3" in s.inactive
    assert s.street_holder["pink"] == A


def test_deactivated_owned_field_keeps_owner():
    log = Log()
    log.visit(A, "p1")
    log.add(R.FIELD_DEACTIVATED, spot="p1")
    s = log.state()
    assert s.owners["p1"] == A
    assert s.points_of(A) == 1


def test_replace_keeps_owner_and_reactivates():
    log = Log()
    log.visit(A, "p1")
    log.add(R.FIELD_DEACTIVATED, spot="p1")
    log.add(R.FIELD_REPLACED, spot="p1", reserve="bisou")
    s = log.state()
    assert s.owners["p1"] == A
    assert s.replaced["p1"] == "bisou"
    assert "p1" not in s.inactive


def test_phases():
    log = Log()
    assert log.state().phase == "setup"
    log.add(R.GAME_STARTED)
    assert log.state().phase == "running"
    log.add(R.GAME_PAUSED)
    assert log.state().phase == "paused"
    log.add(R.GAME_RESUMED)
    log.add(R.GAME_ENDED)
    assert log.state().phase == "ended"


def test_replay_is_deterministic_and_pure():
    log = Log()
    log.visit(A, "b1")
    log.visit(B, "b2")
    log.visit(C, "n")
    s1 = log.state()
    s2 = log.state()
    assert s1.points == s2.points and s1.owners == s2.owners


def test_apply_does_not_mutate_input():
    s0 = R.State()
    ev = Event(1, "2026-10-13T20:00:00+00:00", R.VISIT_APPROVED, {"team": A, "spot": "p1", "submitted_at": "x"})
    s1 = R.apply(s0, ev, BOARD, CFG)
    assert s0.owners == {} and s1.owners == {"p1": A}


def test_preview_matches_apply():
    log = Log()
    log.visit(A, "b1")
    ev = Event(99, "2026-10-13T21:00:00+00:00", R.VISIT_APPROVED,
               {"team": A, "spot": "b2", "submitted_at": "2026-10-13T21:00:00+00:00"})
    entries = R.preview(log.state(), ev, BOARD, CFG)
    assert sorted(e.kind for e in entries) == [R.BUY, R.STREET_BONUS]


def test_double_points_after_multiplier_by_upload_time():
    log = Log()
    log.visit(A, "p1", submitted_at="2026-10-13T20:00:00+00:00")
    log.add(R.MULTIPLIER_SET, ts="2026-10-13T20:30:00+00:00", factor=2)
    # vor 20:30 hochgeladen, aber erst später freigegeben: einfach
    log.visit(B, "b1", submitted_at="2026-10-13T20:29:00+00:00")
    # nach 20:30 hochgeladen: doppelt (Miete für A)
    log.visit(B, "p1", submitted_at="2026-10-13T20:31:00+00:00")
    s = log.state()
    assert s.points_of(B) == 1
    assert s.points_of(A) == 1 + 2


def test_double_points_street_bonus_removed_with_same_amount():
    log = Log()
    log.add(R.MULTIPLIER_SET, ts="2026-10-13T19:00:00+00:00", factor=2)
    log.visit(A, "b1")
    log.visit(A, "b2")
    assert log.state().points_of(A) == 2 + 2 + 4
    log.add(R.OWNER_SET, spot="b2", team=None)
    assert log.state().points_of(A) == 4


def test_revoking_multiplier_restores_single_points():
    log = Log()
    m = log.add(R.MULTIPLIER_SET, ts="2026-10-13T19:00:00+00:00", factor=2)
    log.visit(A, "p1")
    assert log.state().points_of(A) == 2
    log.add(R.EVENT_REVOKED, event_id=m.id)
    assert log.state().points_of(A) == 1


def test_action_cards_plus_minus_and_doubled():
    log = Log()
    log.add(R.ACTION_DECIDED, team=A, success=True, requested_at="2026-10-13T20:00:00+00:00")
    log.add(R.ACTION_DECIDED, team=B, success=False, requested_at="2026-10-13T20:00:00+00:00")
    log.add(R.MULTIPLIER_SET, ts="2026-10-13T20:10:00+00:00", factor=2)
    log.add(R.ACTION_DECIDED, team=A, success=True, requested_at="2026-10-13T20:11:00+00:00")
    s = log.state()
    assert s.points_of(A) == 1 + 2
    assert s.points_of(B) == -1


def test_started_at_recorded():
    log = Log()
    st = log.add(R.GAME_STARTED)
    assert log.state().started_at == st.ts


def test_ranking_ties_share_place():
    r = R.ranking({1: 5, 2: 7, 3: 5, 4: 1}, [1, 2, 3, 4])
    assert r == [(1, 2, 7), (2, 1, 5), (2, 3, 5), (4, 4, 1)]
