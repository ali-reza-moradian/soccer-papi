"""THE HEDGE BUYS SHARES, NOT PADDED DOLLARS (src/executor/poly_exec.py).

A Polymarket market BUY is denominated in USDC: the venue spends the WHOLE maker amount and returns
``amount / fill_price`` shares. Price improvement therefore comes back as EXTRA SHARES, never as a
refund — so sending ``floor(target) x (best_ask + 2 ticks)`` worth of dollars handed the entire 2-tick
marketability pad back as unhedged shares on every single hedge:

    overshoot = 2 ticks / fill price      -> +2.1% at 95c, +5.6% at 36c, +6.9% at 29c, +40% at 5c

Five of five rest-kalshi hedges overshot (docs/BOOKS_VS_BANKS_20260804.md, T6). 26AUG04JEJBMU O/U5.5
rode 6.5722 shares naked and lost $2.27 — more than the pair it was hedging made (+$1.80).

The fix keeps the pad on the LIMIT (where it buys marketability) and computes the SPEND from the price
we expect to clear at. These tests hold the two properties that matters: the order is always venue-legal,
and it can never buy more than the fill it is hedging.
"""
from __future__ import annotations

import math
import random
from decimal import Decimal

import pytest

from src.executor.poly_exec import (MARKET_BUY_AMOUNT_DP, PolyExec, PolyExecError, market_buy_price,
                                    market_buy_spend)


def _dp(x) -> int:
    return abs(Decimal(str(x)).as_tuple().exponent)


# --------------------------------------------------------------------------- #
# the property: venue-legal, and never over target                              #
# --------------------------------------------------------------------------- #
def test_the_spend_is_always_venue_legal_and_never_over_buys():
    """Random price x size, 20k draws. Two invariants, both absolute:
      * the USDC amount carries at most 2 decimals (the rule that 400'd the CSKA hedge), and
      * at the clearing price it pays for, it buys AT MOST the shares we are hedging."""
    rng = random.Random(20260804)
    for _ in range(20_000):
        tick = rng.choice((0.1, 0.01, 0.001, 0.0001))
        px = round(rng.randrange(1, int(1 / tick)) * tick, 6)
        if not (0.0 < px < 1.0):
            continue
        target = rng.choice((rng.uniform(1.0, 5.0), rng.uniform(5.0, 200.0),
                             rng.uniform(200.0, 900.0), float(rng.randrange(1, 500))))
        usd, shares = market_buy_spend(target, px)
        if usd == 0.0:
            assert shares == 0.0
            continue
        assert _dp(usd) <= MARKET_BUY_AMOUNT_DP, (usd, px, target)
        assert shares <= target + 1e-9, f"OVER-BUY: {shares} sh for a {target} sh fill at {px}"
        assert abs(usd / px - shares) < 1e-6


def test_the_shortfall_is_only_the_venue_s_own_cent_of_precision():
    """Undershoot must be forced by the amount rules and nothing else: the most we can leave behind is
    the sub-cent remainder, i.e. 0.01/price of a share. The executor's HEDGE_SHARE_TOL is 0.5, so this
    books as LOCKED instead of fabricating a partial hedge on every fill."""
    from src.genz.maker_rt.pregame_exec import HEDGE_SHARE_TOL
    for px in (0.03, 0.05, 0.10, 0.29, 0.36, 0.50, 0.76, 0.9567, 0.99):
        for target in (1.0, 5.0, 14.91, 44.74, 76.0, 135.65, 353.0, 894.37):
            _usd, shares = market_buy_spend(target, px)
            shortfall = target - shares
            assert 0.0 <= shortfall <= 0.01 / px + 1e-9, (px, target, shortfall)
            if px >= 0.03:
                assert shortfall < HEDGE_SHARE_TOL, (px, target, shortfall)


def test_a_target_the_venue_cannot_price_buys_nothing():
    assert market_buy_spend(0.0, 0.5) == (0.0, 0.0)
    assert market_buy_spend(-5, 0.5) == (0.0, 0.0)
    assert market_buy_spend(10, 0.0) == (0.0, 0.0)
    assert market_buy_spend(10, 1.5) == (0.0, 0.0)
    assert market_buy_spend("nonsense", 0.5) == (0.0, 0.0)
    assert market_buy_spend(0.4, 0.01) == (0.0, 0.0), "0.4 sh at 1c is under a cent — not an order"


def test_the_limit_price_still_rounds_up_and_stays_in_range():
    """The pad is a CEILING and must stay one: execution happens at the resting asks, so rounding it
    down would make the order non-marketable on a 0.001-tick book."""
    assert market_buy_price(0.7615) == 0.77
    assert market_buy_price(0.76) == 0.76           # a gate-approved cap is already a whole cent
    assert market_buy_price(0.9999) == 0.99
    assert market_buy_price(0.0001) == 0.01


# --------------------------------------------------------------------------- #
# the replay: the two JEJBMU hedges, old sizing vs new                          #
# --------------------------------------------------------------------------- #
#: (Kalshi rest fill sh, padded limit, price it actually cleared at, shares the venue actually gave us)
JEJBMU = [
    (14.91, 0.31, 0.29, 14.965516),
    (44.74, 0.31, 0.29, 47.034481),
    (76.00, 0.38, 0.36, 80.222221),
    (353.0, 0.96, 0.9567, 354.203),
]


def test_the_old_sizing_reproduces_the_venue_shares_exactly():
    """This is what makes the replay evidence rather than assertion: the OLD arithmetic —
    ``floor(target) x limit / fill`` — reproduces what the venue actually handed us, to six decimals."""
    for target, limit, fill, actual in JEJBMU:
        old = math.floor(target) * limit / fill
        assert abs(old - actual) < 0.02, (target, old, actual)


def test_the_new_sizing_removes_the_excess_on_both_jejbmu_markets():
    """O/U5.5 (the three -6 hedges) left 6.5722 sh naked and lost $2.27; O/U1.5 (the 353 hedge) left
    1.203. Under the new sizing both go to ~0, and every one of them UNDER-shoots, never over."""
    ou55 = [r for r in JEJBMU if r[0] in (14.91, 44.74, 76.00)]
    old_excess = sum(math.floor(t) * lim / f - t for t, lim, f, _a in ou55)
    new_excess = sum(market_buy_spend(t, f)[1] - t for t, _lim, f, _a in ou55)
    assert 6.5 < old_excess < 6.6, old_excess          # the 6.5722 sh that rode unhedged
    assert -0.11 <= new_excess <= 0.0, new_excess      # now a sub-cent shortfall, on the safe side

    t, lim, f, _a = JEJBMU[3]                           # O/U1.5, the 353-share hedge
    assert 1.2 < (math.floor(t) * lim / f - t) < 1.3
    assert -0.02 <= market_buy_spend(t, f)[1] - t <= 0.0


def test_every_recent_hedge_would_have_been_within_a_hundredth_of_its_target():
    for target, _limit, fill, _actual in JEJBMU:
        _usd, shares = market_buy_spend(target, fill)
        assert 0.0 <= target - shares < 0.04, (target, shares)


# --------------------------------------------------------------------------- #
# the order that actually gets signed                                           #
# --------------------------------------------------------------------------- #
class _Clob:
    """Records what would be signed, rounding the amount the way the real client does."""

    def __init__(self, tick="0.01"):
        self.tick, self.orders, self.posted = tick, [], []

    def get_tick_size(self, token):
        return self.tick

    def get_neg_risk(self, token):
        return False

    def create_market_order(self, args, options):
        self.orders.append(args)
        maker = math.floor(args.amount * 100) / 100
        return {"maker": maker, "price": args.price, "taker": round(maker / args.price, 4)}

    def post_order(self, signed, order_type):
        self.posted.append(signed)
        return {"status": "matched", "makingAmount": signed["maker"], "takingAmount": signed["taker"],
                "price": signed["price"]}


def test_the_signed_order_carries_the_walked_spend_and_the_padded_limit():
    """Both facts on one order: the LIMIT keeps the 2-tick pad (marketability), the AMOUNT is priced off
    the walk (no overshoot). Conflating them is the whole bug."""
    clob = _Clob()
    PolyExec(client=clob).place_market_buy("TOK", 44.74, price=0.31, expected_price=0.29)
    args = clob.orders[0]
    assert args.price == 0.31, "the pad stays on the limit"
    assert args.amount == round(math.floor(44.74 * 0.29 * 100) / 100, 2) == 12.97
    assert args.side == "BUY"
    assert 12.97 / 0.29 <= 44.74 + 1e-9


def test_it_refuses_to_sign_an_order_that_would_over_buy(monkeypatch):
    """Fail CLOSED, and check it BEFORE signing. An over-hedge is naked directional risk booked as if it
    were a lock — the exact thing this change exists to end — so if the sizer ever regresses, the order
    must not reach the venue at all."""
    from src.executor import poly_exec as pe
    monkeypatch.setattr(pe, "market_buy_spend", lambda target, px: (100.0, target + 1.0))
    clob = _Clob()
    with pytest.raises(PolyExecError, match="over-buy"):
        PolyExec(client=clob).place_market_buy("TOK", 10.0, price=0.31, expected_price=0.29)
    assert clob.orders == [] and clob.posted == [], "it must refuse BEFORE signing"


def test_sub_share_dust_still_never_reaches_the_venue():
    """Unchanged: the existing dust path owns anything the venue cannot price."""
    clob = _Clob()
    res = PolyExec(client=clob).place_market_buy("TOK", 0.49, price=0.76, expected_price=0.74)
    assert res["shares"] == 0.0 and res["order_id"] is None
    assert clob.posted == [] and clob.orders == []


def test_the_hedge_calls_a_sub_cent_shortfall_LOCKED_not_partial():
    """LIVE PROOF, 2026-08-04: a real $2.00 market BUY at 75c delivered 2.666664 sh against a 2.6667 sh
    target. At the old 1e-9 tolerance that is a MISS by 0.000036 of a share, and every hedge would fall
    through to the unwind-or-verify path instead of booking LOCKED. The tolerance has to be shaped like
    the venue's own amount precision."""
    from src.executor.poly_exec import market_buy_shortfall
    from src.genz.maker_rt.hedge import LiveHedger

    class _Poly:
        poly_rate = 0.05

        def place_market_buy(self, token, size, **kw):
            return {"status": "partial", "shares": 2.666664, "usd": 2.0, "avg_price": 0.75,
                    "order_id": "0xabc"}

    res = LiveHedger(poly_client=_Poly(), log=None).hedge_poly(
        {"price": 0.23, "size": 2.6667},
        {"token": "T", "best_ask": 0.75, "max_price": None, "walk_price": 0.75})
    assert res.status == "locked", "a sub-cent shortfall is not a partial hedge"
    assert res.freeze_market is False
    assert abs(market_buy_shortfall(0.75) - 0.01 / 0.75) < 1e-12


def test_a_REAL_shortfall_is_still_a_partial_hedge():
    """The tolerance is clamped to half a share, so a hedge that genuinely missed still freezes and
    unwinds. A tolerance that grows without bound is how a naked position books as a lock."""
    from src.genz.maker_rt.hedge import LiveHedger

    class _Poly:
        poly_rate = 0.05

        def place_market_buy(self, token, size, **kw):
            return {"status": "partial", "shares": 30.0, "usd": 9.0, "avg_price": 0.30,
                    "order_id": "0xabc"}

    res = LiveHedger(poly_client=_Poly(), log=None).hedge_poly(
        {"price": 0.65, "size": 44.74},
        {"token": "T", "best_ask": 0.30, "max_price": None, "walk_price": 0.30})
    assert res.status == "partial" and res.freeze_market is True


def test_the_tolerance_can_never_exceed_half_a_share():
    """At a 1c hedge price one cent buys a whole share, so the raw bound is 1.0. Clamped, always."""
    from src.executor.poly_exec import market_buy_shortfall
    assert market_buy_shortfall(0.01) == pytest.approx(1.0)
    assert min(0.5, market_buy_shortfall(0.01)) == 0.5
    assert market_buy_shortfall(0.0) == 0.0


def test_a_caller_with_no_walk_falls_back_to_the_book_not_to_the_pad():
    """The walk is the good estimate; the best ask is the acceptable one. Sizing off the LIMIT is the
    old bug, so it is the last resort and it says so out loud."""
    class _WithBook(_Clob):
        def get_order_book(self, token):
            raise AssertionError("unused")

    clob = _WithBook()
    ex = PolyExec(client=clob)
    ex.get_orderbook = lambda tok: {"asks": [(0.29, 500.0)]}
    ex.place_market_buy("TOK", 44.74, price=0.31)          # no expected_price -> book's best ask
    assert clob.orders[0].amount == 12.97, "sized off 0.29, not off the 0.31 pad"


# --------------------------------------------------------------------------- #
# PARTIAL FAK fills: the share count comes from VENUE CASH, at every ratio       #
# --------------------------------------------------------------------------- #
class _PartialClob(_Clob):
    """A CLOB that matches only ``ratio`` of the amount — a FAK that ran out of book.

    A partial market BUY reports the amounts it ACTUALLY moved: ``makingAmount`` is the USDC that left
    (less than the order's), ``takingAmount`` the shares that arrived. Anything that reads the REQUEST
    instead — or reads a BUY's makingAmount as shares — mis-states a real position, which on 2026-07-23
    (TBTOR) unwound a phantom remainder and on 2026-08-05 (KLAMCI) decided how much to sell.
    """

    def __init__(self, ratio, fill_price=None, tick="0.01"):
        super().__init__(tick=tick)
        self.ratio, self.fill_price = ratio, fill_price

    def post_order(self, signed, order_type):
        self.posted.append(signed)
        px = self.fill_price or signed["price"]
        usd = round(signed["maker"] * self.ratio, 6)
        if usd <= 0:
            return {"status": "canceled", "makingAmount": 0, "takingAmount": 0}
        return {"status": "matched", "makingAmount": usd, "takingAmount": round(usd / px, 6),
                "price": px}


@pytest.mark.parametrize("ratio", [0.0, 0.3, 0.7, 1.0])
def test_a_partial_fak_reports_the_shares_the_venue_actually_handed_over(ratio):
    """The count is TAKINGAMOUNT, whatever fraction filled — never the request, never the dollar leg."""
    clob = _PartialClob(ratio, fill_price=0.68)
    res = PolyExec(client=clob).place_market_buy("TOK", 115.0, price=0.70, expected_price=0.68)
    spent = round(clob.orders[0].amount * ratio, 6)
    assert res["shares"] == pytest.approx(spent / 0.68 if spent else 0.0)
    assert res["avg_price"] == pytest.approx(0.68) if ratio else True
    assert res["status"] == ("filled" if ratio == 1.0 else ("partial" if ratio else "none"))
    if ratio:
        assert res["cash_debit"] == pytest.approx(spent), "cost is booked from the CASH the venue moved"


@pytest.mark.parametrize("ratio", [0.3, 0.7])
def test_a_partial_fak_is_never_counted_in_dollars(ratio):
    """A BUY's makingAmount is USDC. Reading it as shares under-reports a 68c fill by ~32% — enough to
    make a real hedge look like a miss and send it down the unwind path."""
    clob = _PartialClob(ratio, fill_price=0.68)
    res = PolyExec(client=clob).place_market_buy("TOK", 115.0, price=0.70, expected_price=0.68)
    dollars = round(clob.orders[0].amount * ratio, 6)
    assert res["shares"] > dollars, "shares at a sub-$1 price always exceed the dollars paid"


def test_the_klamci_hedge_response_reads_as_eighty_shares():
    """VENUE TRUTH, 2026-08-05: a 115-share hedge FAK moved $54.4844 for 80 Under shares. That count is
    what decides the naked remainder, and it was right on the day — the 80 shares were lost by the
    REGISTRY, not by the parser. Pinned so the two failures never get conflated again."""
    raw = {"status": "matched", "makingAmount": "54.4844", "takingAmount": "80", "price": "0.67"}
    res = PolyExec(client=_Clob())._normalize(raw, price=0.70, requested_shares=115.0, side="BUY")
    assert res["shares"] == pytest.approx(80.0)
    assert res["avg_price"] == pytest.approx(54.4844 / 80.0)
    assert res["avg_price_source"] == "venue_cash", "the executed price is the amounts' RATIO"
    assert res["cash_debit"] == pytest.approx(54.4844)
    assert res["status"] == "partial"
