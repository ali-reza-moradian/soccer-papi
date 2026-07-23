"""The human-readable alert formatter (src/genz/maker_rt/alerts.py)."""
from __future__ import annotations

from src.genz.maker_rt import alerts


# --------------------------------------------------------------------------- #
# subject: real team/player name, never the ticker                              #
# --------------------------------------------------------------------------- #
def test_subject_tennis_and_ufc_use_the_player_name():
    assert alerts.subject("tennis", "KXWTAMATCH-26JUL24KORKAL", "match_winner", "kalinina") == "Kalinina ML"
    assert alerts.subject("ufc", "KXUFCFIGHT-26JUL25GIBHUS", "fight_winner", "gibson") == "Gibson ML"


def test_subject_mlb_moneyline_resolves_home_away_via_teams():
    assert alerts.subject("mlb", "26JUL241915TORBOS", "ml2", "away", "Blue Jays vs Red Sox") == "Blue Jays ML"
    assert alerts.subject("mlb", "26JUL241915TORBOS", "ml2", "home", "Blue Jays vs Red Sox") == "Red Sox ML"


def test_subject_team_total():
    assert alerts.subject("soccer", "G", "team_total|portland|0.5", "over") == "Portland O0.5"
    assert alerts.subject("soccer", "G", "team_total|kansas city|2.5", "under") == "Kansas City U2.5"


def test_subject_game_total():
    assert alerts.subject("soccer", "G", "total_goals|5.5", "over") == "Total O5.5"


def test_subject_never_returns_the_bare_ticker():
    # an unrecognised market still yields a titled side, not the raw game ticker
    out = alerts.subject("mlb", "KXWEIRD-TICKER", "some_new_market", "yankees")
    assert "KXWEIRD" not in out and out == "Yankees"


def test_head_has_sport_emoji_and_league():
    assert alerts.head("mlb", "G", "ml2", "away", "Yankees vs Sox") == "⚾ MLB · Yankees ML"
    assert alerts.head("tennis", "G", "match_winner", "sinner").startswith("🎾 Tennis · Sinner")


# --------------------------------------------------------------------------- #
# format_event: one line, emoji-led, all the facts                              #
# --------------------------------------------------------------------------- #
def test_placed_line():
    out = alerts.format_event("placed", sport="soccer", game="G", market_key="team_total|portland|0.5",
                              side="over", venue="kalshi", phase="pre", price=0.86, size=5)
    assert out == "🟢 PLACED ⚽ Soccer · Portland O0.5 · $4.30 @ 86c · Kalshi · pre"


def test_repriced_line():
    out = alerts.format_event("repriced", sport="mlb", game="G", market_key="ml2", side="away",
                              teams="Yankees vs Sox", venue="polymarket", phase="inplay",
                              old_price=0.62, new_price=0.64)
    assert out == "🔵 REPRICED ⚾ MLB · Yankees ML · 62c → 64c · Poly · in-play"


def test_cancelled_line_humanizes_reason():
    out = alerts.format_event("cancelled", sport="mlb", game="G", market_key="ml2", side="away",
                              teams="Yankees vs Sox", reason="reprice_cross")
    assert out == "⚪ CANCELLED ⚾ MLB · Yankees ML · reason: price moved"
    aged = alerts.format_event("cancelled", sport="mlb", game="G", market_key="ml2", side="home",
                               teams="Yankees vs Sox", reason="age_out")
    assert "held too long (aged out)" in aged


def test_filled_line():
    out = alerts.format_event("filled", sport="tennis", game="G", market_key="match_winner",
                              side="sinner", price=0.95, size=5)
    assert out == "💰 FILLED 🎾 Tennis · Sinner ML · $4.75 @ 95c · hedging…"


def test_locked_line():
    out = alerts.format_event("locked", sport="tennis", game="G", market_key="match_winner",
                              side="sinner", pnl=0.06, net_pct=1.3, hedge_price=0.04, hedge_venue="polymarket")
    assert out == "✅ LOCKED 🎾 Tennis · Sinner ML · +$0.06 net (+1.3%) · hedge 4c Poly"


def test_unwound_line():
    out = alerts.format_event("unwound", sport="mlb", game="G", market_key="ml2", side="away",
                              teams="Yankees vs Sox", size=5, price=0.61, reason="hedge missed")
    assert out == "🟠 UNWOUND ⚾ MLB · Yankees ML · sold 5 @ 61c · hedge missed"


def test_halted_and_problem_lines_spell_out_the_reason():
    assert alerts.format_event("halted", detail="day loss cap hit (-$25.10)") == \
        "🔴 HALTED day loss cap hit (-$25.10)"
    assert alerts.format_event("problem", detail="place failed — Portland O0.5 · market closed") == \
        "⚠️ PROBLEM place failed — Portland O0.5 · market closed"


def test_missing_facts_degrade_gracefully():
    out = alerts.format_event("placed", sport="mlb", game="G", market_key="ml2", side="away",
                              teams="Yankees vs Sox", venue="kalshi", phase="pre")   # no price/size
    assert "$—" in out and "@ —" in out and out.startswith("🟢 PLACED ⚾ MLB · Yankees ML")


# --------------------------------------------------------------------------- #
# digest line: open X/Y, best edge seen, no jargon                              #
# --------------------------------------------------------------------------- #
def test_digest_line_matches_spec():
    out = alerts.digest_line(15, placed=12, cancelled=9, fills=0, open_now=1, max_open=2, best_edge_pct=0.9)
    assert out == "📊 15m · 12 placed · 9 cancelled · 0 fills · open 1/2 · best edge seen 0.9%"


def test_digest_line_omits_edge_when_none_and_has_no_slot_jargon():
    out = alerts.digest_line(15, placed=3, cancelled=1, fills=0, open_now=2, max_open=2)
    assert "best edge" not in out and "slot-refuse" not in out and "open 2/2" in out


def test_helpers():
    assert alerts.cents(0.86) == "86c" and alerts.cents(None) == "—"
    assert alerts.money(4.3) == "$4.30"
    assert alerts.venue_label("polymarket") == "Poly" and alerts.venue_label("kalshi") == "Kalshi"
    assert alerts.phase_label("inplay") == "in-play" and alerts.phase_label("pre") == "pre"
