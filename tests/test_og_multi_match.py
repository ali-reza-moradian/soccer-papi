"""Event -> tree-game matching per sport: identity + date/time gates, decoy rejection, ambiguity."""
from __future__ import annotations

from datetime import datetime, timezone

from src.logsetup import get_logger
from src.og_multi import match
from src.og_multi.match import _sport_fns

LOG = get_logger("test-ogm-match")
NOW = datetime(2026, 7, 19, 23, 0, 0, tzinfo=timezone.utc)


def _wnode(mt, side, label):
    return {"market_type": mt, "market_key": mt, "side": side, "outcome_label": label, "kind": "2way"}


def _mlb_game(home, away, kickoff):
    return {"home": home, "away": away, "kickoff_utc": kickoff, "date": kickoff[:10],
            "nodes": [_wnode("ml2", "home", home), _wnode("ml2", "away", away)]}


def _combat_game(sport, a, b, day):
    mt = "match_winner" if sport == "tennis" else "fight_winner"
    surname, _ = _sport_fns(sport)
    return {"home": b, "away": a, "date": day, "kickoff_utc": day + "T13:00:00Z",
            "nodes": [_wnode(mt, surname(a), a), _wnode(mt, surname(b), b)]}


def _ev(home, away, commence):
    return {"home_team": home, "away_team": away, "commence_time": commence}


# --- MLB -------------------------------------------------------------------- #
def test_mlb_match_both_orientations():
    games = {"g1": _mlb_game("New York Yankees", "Los Angeles Dodgers", "2026-07-19T23:20:00Z")}
    r = match.match_events("mlb", [_ev("New York Yankees", "Los Angeles Dodgers", "2026-07-19T23:30:00Z")],
                           games, now=NOW, log=LOG)
    assert "g1" in r.by_game and r.by_game["g1"][1].method == "team_tokens"
    r2 = match.match_events("mlb", [_ev("Los Angeles Dodgers", "New York Yankees", "2026-07-19T23:10:00Z")],
                            games, now=NOW, log=LOG)
    assert "g1" in r2.by_game                       # unordered identity


def test_mlb_decoy_wrong_time_rejected():
    games = {"g1": _mlb_game("New York Yankees", "Los Angeles Dodgers", "2026-07-19T23:20:00Z")}
    r = match.match_events("mlb", [_ev("New York Yankees", "Los Angeles Dodgers", "2026-07-20T05:00:00Z")],
                           games, now=NOW, log=LOG)
    assert not r.by_game and r.unmatched          # same teams, +5h40m -> outside +/-90min


def test_mlb_token_containment_abbreviation():
    games = {"g1": _mlb_game("Oakland Athletics", "Seattle Mariners", "2026-07-19T23:20:00Z")}
    r = match.match_events("mlb", [_ev("Athletics", "Seattle Mariners", "2026-07-19T23:20:00Z")],
                           games, now=NOW, log=LOG)
    assert "g1" in r.by_game                       # {athletics} subset of {oakland, athletics}


def test_mlb_ambiguous_two_games_refused():
    games = {"g1": _mlb_game("New York Yankees", "Los Angeles Dodgers", "2026-07-19T23:20:00Z"),
             "g2": _mlb_game("New York Yankees", "Los Angeles Dodgers", "2026-07-19T23:40:00Z")}
    r = match.match_events("mlb", [_ev("New York Yankees", "Los Angeles Dodgers", "2026-07-19T23:30:00Z")],
                           games, now=NOW, log=LOG)
    assert not r.by_game and r.ambiguous          # within 90min of BOTH -> refuse (never guess)


# --- tennis / UFC ----------------------------------------------------------- #
def test_tennis_surname_match_and_date_decoy():
    games = {"t1": _combat_game("tennis", "Andrey Rublev", "Alexei Tabilo", "2026-07-18")}
    r = match.match_events("tennis", [_ev("Andrey Rublev", "Alexei Tabilo", "2026-07-18T13:00:00Z")],
                           games, now=NOW, log=LOG)
    assert "t1" in r.by_game and r.by_game["t1"][1].method == "surname_tokens"
    r2 = match.match_events("tennis", [_ev("Andrey Rublev", "Alexei Tabilo", "2026-07-21T13:00:00Z")],
                            games, now=NOW, log=LOG)
    assert not r2.by_game                          # 3 days off -> outside date +/-1


def test_tennis_matches_despite_imperfect_tree_home_label():
    """The tree home/away can be imperfectly parsed; the winner nodes' surname `side`s carry the match."""
    g = {"home": "Tabilo: Round of 32", "away": "Rublev", "date": "2026-07-18",
         "kickoff_utc": "2026-07-18T13:00:00Z",
         "nodes": [_wnode("match_winner", "rublev", "Andrey Rublev"),
                   _wnode("match_winner", "tabilo", "Alexei Tabilo")]}
    r = match.match_events("tennis", [_ev("Andrey Rublev", "Alexei Tabilo", "2026-07-18T13:00:00Z")],
                           {"t1": g}, now=NOW, log=LOG)
    assert "t1" in r.by_game


def test_ufc_fighter_match():
    games = {"u1": _combat_game("ufc", "Ezra Elliott", "Damien Anderson", "2026-07-18")}
    r = match.match_events("ufc", [_ev("Ezra Elliott", "Damien Anderson", "2026-07-18T02:00:00Z")],
                           games, now=NOW, log=LOG)
    assert "u1" in r.by_game and r.by_game["u1"][1].method == "fighter_tokens"


def test_name_eq_routes_outcomes():
    assert match.name_eq("mlb", "Los Angeles Dodgers", "Los Angeles Dodgers")
    assert match.name_eq("tennis", "Andrey Rublev", "Rublev, Andrey")   # token containment
    assert match.name_eq("ufc", "Damien Anderson", "Damien Anderson")
    assert not match.name_eq("mlb", "New York Yankees", "Los Angeles Dodgers")
