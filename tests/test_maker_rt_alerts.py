"""Plain-language Telegram alerts (src/genz/maker_rt/alerts.py): every alert is self-explanatory to a
non-technical reader — real names, $ + cents-price + shares + venue + phase, and always the WHY. Never
a ticker or a UUID."""
from __future__ import annotations

from src.genz.maker_rt import alerts


# --------------------------------------------------------------------------- #
# names: real team/player, never a ticker                                       #
# --------------------------------------------------------------------------- #
def test_bet_name_and_matchup_use_real_names():
    assert alerts.bet_name("tennis", "KXATP-...", "match_winner", "hanfmann") == "Hanfmann"
    assert alerts.matchup("tennis", "G", "match_winner", "hanfmann", "Hanfmann vs Halys") == "Hanfmann vs Halys"
    assert alerts.other_name("Hanfmann", "Hanfmann vs Halys") == "Halys"
    assert alerts.bet_name("mlb", "G", "ml2", "away", "Blue Jays vs Red Sox") == "Blue Jays"
    assert alerts.bet_name("soccer", "G", "team_total|portland|0.5", "over") == "Portland O0.5"


def test_no_ticker_or_uuid_ever_leaks():
    out = alerts.format_event("placed", sport="ufc", game="KXUFCFIGHT-26JUL25GIBHUS", market_key="fight_winner",
                              side="gibson", venue="polymarket", phase="pre", price=0.16, size=100,
                              hedge_venue="kalshi", hedge_price=0.82)
    assert "KXUFC" not in out and "Gibson" in out


# --------------------------------------------------------------------------- #
# each alert type renders the required facts                                     #
# --------------------------------------------------------------------------- #
def test_placed_is_self_explanatory():
    out = alerts.format_event("placed", sport="tennis", game="G", market_key="match_winner", side="hanfmann",
                              teams="Hanfmann vs Halys", venue="polymarket", phase="inplay", price=0.56,
                              size=22, hedge_venue="kalshi", hedge_price=0.41, exp_net_usd=0.31,
                              exp_net_pct=1.3, open_now=2, max_open=2, fills_today=3, stake_today=47.0,
                              stake_cap=300.0)
    assert out.startswith("🟢 PLACED · 🎾 Tennis · Hanfmann vs Halys")
    assert "Offering $12.32 on Hanfmann to win @ 56¢ (22 shares) on Polymarket · in-play" in out
    assert "hedge on Kalshi @ 41¢" in out and "locks +$0.31" in out
    assert "open 2/2" in out and "3 fills, $47.00 of $300.00 used" in out


def test_filled_names_the_bet_and_says_hedging():
    out = alerts.format_event("filled", sport="tennis", game="G", market_key="match_winner", side="hanfmann",
                              teams="Hanfmann vs Halys", price=0.56, size=22)
    assert out.startswith("💰 FILLED! · 🎾 Tennis · Hanfmann vs Halys")
    assert "Someone took $12.32 of Hanfmann @ 56¢ (22 sh) · buying the hedge now…" in out


def test_locked_shows_both_legs_risk_payout_and_running_totals():
    out = alerts.format_event("locked", sport="tennis", game="G", market_key="match_winner", side="hanfmann",
                              teams="Hanfmann vs Halys", venue="polymarket", hedge_venue="kalshi",
                              rest_price=0.56, rest_shares=22, hedge_price=0.41, hedge_shares=22,
                              hedge_fee=0.08, pnl=0.31, net_pct=1.45, fills_today=4, today_pnl=1.12,
                              lifetime_pnl=1.37)
    assert "profit is GUARANTEED either way" in out
    assert "Bought: Hanfmann $12.32 @ 56¢ (Poly) + Halys $9.02 @ 41¢ (Kalshi)" in out
    assert "Total risked $21.42 → pays $22.00 · 💵 net +$0.31 after fees (+1.45% ROI)" in out
    assert "📈 Today: 4 fills · +$1.12 · Lifetime: +$1.37 (settled + today's estimate)" in out


# --------------------------------------------------------------------------- #
# THE WORDS MATCH THE POSITION — nothing is "guaranteed" while shares ride naked #
# --------------------------------------------------------------------------- #
def _locked(**kw):
    base = dict(sport="soccer", game="26AUG04JEJBMU", market_key="total_goals|5.5", side="over",
                teams="Jeju SK FC vs Bayern Munich", venue="kalshi", hedge_venue="polymarket",
                rest_price=0.65, rest_shares=135.65, hedge_price=0.3457, hedge_shares=142.2222,
                hedge_fee=0.0, pnl=1.80, net_pct=1.34)
    base.update(kw)
    return alerts.format_event("locked", **base)


def test_an_unhedged_remainder_forbids_the_word_guaranteed():
    """26AUG04JEJBMU O/U5.5: 142.2222 Polymarket shares against 135.65 on Kalshi. The pair made +$1.80
    and the 6.5722-share remainder lost $2.27 (-100%) — so the trade LOST money while the alert had
    announced it as a lock. "Guaranteed" is only ever true of the MATCHED shares."""
    out = _locked()
    assert "GUARANTEED" not in out
    assert "hedged 135.65 sh · 6.5722 sh riding unhedged ($2.27 at risk, settles on its own)" in out
    assert "one-way bet I could not pair off" in out
    assert "→ pays $135.65" in out, "the payout names the matched shares, not the whole fill"


def test_the_remainder_is_priced_on_whichever_leg_actually_holds_it():
    """More hedge than rest is an over-bought hedge exposed at the HEDGE price; more rest than hedge is
    an under-hedged fill exposed at the REST price. Reading the wrong one understates the risk."""
    assert "$2.27 at risk" in _locked()                                    # 6.5722 x 0.3457 (hedge)
    out = _locked(rest_shares=142.2222, hedge_shares=135.65, rest_price=0.65)
    assert "$4.27 at risk" in out                                          # 6.5722 x 0.65 (rest)


def test_a_matched_pair_is_still_allowed_to_say_guaranteed():
    """The rule must not fire on the fractional dust both venues fill to — otherwise every single lock
    carries a warning and the warning stops meaning anything."""
    out = _locked(rest_shares=135.65, hedge_shares=135.6512)
    assert "profit is GUARANTEED either way" in out and "riding unhedged" not in out


def test_a_dust_fill_with_no_hedge_never_uses_the_locked_template():
    """The 1-share Jeju/Bayern U1.5 fill at 3c was announced "profit is GUARANTEED either way · pays
    $1.00" with the hedge rendered "$— @ —": a message that showed no hedge and promised a certainty in
    the same breath."""
    out = _locked(market_key="total_goals|1.5", rest_price=0.03, rest_shares=1,
                  hedge_price=None, hedge_shares=0, pnl=0.008, net_pct=None)
    assert "GUARANTEED" not in out and "LOCKED" not in out
    assert "tiny fill, no hedge bought for it" in out
    assert "too small to hedge on its own" in out
    assert "it pays $1.00 if" in out and "worth nothing if not" in out
    assert "settles on its own" in out


def test_lifetime_says_which_lifetime_it_means():
    """Overnight on 2026-08-04 the alerts read "Lifetime: +$33.62"; the settled truth after the morning
    was +$32.38. The difference is today's fill-time ESTIMATE of pairs that had not settled — a
    different kind of number wearing the same word."""
    out = _locked(fills_today=4, today_pnl=1.24, lifetime_pnl=33.62, lifetime_settled=32.38)
    assert "Lifetime: +$32.38 settled (+$33.62 including today's estimate)" in out
    bare = _locked(fills_today=4, today_pnl=1.24, lifetime_pnl=33.62)
    assert "Lifetime: +$33.62 (settled + today's estimate)" in bare


def test_settled_names_the_winner_and_lifetime():
    out = alerts.format_event("settled", sport="tennis", game="G", market_key="match_winner", side="hanfmann",
                              teams="Hanfmann vs Halys", winner="Halys", payout=22.00, cost=21.42,
                              pnl=0.31, roi_pct=1.45, lifetime_pnl=1.37)
    assert out.startswith("🏁 SETTLED · 🎾 Tennis · Hanfmann vs Halys · Halys won")
    assert "Collected $22.00 on $21.42 · 💵 +$0.31 (+1.45%) · Lifetime +$1.37 settled" in out


def test_a_naked_remainder_settling_says_what_it_is():
    """One game emits TWO settled alerts — the matched pair and the shares that were never paired off.
    Unlabelled, a +$1.80 beside a -$2.27 (-100%) reads as two independent bets, one of which
    inexplicably lost everything."""
    out = alerts.format_event("settled", sport="soccer", game="26AUG04JEJBMU",
                              market_key="total_goals|5.5", side="over",
                              teams="Jeju SK FC vs Bayern Munich", winner="Under 5.5", payout=0.0,
                              cost=2.27, pnl=-2.2737, roi_pct=-100.0, untracked=True)
    assert "the UNHEDGED REMAINDER of this trade" in out
    assert "could not pair off" in out and "luck, not edge" in out
    assert "hedged pair on this game is reported separately" in out


def test_unwound_explains_the_safety_net():
    out = alerts.format_event("unwound", sport="mlb", game="G", market_key="ml2", side="away",
                              teams="Rays vs Jays", size=5, price=0.61, cost=0.18)
    assert out.startswith("🟠 UNWOUND · ⚾ MLB · Rays vs Jays · couldn't hedge in time, sold back")
    assert "Cost me $0.18 (that's the safety net working — no open risk)" in out


def test_cancelled_gives_duration_and_plain_reason():
    out = alerts.format_event("cancelled", sport="tennis", game="G", market_key="match_winner", side="hanfmann",
                              teams="Hanfmann vs Halys", reason="now_behind", age_s=252)
    assert "offer pulled after 4m 12s" in out
    assert "Reason: someone outbid me — my price is no longer best" in out


def test_stopped_and_problem_are_plain_and_actionable():
    stop = alerts.format_event("halted", detail="Trading is PAUSED — check both venues are flat.")
    assert stop == "🔴 STOPPED · Trading is PAUSED — check both venues are flat."
    prob = alerts.format_event("problem", detail="Couldn't place my offer on Gibson — the market has closed.")
    assert prob.startswith("⚠️ PROBLEM · Couldn't place my offer on Gibson")


# --------------------------------------------------------------------------- #
# digest: plain roll-up with the WHY                                            #
# --------------------------------------------------------------------------- #
def test_digest_is_plain_with_usage_and_why():
    out = alerts.digest_line(15, placed=42, cancelled=9, fills=0, open_now=2, max_open=2, best_edge_pct=1.1,
                             stake_today=47.0, stake_cap=300.0, today_pnl=1.12,
                             why_no_fills="nobody crossed our price yet")
    assert out.startswith("📊 15m summary · 42 offers placed · 0 taken · best edge seen 1.1%")
    assert "Currently offering 2/2 · today $47.00/$300.00 used · 0 fills · +$1.12" in out
    assert "Why no fills: nobody crossed our price yet" in out


def test_digest_flap_warning_is_plain():
    out = alerts.digest_line(15, placed=3, cancelled=1, fills=1, open_now=2, max_open=2,
                             kalshi_flaps=3, kalshi_down_s=12.0)
    assert "Kalshi feed was flaky: 3 drops (12s)" in out
    assert "Why no fills" not in out                         # there WERE fills


# --------------------------------------------------------------------------- #
# small helpers                                                                 #
# --------------------------------------------------------------------------- #
def test_helpers():
    assert alerts.cents(0.56) == "56¢" and alerts.cents(None) == "—"
    assert alerts.money(4.3) == "$4.30" and alerts.signed_money(-0.18) == "-$0.18"
    assert alerts.dur(252) == "4m 12s" and alerts.dur(9) == "9s" and alerts.dur(4562) == "1h 16m"
    assert alerts.venue_full("polymarket") == "Polymarket" and alerts.venue_label("kalshi") == "Kalshi"
    assert alerts.phase_label("inplay") == "in-play" and alerts.phase_label("pre") == "pre-game"
