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


def test_unknown_limit_marks_low_confidence():
    a = _cand(1, "Yes", "polymarket", 2.10, limit=None, is_exchange=True)
    b = _cand(2, "No", "pinnacle", 2.10, limit=1500)
    res = compute_arb([a, b], unknown_limit_fallback=100)
    assert res.low_confidence
    # T_max derived from the only known limit (pinnacle leg).
    assert res.binding_book == "pinnacle"


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
