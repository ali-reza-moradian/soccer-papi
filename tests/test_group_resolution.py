"""Earliest-possible group resolution: schedule parse, standings, mathematical clinch, and the
resolves-by / ⚡-within-window estimate."""
from __future__ import annotations

from datetime import datetime, timezone

from src import group_resolution as gr

NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)
GROUP_A = frozenset({"south africa", "mexico", "korea republic", "czechia"})


def _kgame(date, t1, t2, *, winner=None, settled=False):
    """Build the 3 KXWCGAME markets for one game. winner: t1|t2|'tie'|None."""
    et = f"KXWCGAME-{date}XXXYYY"
    out = []
    for sub in (t1, t2, "Tie"):
        res = ""
        if settled:
            res = "yes" if (winner == sub or (winner == "tie" and sub == "Tie")) else "no"
        out.append({"event_ticker": et, "yes_sub_title": sub, "result": res})
    return out


# Group A schedule: MD1+MD2 played (26JUN17/18/21/22), MD3 on 26JUN24.
def _group_a_games(mx_dominant=False):
    g = []
    # MD1 (played)
    g += _kgame("26JUN17MEXKOR", "Mexico", "Korea Republic", winner="Mexico", settled=True)
    g += _kgame("26JUN18CZERSA", "Czechia", "South Africa", winner="Czechia", settled=True)
    # MD2 (played)
    g += _kgame("26JUN21MEXCZE", "Mexico", "Czechia", winner=("Mexico" if mx_dominant else "tie"), settled=True)
    g += _kgame("26JUN21KORRSA", "Korea Republic", "South Africa", winner="tie", settled=True)
    # MD3 (NOT played) on Jun 24
    g += _kgame("26JUN24MEXRSA", "Mexico", "South Africa")
    g += _kgame("26JUN24CZEKOR", "Czechia", "Korea Republic")
    return gr.parse_game_schedule(g)


def test_parse_schedule_and_final_match_date():
    games = _group_a_games()
    grp = [g for g in games if g.teams <= GROUP_A]
    assert len(grp) == 6                                  # 4-team round robin
    last = max(g.date_iso for g in grp)
    assert last.startswith("2026-06-24")                  # MD3 is the group's final match
    played = [g for g in grp if g.winner is not None]
    assert len(played) == 4                               # MD1 + MD2 settled


def test_standings_points():
    st = gr.standings(_group_a_games(), GROUP_A)
    # Mexico: win + draw = 4; Czechia: win + draw = 4; Korea: loss + draw = 1; SA: loss + draw = 1.
    assert st["mexico"]["pts"] == 4 and st["mexico"]["played"] == 2
    assert st["korea republic"]["pts"] == 1 and st["south africa"]["pts"] == 1


def test_clinch_bottom_after_two_rounds():
    """Mexico beats Czechia in MD2 -> Mexico 6, Czechia 3, Korea 1, SA 1 with one game left each.
    SA's max is 1+3=4 < Korea? no (Korea max 4 too). Use a case where bottom IS locked: give Korea &
    SA both 0 pts and max 3 each, below Mexico/Czechia's 6 -> neither bottom locked individually."""
    st = gr.standings(_group_a_games(mx_dominant=True), GROUP_A)
    # Mexico 6, Czechia 3, Korea 1, SA 1. Nobody's winner/bottom is mathematically locked yet.
    assert gr.is_clinched("mexico", "winner", st) is False     # Czechia max 3+3=6, ties Mexico 6 -> not strict
    assert gr.is_clinched("south africa", "bottom", st) is False


def test_clinch_winner_locked():
    # Construct standings where Mexico is mathematically the winner: Mexico 9 (3 wins), others <= 3.
    st = {"mexico": {"pts": 6, "played": 2}, "czechia": {"pts": 0, "played": 2},
          "korea republic": {"pts": 0, "played": 2}, "south africa": {"pts": 0, "played": 2}}
    # Mexico 6, max others = 0 + 3 = 3 < 6 -> winner clinched.
    assert gr.is_clinched("mexico", "winner", st) is True
    # And South Africa bottom: max 0+3=3, others' current pts: mexico 6, cze 0, kor 0 -> NOT < 0.
    assert gr.is_clinched("south africa", "bottom", st) is False
    # Bottom locked example: SA max 0 (played 3), others all > 0.
    st2 = {"mexico": {"pts": 9, "played": 3}, "czechia": {"pts": 4, "played": 3},
           "korea republic": {"pts": 4, "played": 3}, "south africa": {"pts": 0, "played": 3}}
    assert gr.is_clinched("south africa", "bottom", st2) is True


def test_resolution_estimate_final_date_and_window():
    games = _group_a_games()
    est = gr.resolution_estimate(GROUP_A, "winner", "mexico", games, NOW, within_days=3)
    assert est["resolves_by_utc"].startswith("2026-06-24")
    assert est["resolves_by_days"] == 5                   # Jun 19 -> Jun 24
    assert est["clinched"] is False
    # Only the final match (MD3) is unplayed, so the earliest remaining match == resolves_by:
    # it can't resolve EARLY, and it's ~5 days out -> not within the 3-day window.
    assert est["early"] is False
    assert est["within_window"] is False


def test_resolution_estimate_clinched_is_within_window():
    # All group games settled + winner locked -> earliest = now -> within the 3-day window (⚡).
    st_games = _group_a_games()
    # Force a clinched winner by building standings via a fully-decided set is complex; use the
    # estimate path with a clinched team through is_clinched on a locked schedule:
    locked = []
    locked += _kgame("26JUN17MEXKOR", "Mexico", "Korea Republic", winner="Mexico", settled=True)
    locked += _kgame("26JUN18CZERSA", "Czechia", "South Africa", winner="Czechia", settled=True)
    locked += _kgame("26JUN21MEXCZE", "Mexico", "Czechia", winner="Mexico", settled=True)
    locked += _kgame("26JUN21KORRSA", "Korea Republic", "South Africa", winner="Korea Republic", settled=True)
    locked += _kgame("26JUN24MEXRSA", "Mexico", "South Africa", winner="Mexico", settled=True)
    locked += _kgame("26JUN24CZEKOR", "Czechia", "Korea Republic", winner="Czechia", settled=True)
    games = gr.parse_game_schedule(locked)
    st = gr.standings(games, GROUP_A)
    assert gr.is_clinched("mexico", "winner", st) is True          # Mexico 9 -> winner
    est = gr.resolution_estimate(GROUP_A, "winner", "mexico", games, NOW, within_days=3)
    assert est["clinched"] is True and est["early"] is True and est["within_window"] is True
