"""Unit tests for the presentation layer: money, Eastern-time conversion, labels."""
from __future__ import annotations

from datetime import datetime, timezone

from src import formatting as fmt
from src.telegram import format_opportunity


# --- money / numbers --------------------------------------------------------
def test_money_is_two_decimals_with_dollar():
    assert fmt.money(0.195469) == "$0.19"
    assert fmt.money(11.985) == "$11.98"  # banker-free: f-string rounds 11.985 -> 11.98
    assert fmt.money(20) == "$20.00"
    assert fmt.money(None) == "$0.00"


def test_num2_is_two_decimals_no_dollar():
    assert fmt.num2(0.195469) == "0.19"
    assert fmt.num2(2.1) == "2.10"
    assert fmt.num2(None) == "0.00"


# --- timezone ---------------------------------------------------------------
def test_utc_converts_to_eastern():
    # 19:00 UTC on 2026-06-07 is 15:00 EDT (UTC-4 in summer).
    assert fmt.fmt_dt("2026-06-07T19:00:00Z") == "2026-06-07 15:00 EDT"
    assert fmt.fmt_time("2026-06-07T19:00:00Z") == "15:00 EDT"
    assert fmt.date_local("2026-06-07T03:30:00Z") == "2026-06-06"  # 23:30 prev day in ET


def test_iso_local_has_offset():
    iso = fmt.iso_local("2026-06-07T19:00:00Z")
    assert iso == "2026-06-07T15:00:00-0400"


def test_parse_iso_handles_bad_and_none():
    assert fmt.parse_iso("") is None
    assert fmt.parse_iso("not-a-date") is None
    assert fmt.fmt_dt(None) == "?"


# --- market / outcome labels ------------------------------------------------
def test_outcome_label_maps_1x2_to_team_names():
    assert fmt.outcome_label("1", "USA", "Germany") == "USA"
    assert fmt.outcome_label("2", "USA", "Germany") == "Germany"
    assert fmt.outcome_label("X", "USA", "Germany") == "Draw"


def test_outcome_label_totals_carry_line():
    assert fmt.outcome_label("Over", family="totals", line=2.5) == "Over 2.5"
    assert fmt.outcome_label("Under", family="totals", line=2.5) == "Under 2.5"


def test_market_label_makes_line_explicit():
    assert fmt.market_label("Asian Handicap", "asian_handicap", -1.5) == "Asian Handicap (-1.5)"
    assert fmt.market_label("Total Goals", "totals", 2.5) == "Total Goals (2.5)"
    # No line -> unchanged; already-present line -> not duplicated.
    assert fmt.market_label("Match Result", "1x2", None) == "Match Result"
    assert fmt.market_label("Total Goals Over/Under 2.5", "totals", 2.5) == "Total Goals Over/Under 2.5"


# --- full alert -------------------------------------------------------------
def test_format_opportunity_end_to_end():
    arb = {
        "match": "USA vs Germany",
        "home_team": "USA",
        "away_team": "Germany",
        "tournament": "Int. Friendly Games",
        "kickoff_utc": "2026-06-07T19:00:00Z",
        "market": "Total Goals",
        "market_family": "totals",
        "market_line": 2.5,
        "roi_pct": 1.234567,
        "max_liquidity": 315.0,
        "max_profit": 0.195469,
        "actionable": True,
        "legs": [
            {"book": "pinnacle", "outcome": "Over", "decimal_odds": 2.10, "limit": 1500, "stake": 150.0},
            {"book": "1xbet", "outcome": "Under", "decimal_odds": 2.05, "limit": 5000, "stake": 153.66},
        ],
        "bet_links": {},
    }
    msg = format_opportunity(arb)
    # 2-decimal money with $ everywhere.
    assert "stake $150.00" in msg
    assert "stake $153.66" in msg
    assert "limit $1500.00" in msg
    assert "profit <b>$0.19</b>" in msg
    assert "T_max <b>$315.00</b>" in msg
    assert "ROI <b>1.23%</b>" in msg
    # Total investment = sum of stakes.
    assert "Total Investment: $303.66" in msg
    # Market line made explicit + totals outcomes carry the line.
    assert "Total Goals (2.5)" in msg
    assert "Over 2.5" in msg
    assert "Under 2.5" in msg
    # Eastern time, no UTC.
    assert "15:00 EDT" in msg
    assert "UTC" not in msg


def test_format_opportunity_1x2_uses_team_names():
    arb = {
        "match": "USA vs Germany", "home_team": "USA", "away_team": "Germany",
        "tournament": "Friendly", "kickoff_utc": "2026-06-07T19:00:00Z",
        "market": "Match Result", "market_family": "1x2", "market_line": None,
        "roi_pct": 2.0, "max_liquidity": 100.0, "max_profit": 2.0, "actionable": True,
        "legs": [
            {"book": "pinnacle", "outcome": "1", "decimal_odds": 3.5, "limit": 100, "stake": 30.0},
            {"book": "1xbet", "outcome": "X", "decimal_odds": 3.6, "limit": 100, "stake": 30.0},
            {"book": "kalshi", "outcome": "2", "decimal_odds": 3.7, "limit": 100, "stake": 40.0},
        ],
        "bet_links": {},
    }
    msg = format_opportunity(arb)
    assert "— USA @" in msg
    assert "— Germany @" in msg
    assert "— Draw @" in msg
