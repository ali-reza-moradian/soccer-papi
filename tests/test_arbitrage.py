"""Arb-math tests, including the worked example from the build spec."""
from __future__ import annotations

import math

from src.arbitrage import (
    Candidate,
    compute_arb,
    make_signature,
    select_legs,
)


def _cand(oid, name, book, odds, limit=None, group=None, commission=0.0, is_exchange=False):
    return Candidate(
        outcome_id=oid,
        outcome_name=name,
        book=book,
        clone_group=group or book,
        decimal_odds=odds,
        american_odds=None,
        limit=limit,
        commission=commission,
        is_exchange=is_exchange,
    )


def test_worked_example_over_under_2_5():
    """Over 2.5 @ 2.10 (bet365, limit 1500) vs Under 2.5 @ 2.05 (1xbet, limit 5000)."""
    over = _cand(106, "Over", "bet365", 2.10, limit=1500)
    under = _cand(107, "Under", "1xbet", 2.05, limit=5000)

    res = compute_arb([over, under])

    assert res.is_arb
    assert math.isclose(res.arb_sum_S, 0.96399, abs_tol=1e-4)
    assert math.isclose(res.roi_pct, 3.7349, abs_tol=1e-3)
    assert math.isclose(res.t_max, 3036.6, abs_tol=0.2)

    stakes = {leg.book: leg.stake for leg in res.legs}
    assert math.isclose(stakes["bet365"], 1500.0, abs_tol=0.2)   # binding leg uses full limit
    assert math.isclose(stakes["1xbet"], 1536.6, abs_tol=0.2)

    assert math.isclose(res.max_profit, 113.4, abs_tol=0.3)
    assert res.binding_book == "bet365"

    # Every leg returns the same amount = T / S.
    payouts = [leg.stake * leg.eff_odds for leg in res.legs]
    assert math.isclose(payouts[0], payouts[1], rel_tol=1e-3)
    assert math.isclose(payouts[0], res.t_max / res.arb_sum_S, rel_tol=1e-3)


def test_pure_two_book_arb_between_sportsbooks():
    """Any two books can form an arb on their own — here Stake vs Cloudbet, with no exchange
    or crypto prediction market involved (change #3: universal bookmaker pairing)."""
    over = _cand(106, "Over", "stake", 2.10, limit=3000)
    under = _cand(107, "Under", "cloudbet", 2.05, limit=3000)
    chosen = select_legs({106: [over], 107: [under]})
    assert chosen is not None
    res = compute_arb(chosen)
    assert res.is_arb
    assert not res.involves_exchange
    assert {leg.book for leg in res.legs} == {"stake", "cloudbet"}


def test_no_arb_when_S_at_least_one():
    a = _cand(1, "1", "x", 1.90, limit=1000)
    b = _cand(2, "2", "y", 1.90, limit=1000)
    res = compute_arb([a, b])
    assert not res.is_arb
    assert res.roi_decimal < 0


def test_three_way_arb_and_equal_payouts():
    legs = [
        _cand(101, "1", "pinnacle", 3.10, limit=2000),
        _cand(102, "X", "1xbet", 3.60, limit=2000),
        _cand(103, "2", "kalshi", 3.70, limit=2000),
    ]
    res = compute_arb(legs)
    assert res.is_arb
    payouts = [leg.stake * leg.eff_odds for leg in res.legs]
    assert max(payouts) - min(payouts) < 1.0
    # total stake equals T_max
    assert math.isclose(sum(leg.stake for leg in res.legs), res.t_max, rel_tol=2e-3)


def test_commission_reduces_effective_odds():
    # 2% commission on an exchange leg lowers its effective odds.
    c = _cand(1, "Yes", "kalshi", 2.00, limit=1000, commission=0.02, is_exchange=True)
    assert math.isclose(c.eff_odds, 1.98, abs_tol=1e-9)
    other = _cand(2, "No", "pinnacle", 2.10, limit=1000)
    res = compute_arb([c, other])
    # S uses effective odds, not raw.
    expected_S = 1 / 1.98 + 1 / 2.10
    assert math.isclose(res.arb_sum_S, expected_S, abs_tol=1e-9)


def test_kalshi_commission_007_matches_quadratic_fee_at_midprice():
    """Calibration: at the 50c midpoint the flat commission c=0.07 reproduces Kalshi's quadratic taker
    fee. A kalshi leg's effective implied cost 1/eff_odds ~= P + 0.07*P*(1-P) (= 0.5175 at P=0.5) within
    1e-3, so a kalshi leg needs ~1.75% of gross edge just to clear S<1 against a fair 50/50 counter-leg."""
    P = 0.5
    c = _cand(1, "Yes", "kalshi", 1.0 / P, limit=1000, commission=0.07, is_exchange=True)
    eff_implied = 1.0 / c.eff_odds                       # 1/1.93 = 0.51813
    assert math.isclose(eff_implied, P + 0.07 * P * (1 - P), abs_tol=1e-3)   # ~= 0.5175
    # A fair 50c counter-leg (no commission) leaves S = 0.5175.. + 0.5 > 1 -> the fee kills the phantom.
    other = _cand(2, "No", "polymarket", 1.0 / P, limit=1000)
    res = compute_arb([c, other])
    assert not res.is_arb and res.arb_sum_S > 1.0
    assert math.isclose(res.arb_sum_S, eff_implied + 0.5, abs_tol=1e-9)


def test_select_legs_allows_same_account_for_multiple_legs():
    """One account may back more than one leg of the same arb (user bets this way)."""
    outcomes = {
        1: [
            _cand(1, "Over", "stake", 2.20, limit=1000, group="stake-group"),
        ],
        2: [
            # Best Under is on the same operator — allowed, not rejected.
            _cand(2, "Under", "stake-clone", 2.20, limit=1000, group="stake-group"),
        ],
    }
    chosen = select_legs(outcomes)
    assert chosen is not None
    assert [c.outcome_name for c in chosen] == ["Over", "Under"]


def test_select_legs_picks_best_price_per_outcome():
    outcomes = {
        1: [
            _cand(1, "Over", "stake", 2.30, limit=1000, group="g1"),
        ],
        2: [
            _cand(2, "Under", "stake-clone", 2.40, limit=1000, group="g1"),   # same operator, better price
            _cand(2, "Under", "pinnacle", 2.05, limit=1000, group="pinnacle"),  # worse
        ],
    }
    chosen = select_legs(outcomes)
    assert chosen is not None
    by_outcome = {c.outcome_id: c.book for c in chosen}
    assert by_outcome[2] == "stake-clone"  # best price wins; same-operator pairing is fine


def test_select_then_compute_two_legs_on_one_account():
    """User's case: Home on Polymarket, Draw AND Away both on 1xBet -> still a valid arb."""
    outcomes = {
        1: [_cand(1, "1", "polymarket", 2.90, limit=1000, group="polymarket")],
        2: [_cand(2, "X", "1xbet", 3.70, limit=1000, group="1xbet")],
        3: [_cand(3, "2", "1xbet", 3.80, limit=1000, group="1xbet")],
    }
    chosen = select_legs(outcomes)
    assert chosen is not None
    assert [c.book for c in chosen] == ["polymarket", "1xbet", "1xbet"]
    res = compute_arb(chosen)
    assert res.is_arb  # 1/2.9 + 1/3.7 + 1/3.8 < 1
    # Two legs share the 1xbet account; payouts still equalise across all three.
    payouts = [leg.stake * leg.eff_odds for leg in res.legs]
    assert max(payouts) - min(payouts) < 1.0


def test_unknown_limit_marks_low_confidence_and_binds_on_assumed_cap():
    """A leg with NO real limit is capped at the assumed cap and flagged UNVERIFIED — and that
    assumed cap now constrains T_max (the safety fix), so the limit-less leg binds when its cap is
    the tightest. Previously the unknown leg was bottomless and pinnacle bound at $1500."""
    a = _cand(1, "Yes", "polymarket", 2.10, limit=None, is_exchange=True)
    b = _cand(2, "No", "pinnacle", 2.10, limit=1500)
    res = compute_arb([a, b], assumed_unknown_limit=100)
    assert res.low_confidence
    # polymarket's assumed $100 cap (< pinnacle's real $1500) now binds T_max.
    assert res.binding_book == "polymarket"
    assert res.unverified_books == ["polymarket"]
    poly_leg = next(lg for lg in res.legs if lg.book == "polymarket")
    assert poly_leg.unverified and poly_leg.effective_limit == 100
    assert next(lg for lg in res.legs if lg.book == "pinnacle").unverified is False


def test_per_book_assumed_limit_override():
    """assumed_unknown_limit_by_book overrides the default for a specific book."""
    a = _cand(1, "Yes", "1xbet", 2.10, limit=None)
    b = _cand(2, "No", "unibet", 2.10, limit=None)
    res = compute_arb([a, b], assumed_unknown_limit=1000,
                      assumed_unknown_limit_by_book={"1xbet": 2000})
    by_book = {lg.book: lg.effective_limit for lg in res.legs}
    assert by_book == {"1xbet": 2000, "unibet": 1000}
    assert set(res.unverified_books) == {"1xbet", "unibet"}


def test_bankroll_cap_scales_stakes_and_profit():
    """cap_total_investment scales an over-large arb down to the bankroll; ROI/S unchanged."""
    from src.arbitrage import cap_total_investment
    a = _cand(1, "Over", "pinnacle", 2.10, limit=100000)
    b = _cand(2, "Under", "1xbet", 2.05, limit=100000)
    res = compute_arb([a, b])
    assert res.t_max > 30000                      # natural T_max is huge
    roi_before, s_before = res.roi_decimal, res.arb_sum_S
    capped = cap_total_investment(res, 30000)
    assert math.isclose(capped.t_max, 30000, abs_tol=1.0)
    assert math.isclose(sum(lg.stake for lg in capped.legs), 30000, abs_tol=1.0)
    assert math.isclose(capped.roi_decimal, roi_before) and capped.arb_sum_S == s_before
    # Already-small arbs are left untouched.
    small = compute_arb([_cand(1, "Over", "pinnacle", 2.10, limit=500),
                         _cand(2, "Under", "1xbet", 2.05, limit=500)])
    t_before = small.t_max
    assert cap_total_investment(small, 30000).t_max == t_before


def test_tiny_known_limit_marks_low_confidence():
    """A known-but-thin limit (< floor) on an exchange leg flags low_confidence (kept, not dropped)."""
    a = _cand(1, "Yes", "polymarket", 2.10, limit=4, is_exchange=True)   # only $4 available
    b = _cand(2, "No", "kalshi", 2.10, limit=2000, is_exchange=True)
    res = compute_arb([a, b], low_confidence_limit_floor=10.0)
    assert res.is_arb
    assert res.low_confidence
    # A comfortable limit on both legs does NOT flag low_confidence.
    fat = compute_arb([_cand(1, "Yes", "polymarket", 2.10, limit=500, is_exchange=True),
                       _cand(2, "No", "kalshi", 2.10, limit=500, is_exchange=True)],
                      low_confidence_limit_floor=10.0)
    assert not fat.low_confidence


def test_exchange_vs_exchange_arb_is_valid():
    """Kalshi <-> Polymarket (exchange vs exchange) is a legitimate arb — nothing filters it out."""
    a = _cand(1, "Yes", "kalshi", 2.10, limit=1000, is_exchange=True)
    b = _cand(2, "No", "polymarket", 2.05, limit=1000, is_exchange=True)
    chosen = select_legs({1: [a], 2: [b]})
    assert chosen is not None
    res = compute_arb(chosen)
    assert res.is_arb
    assert res.involves_exchange
    assert {leg.book for leg in res.legs} == {"kalshi", "polymarket"}


def test_signature_is_stable_and_order_independent():
    over = _cand(106, "Over", "bet365", 2.10, limit=1500)
    under = _cand(107, "Under", "1xbet", 2.05, limit=5000)
    r1 = compute_arb([over, under])
    r2 = compute_arb([under, over])
    s1 = make_signature("fx1", 106, 2.5, r1.legs)
    s2 = make_signature("fx1", 106, 2.5, r2.legs)
    assert s1 == s2


def test_all_poly_3way_net_negative_from_taker_fee():
    """A 3-way all-Polymarket moneyline (S=0.989, GROSS-positive) is NET-NEGATIVE once the sports taker
    fee (0.05*min(p,1-p) per leg) is applied: net_roi ~ -3.8% (gross-positive / net-negative)."""
    legs = [_cand(1, "H", "polymarket", 1 / 0.36, limit=100000, is_exchange=True),
            _cand(2, "X", "polymarket", 1 / 0.316, limit=100000, is_exchange=True),
            _cand(3, "A", "polymarket", 1 / 0.3125, limit=100000, is_exchange=True)]
    res = compute_arb(legs)
    assert math.isclose(res.arb_sum_S, 0.9885, abs_tol=1e-3) and res.roi_pct > 0   # gross sub-1
    assert math.isclose(res.net_roi_pct, -3.84, abs_tol=0.05)                      # NET ~ -3.8%


def test_kalshi_poly_exact_fees_not_commission():
    """Kalshi is no longer in the commission map — its net fee is the exact 0.07*p*(1-p); Polymarket is
    0.05*min(p,1-p). A cross-venue btts K0.47 + P0.51 (S=0.98, gross-positive) -> net ~ -2.2%."""
    res = compute_arb([_cand(1, "Yes", "kalshi", 1 / 0.47, limit=100000, is_exchange=True),
                       _cand(2, "No", "polymarket", 1 / 0.51, limit=100000, is_exchange=True)])
    assert math.isclose(res.arb_sum_S, 0.98, abs_tol=1e-6) and res.roi_pct > 0
    assert math.isclose(res.net_roi_pct, -2.24, abs_tol=0.05)


def test_poly_fee_rate_zero_leaves_gross_untouched():
    """poly_fee_rate=0 (fees disabled) -> net == gross (the pre-fee behavior)."""
    legs = [_cand(1, "H", "polymarket", 1 / 0.36, limit=100000, is_exchange=True),
            _cand(2, "X", "polymarket", 1 / 0.316, limit=100000, is_exchange=True),
            _cand(3, "A", "polymarket", 1 / 0.3125, limit=100000, is_exchange=True)]
    res = compute_arb(legs, poly_fee_rate=0.0)
    assert res.net_fee_rate == 0.0
    assert math.isclose(res.net_roi_decimal, res.roi_decimal, abs_tol=1e-12)
