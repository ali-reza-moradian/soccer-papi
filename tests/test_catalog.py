"""Tests for market classification, clone groups, tournament resolution, line matching."""
from __future__ import annotations

from src.catalog import (
    build_clone_group_fn,
    build_market_specs,
    classify_family,
    resolve_tournament_ids,
)


# --------------------------------------------------------------------------- #
# classify_family                                                              #
# --------------------------------------------------------------------------- #
def test_classify_core_families():
    assert classify_family("Full Time Result", "1x2", 3, 0) == "1x2"
    assert classify_family("Total Goals Over/Under 2.5", "totals", 2, 2.5) == "totals"
    assert classify_family("Both Teams To Score", "btts", 2, 0) == "btts"
    assert classify_family("Draw No Bet", "dnb", 2, 0) == "dnb"
    assert classify_family("Odd/Even Total Goals", "odd_even", 2, 0) == "odd_even"
    assert classify_family("Asian Handicap -0.5", "asian_handicap", 2, -0.5) == "asian_handicap"
    assert classify_family("3-Way Handicap -1", "1x2", 3, -1) == "euro_handicap"
    assert classify_family("Home Team Total Over/Under 1.5", "totals", 2, 1.5) == "team_totals"


def test_double_chance_is_excluded():
    assert classify_family("Double Chance", "double_chance", 3, 0) is None


def test_unknown_market_returns_none():
    assert classify_family("Correct Score", "correct_score", 17, 0) is None
    assert classify_family("Player to Score", "anytime_scorer", 2, 0) is None


# --------------------------------------------------------------------------- #
# build_market_specs                                                           #
# --------------------------------------------------------------------------- #
SAMPLE_MARKETS = [
    {"marketId": 101, "marketName": "Full Time Result", "marketType": "1x2", "sportId": 10,
     "period": "fulltime", "handicap": 0, "playerProp": False,
     "outcomes": [{"outcomeId": 101, "outcomeName": "1"}, {"outcomeId": 102, "outcomeName": "X"},
                  {"outcomeId": 103, "outcomeName": "2"}]},
    {"marketId": 106, "marketName": "Total Goals Over/Under 2.5", "marketType": "totals", "sportId": 10,
     "period": "fulltime", "handicap": 2.5, "playerProp": False,
     "outcomes": [{"outcomeId": 1, "outcomeName": "Over"}, {"outcomeId": 2, "outcomeName": "Under"}]},
    {"marketId": 200, "marketName": "Double Chance", "marketType": "double_chance", "sportId": 10,
     "period": "fulltime", "handicap": 0, "playerProp": False,
     "outcomes": [{"outcomeId": 1, "outcomeName": "1X"}, {"outcomeId": 2, "outcomeName": "12"},
                  {"outcomeId": 3, "outcomeName": "X2"}]},
    {"marketId": 300, "marketName": "Asian Handicap 2.75", "marketType": "asian_handicap", "sportId": 10,
     "period": "fulltime", "handicap": 2.75, "playerProp": False,
     "outcomes": [{"outcomeId": 1, "outcomeName": "Home"}, {"outcomeId": 2, "outcomeName": "Away"}]},
    {"marketId": 400, "marketName": "Anytime Goalscorer", "marketType": "scorer", "sportId": 10,
     "period": "fulltime", "handicap": 0, "playerProp": True,
     "outcomes": [{"outcomeId": 1, "outcomeName": "Yes"}, {"outcomeId": 2, "outcomeName": "No"}]},
    {"marketId": 999, "marketName": "Tennis Set Winner", "marketType": "1x2", "sportId": 13,
     "period": "fulltime", "handicap": 0, "playerProp": False,
     "outcomes": [{"outcomeId": 1, "outcomeName": "1"}, {"outcomeId": 2, "outcomeName": "2"}]},
]


def test_build_market_specs_accepts_mece_excludes_rest():
    specs, skipped = build_market_specs(SAMPLE_MARKETS, sport_id=10, exclude_names=["double chance"])
    assert set(specs.keys()) == {101, 106, 300}  # DC excluded, scorer is player-prop, tennis wrong sport
    assert specs[101].family == "1x2"
    assert specs[101].line is None
    assert specs[106].family == "totals"
    assert specs[106].line == 2.5
    assert specs[300].has_quarter_line  # 2.75 is a quarter line


def test_quarter_line_detection():
    specs, _ = build_market_specs(SAMPLE_MARKETS, sport_id=10, exclude_names=["double chance"])
    assert specs[300].has_quarter_line is True
    assert specs[106].has_quarter_line is False  # 2.5 is a clean half line


# --------------------------------------------------------------------------- #
# clone groups                                                                 #
# --------------------------------------------------------------------------- #
def test_clone_groups_union_by_cloneof_name():
    books = [
        {"slug": "pinnacle", "bookmakerName": "Pinnacle", "cloneOf": None},
        {"slug": "betsson", "bookmakerName": "Betsson", "cloneOf": "Pinnacle"},  # clone by NAME
        {"slug": "1xbet", "bookmakerName": "1xBet", "cloneOf": None},
        {"slug": "1xbet-clone", "bookmakerName": "Clone", "cloneOf": "1xbet"},   # clone by SLUG
    ]
    group_of = build_clone_group_fn(books)
    assert group_of("pinnacle") == group_of("betsson")
    assert group_of("1xbet") == group_of("1xbet-clone")
    assert group_of("pinnacle") != group_of("1xbet")
    # Unknown books map to themselves.
    assert group_of("mystery") == "mystery"


# --------------------------------------------------------------------------- #
# tournament resolution                                                        #
# --------------------------------------------------------------------------- #
def test_resolve_friendlies_national_only():
    tours = [
        {"tournamentId": 17, "tournamentName": "International Friendlies", "tournamentSlug": "intl-friendlies",
         "categoryName": "International", "categorySlug": "international"},
        {"tournamentId": 18, "tournamentName": "Club Friendlies", "tournamentSlug": "club-friendlies",
         "categoryName": "Club", "categorySlug": "club"},
        {"tournamentId": 1, "tournamentName": "Premier League", "tournamentSlug": "premier-league",
         "categoryName": "England", "categorySlug": "england"},
    ]
    matched = resolve_tournament_ids(tours, "friendl", national_teams_only=True)
    ids = {t["tournamentId"] for t in matched}
    assert ids == {17}  # club friendlies dropped, premier league not matched
