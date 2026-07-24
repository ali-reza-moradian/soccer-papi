"""Fee-honest hedging: the ACTUAL taker fee is folded into locked_net (at fill) and the settled cost
basis (both legs), so the auto-reported net is not ~1 fee optimistic — comparable to the whole edge at
these sizes. The rest leg is a MAKER order (fee 0); the hedge is a TAKER lift whose fee is real."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.executor.fees_sizing import kalshi_fee_usd, poly_fee_usd
from src.genz.maker_rt.hedge import LiveHedger, kalshi_actual_fee
from src.genz.maker_rt.pregame_exec import PregameLiveExecutor

from .test_maker_rt_pregame import _Hedger, _OrderClient, _Store, _cand, _dec, _exec


# --------------------------------------------------------------------------- #
# 1. kalshi_actual_fee: exact ceil-to-cent, prefers a sane venue-reported value #
# --------------------------------------------------------------------------- #
def test_kalshi_actual_fee_is_the_official_ceil_to_cent_formula():
    # 5 @ 0.41: 0.07*5*0.41*0.59 = 0.084665 -> ceil to cent = $0.09.
    assert kalshi_actual_fee({}, 5, 0.41) == pytest.approx(0.09)
    assert kalshi_actual_fee({}, 5, 0.41) == pytest.approx(kalshi_fee_usd(5, 0.41))


def test_kalshi_actual_fee_prefers_venue_value_when_it_agrees():
    # A reported dollar fee close to the formula is used verbatim (the venue is the source of truth).
    assert kalshi_actual_fee({"average_fee_paid": 0.08}, 5, 0.41) == pytest.approx(0.08)
    # A cents-scaled report (9 == $0.09) is accepted after /100 (matches the formula).
    assert kalshi_actual_fee({"average_fee_paid": 9}, 5, 0.41) == pytest.approx(0.09)
    # A wildly-off report (a unit slip) is REJECTED -> fall back to the exact formula.
    assert kalshi_actual_fee({"fee": 500}, 5, 0.41) == pytest.approx(kalshi_fee_usd(5, 0.41))


# --------------------------------------------------------------------------- #
# 2. the live hedger exposes the ACTUAL fee and prices pnl with it               #
# --------------------------------------------------------------------------- #
class _FakeKalshi:
    def __init__(self, fill_count, avg): self._r = {"fill_count": fill_count, "avg_price": avg}
    def place_order(self, *a, **k): return self._r


class _FakePoly:
    def __init__(self, shares, avg): self._r = {"shares": shares, "avg_price": avg}
    def place_market_buy(self, *a, **k): return self._r


def test_kalshi_hedge_reports_actual_fee_and_feehonest_pnl():
    h = LiveHedger(kalshi_client=_FakeKalshi(5, 0.41))
    r = h.hedge({"price": 0.56, "size": 5}, {"ticker": "KX-1", "side": "yes", "best_ask": 0.41})
    assert r.status == "locked" and r.hedge_fee == pytest.approx(0.09)
    # pnl = (1 - 0.56 - 0.41)*5 - 0.09 = 0.15 - 0.09 = 0.06 (fee-honest, NOT the ~0.065 modeled estimate)
    assert r.locked_pnl == pytest.approx(0.06, abs=1e-6)


def test_poly_hedge_reports_actual_taker_fee():
    h = LiveHedger(poly_client=_FakePoly(5, 0.41), poly_rate=0.05)
    r = h.hedge_poly({"price": 0.56, "size": 5}, {"token": "T", "best_ask": 0.41})
    assert r.status == "locked" and r.hedge_fee == pytest.approx(poly_fee_usd(5, 0.41, 0.05))


# --------------------------------------------------------------------------- #
# 3. _actual_locked_net (pure): actual hedge price + fee, not the quoted ask     #
# --------------------------------------------------------------------------- #
def test_actual_locked_net_uses_actual_price_and_fee():
    # 1 - 0.56 - 0.41 - 0.09/5 = 0.03 - 0.018 = 0.012
    assert PregameLiveExecutor._actual_locked_net(0.56, 0.41, 0.09, 5, None) == pytest.approx(0.012)
    # no actual price -> fall back to the estimate
    assert PregameLiveExecutor._actual_locked_net(0.56, None, 0.09, 5, 0.02) == pytest.approx(0.02)


# --------------------------------------------------------------------------- #
# 4. end-to-end: the fee lands in the recorded locked_net AND the cost basis     #
# --------------------------------------------------------------------------- #
def test_fee_flows_into_locked_net_and_cost_basis(tmp_path):
    oc = _OrderClient()
    hedger = _Hedger(SimpleNamespace(status="locked", hedged_shares=5, hedge_avg_price=0.41,
                                     hedge_fee=0.09, locked_pnl=0.06, unwind_cost=None))
    ex, _ = _exec(tmp_path, order_client=oc, hedger=hedger)
    ex.caps.quote_usd_max = 3.0                             # pin size to 5 (floor(3.0/0.56)=5)
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.41)
    ex.place_or_reprice(_cand(), _dec(price=0.56, hedge_ask=0.41), None, store, now=None, now_ts=1.0)
    oid = oc.rests[0]["oid"]
    ex.on_order_update({"order_id": oid, "size_matched": 5, "price": 0.56}, store, None, 2.0)
    row = [r for r in ex.state.rows if r.get("event") == "hedge_locked"][0]
    # locked_net (recorded as %): 1 - 0.56 - 0.41 - 0.09/5 = 0.012 -> 1.2%
    assert row["locked_net"] == pytest.approx(1.2, abs=1e-3)
    legs = list(ex._market_legs.values())[0]
    # rest leg (poly, maker fee 0) = 5*0.56 = 2.80 ; hedge leg (kalshi taker) = 5*0.41 + 0.09 = 2.14
    assert legs["poly"]["cost"] == pytest.approx(2.80)
    assert legs["kalshi"]["cost"] == pytest.approx(2.14)
