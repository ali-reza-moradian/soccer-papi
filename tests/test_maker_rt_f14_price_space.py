"""Regression suite for the 2026-07-29 F14 PRICE-SIDE INVERSION + the booking invariants it needed.

PRODUCTION EVIDENCE (Kalshi /portfolio/fills + /portfolio/settlements + the order responses, all
re-read from the venue on 2026-07-30; data/ops/maker_rt.log times UTC):

  01:21:06  hedge LOCKED KXBRASILEIROBTOTAL-26JUL28FORBOT-4 x353 @ 0.0500 -> pnl $320.05
            VENUE: no_price_dollars 0.9500, taker_fill_cost_dollars 335.35, fee 1.1738.  REAL: +$2.36
  07:14:27  hedge LOCKED KXCLUBFTOTAL-26JUL29TOTSYD-5      x212 @ 0.3561 -> pnl $63.14
            VENUE: swept 0.64/0.65, taker_fill_cost_dollars 136.49, fee 3.4030.          REAL: +$2.15
  09:00:45  hedge LOCKED KXCLUBFTOTAL-26JUL29CERBVB-3       x92 @ 0.7700 -> pnl $-49.91
            VENUE: no_price_dollars 0.2300, taker_fill_cost_dollars  21.16, fee 1.1406.  REAL: -$0.22

Every one of those "prices" is the YES-space complement of what a NO share actually cost. The v2
create-order response reports an average fill price in YES-space and the read path never converted it
back, so a NO-side hedge booked at ``1 - real``. The SIGN of the resulting error is decided by which
side of 50c the hedge sat on, which is why the same bug produced a phantom +$320 windfall AND a
phantom -$50 disaster on the same day — and why the +$320 was the more dangerous of the two: it
re-based ``pnl_today`` to +$330 of fiction, so the -$50 daily-loss rail needed a REAL -$380 to trip.

Four independent fixes are pinned here:
  1. the normalizer books from VENUE CASH (taker_fill_cost / fill_count), which is side-correct by
     construction and exact across a multi-level sweep;
  2. the legacy YES-space field, when that is all there is, is CONVERTED using the order's own side;
  3. the executed hedge cap is solved at break-even, making a >$1.00/share pair unfillable;
  4. booking-time invariants REFUSE an impossible pair or an above-ceiling edge and quarantine it,
     because every rail downstream of the books trusts the books.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.executor import poly_exec
from src.executor.kalshi_exec import KalshiExec
from src.genz.maker_rt.hedge import LiveHedger, hedge_taker_fee, locked_net
from src.genz.maker_rt.pregame_exec import (HEDGE_EXECUTION_FLOOR, PAIR_SUM_TOL,
                                            PregameLiveExecutor)

_DT = datetime(2026, 7, 29, 9, 0, 45, tzinfo=timezone.utc)

#: The three real v2 order responses, verbatim from GET /portfolio/orders/{id} on 2026-07-30, with the
#: rest-leg price each was hedging and the venue-truth realized pnl of the pair.
_INCIDENTS = {
    # name:      (order response fields,                                    rest_px, booked_px, real_pnl)
    "FORBOT": ({"fill_count_fp": "353.00", "no_price_dollars": "0.9600", "yes_price_dollars": "0.0400",
                "taker_fill_cost_dollars": "335.350000", "maker_fill_cost_dollars": "0.000000",
                "taker_fees_dollars": "1.173800", "maker_fees_dollars": "0.000000"}, 0.04, 0.05, 2.3562),
    "TOTSYD": ({"fill_count_fp": "212.00", "no_price_dollars": "0.6500", "yes_price_dollars": "0.3500",
                "taker_fill_cost_dollars": "136.490000", "maker_fill_cost_dollars": "0.000000",
                "taker_fees_dollars": "3.403000", "maker_fees_dollars": "0.000000"}, 0.33, 0.3561, 2.1470),
    "CERBVB": ({"fill_count_fp": "92.00", "no_price_dollars": "0.2300", "yes_price_dollars": "0.7700",
                "taker_fill_cost_dollars": "21.160000", "maker_fill_cost_dollars": "0.000000",
                "taker_fees_dollars": "1.140600", "maker_fees_dollars": "0.000000"}, 0.76, 0.77, -0.2206),
}


def _norm(fields: dict, side: str = "NO"):
    k = KalshiExec(api_key_id="x", signer=lambda m: "x")
    n = int(float(fields.get("fill_count_fp") or fields.get("fill_count") or 0))
    return k._normalize_order_response({"order": fields}, n, side=side)


# --------------------------------------------------------------------------- #
# 1. the normalizer returns the OUTCOME's own price, from venue cash            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(_INCIDENTS))
def test_no_side_price_is_decoded_out_of_yes_space(name):
    """Each incident booked the YES-space complement. The normalizer must return the NO price."""
    fields, _rest, booked, _pnl = _INCIDENTS[name]
    res = _norm(fields)
    real = float(fields["taker_fill_cost_dollars"]) / float(fields["fill_count_fp"])
    assert res["avg_price"] == pytest.approx(real, abs=1e-6)
    assert res["avg_price_source"] == "venue_cash"
    # ...and it is emphatically NOT what the incident booked (which was 1 - real).
    assert abs(res["avg_price"] - booked) > 0.01
    assert res["avg_price"] == pytest.approx(1.0 - booked, abs=0.011)


@pytest.mark.parametrize("name", sorted(_INCIDENTS))
def test_booked_pnl_now_reproduces_venue_truth_to_the_cent(name):
    """The whole point: fee-honest locked pnl from the fixed read equals the venue's realized number."""
    fields, rest_px, _booked, real_pnl = _INCIDENTS[name]
    res = _norm(fields)
    n, px, fee = res["fill_count"], res["avg_price"], res["fee"]
    assert PregameLiveExecutor._actual_locked_net(rest_px, px, fee, n, None) * n == \
        pytest.approx(real_pnl, abs=0.01)


def test_multi_level_sweep_price_comes_from_cash_not_any_single_price_field():
    """Tottenham's 212 NO swept 0.64 AND 0.65. No single price field can express that — only the cash
    ratio (136.49/212 = 0.6438) does, which is exactly why cash outranks every price field."""
    res = _norm(_INCIDENTS["TOTSYD"][0])
    assert res["avg_price"] == pytest.approx(0.643821, abs=1e-6)
    assert res["avg_price"] != pytest.approx(0.65)     # not the order's limit
    assert res["avg_price"] != pytest.approx(0.64)     # not the deepest level either


def test_legacy_yes_space_average_is_converted_by_side():
    """v1/mocks report only ``average_fill_price``, in YES-space cents. With no cash to fall back on,
    the SIDE is what carries the space — this is the un-converted read that caused the incident."""
    legacy = {"fill_count": 92, "average_fill_price": 77}
    assert _norm(legacy, side="NO")["avg_price"] == pytest.approx(0.23)
    assert _norm(legacy, side="YES")["avg_price"] == pytest.approx(0.77)
    assert _norm(legacy, side="NO")["avg_price_source"] == "legacy_yes_space"
    # side unknown -> unchanged legacy behaviour, never a silent guess
    assert _norm(legacy, side=None)["avg_price"] == pytest.approx(0.77)


def test_one_cent_fill_is_a_cent_not_a_dollar():
    """N31: a contract price lives strictly inside (0,1), so a bare 1.0 can only be ONE CENT. Reading it
    as $1.00 booked a phantom gain on exactly the distressed 0.01-limit unwind sells."""
    assert _norm({"fill_count": 1, "average_fill_price": 1}, side="YES")["avg_price"] == pytest.approx(0.01)


def test_venue_cash_and_fee_are_exposed_for_cost_basis():
    res = _norm(_INCIDENTS["CERBVB"][0])
    assert res["cash_debit"] == pytest.approx(21.16)
    assert res["fee"] == pytest.approx(1.1406)


# --------------------------------------------------------------------------- #
# 2. the Poly leg books the cash ratio, not the limit we sent                   #
# --------------------------------------------------------------------------- #
def test_poly_hedge_books_the_executed_price_not_the_two_ticks_through_limit():
    """The six 2026-07-29 rest-kalshi hedges each booked 2c dearer than they filled, because a
    marketable buy is deliberately sent through the ask and the limit was what got booked. Venue truth
    (data-api /trades): the 04:05Z hedge filled 118.40 sh for $41.44 = 0.35, booked 0.37."""
    p = poly_exec.PolyExec(client=object())
    raw = {"success": True, "status": "matched", "makingAmount": "41.44", "takingAmount": "118.40"}
    res = p._normalize(raw, price=0.37, requested_shares=118, side="BUY")
    assert res["avg_price"] == pytest.approx(0.35, abs=1e-9)
    assert res["avg_price_source"] == "venue_cash"
    assert res["cash_debit"] == pytest.approx(41.44)
    # the pair the venue actually gave us was PROFITABLE, not the booked -0.85% loss
    assert locked_net(0.62, res["avg_price"]) == pytest.approx(0.03, abs=1e-9)


# --------------------------------------------------------------------------- #
# 3. a >$1.00/share pair is UNFILLABLE, not merely declined                     #
# --------------------------------------------------------------------------- #
def test_cerezo_booked_pair_is_structurally_unreachable():
    """CERBVB as BOOKED: rest 0.76 + hedge 0.77 = $1.53/share. The cap sent to Kalshi for a 0.76 rest
    leg is far below 0.77, so no such fill can occur — the venue would return nothing and the caller
    would run its verified unwind."""
    cap = PregameLiveExecutor._hedge_price_cap(0.76, "kalshi", 0.05)
    assert cap < 0.77, f"cap {cap} would permit the $1.53/share pair"
    assert 0.76 + cap <= 1.0 + 1e-9


def test_tottenham_fee_inclusive_pair_cannot_exceed_a_dollar():
    """TOTSYD as BOOKED: 0.62 + 0.37 sums to 0.99 and so slipped the old dollar-pair rule — but the
    Poly taker fee pushes it to $1.0085. The cap is solved at FEE-INCLUSIVE break-even, so 0.37 is
    outside the limit we send."""
    cap = PregameLiveExecutor._hedge_price_cap(0.62, "polymarket", 0.05)
    assert cap < 0.37, f"cap {cap} would permit the $1.0085/share fee-inclusive pair"
    assert 0.62 + 0.37 + hedge_taker_fee("polymarket", 0.37, 0.05) > 1.0    # the pair it must exclude
    assert 0.62 + cap + hedge_taker_fee("polymarket", cap, 0.05) <= 1.0 + 1e-9


@pytest.mark.parametrize("rest,venue", [(0.04, "kalshi"), (0.33, "kalshi"), (0.76, "kalshi"),
                                        (0.62, "polymarket"), (0.94, "polymarket"), (0.97, "polymarket")])
def test_no_reachable_hedge_price_can_make_a_losing_pair(rest, venue):
    """The invariant, stated generally: for ANY rest fill, paying the cap leaves a fee-inclusive pair of
    at most $1.00/share. This is what "structurally impossible" means — it is a property of the number
    we send to the venue, not a check we remember to run."""
    cap = PregameLiveExecutor._hedge_price_cap(rest, venue, 0.05)
    assert rest + cap + hedge_taker_fee(venue, cap, 0.05) <= 1.0 + 1e-9


def test_a_rest_fill_with_no_payable_hedge_never_places_an_order():
    """When the cap solves to 0 there is no price that clears break-even; the hedger must report a miss
    (-> verified unwind) rather than fire a 1-tick order that cannot fill."""
    assert PregameLiveExecutor._hedge_price_cap(1.05, "kalshi", 0.05) == 0.0

    class _Boom:
        def place_order(self, *a, **k):            # pragma: no cover - must never be reached
            raise AssertionError("placed an order at a price the gate could not approve")

    h = LiveHedger(kalshi_client=_Boom(), poly_client=_Boom())
    res = h.hedge({"price": 1.05, "size": 10}, {"ticker": "KX-1", "side": "no",
                                                "best_ask": 0.5, "max_price": 0.0})
    assert res.status == "missed" and res.hedged_shares == 0
    res = h.hedge_poly({"price": 1.05, "size": 10}, {"token": "T", "best_ask": 0.5, "max_price": 0.0})
    assert res.status == "missed" and res.hedged_shares == 0


def test_the_executed_cap_is_never_looser_than_the_gate_that_approved_it():
    """The order is built FROM the cap: the hedger may quote tighter (a better book) but never looser."""
    from src.genz.maker_rt.hedge import _apply_cap
    cap = PregameLiveExecutor._hedge_price_cap(0.62, "polymarket", 0.05)
    for book_limit in (0.10, 0.30, cap, 0.50, 0.99):
        assert _apply_cap(book_limit, cap) <= cap + 1e-12


# --------------------------------------------------------------------------- #
# 4. booking-time invariants: an impossible number is REFUSED, not recorded     #
# --------------------------------------------------------------------------- #
def test_booking_refuses_both_signs_of_the_space_error():
    """The same bug reads as a disaster on one side of 50c and a windfall on the other. Both are
    refused, because both are the same impossible pair."""
    refuse = PregameLiveExecutor.book_refuse_reason
    assert refuse(0.76, 0.77, -0.5425, 5.0) == "pair_out_of_band"      # Cerezo   $1.53/share
    assert refuse(0.04, 0.05, 0.9067, 5.0) == "pair_out_of_band"       # Fortaleza $0.09/share
    assert refuse(0.33, 0.3561, 0.2978, 5.0) == "pair_out_of_band"     # Tottenham $0.69/share


def test_booking_refuses_an_edge_above_the_same_ceiling_that_gates_quoting():
    """A ceiling that only gates the QUOTE stops applying the moment real money is involved."""
    refuse = PregameLiveExecutor.book_refuse_reason
    assert refuse(0.50, 0.48, 0.06, 5.0) == "locked_above_ceiling"     # pair in band, edge 6% > 5%
    assert refuse(0.50, 0.48, 0.02, 5.0) is None                       # same pair, plausible edge


def test_booking_admits_every_genuine_pair_the_maker_is_meant_to_trade():
    """The rail must not strangle the trade. Real venue-truth pairs from the same session all book."""
    refuse = PregameLiveExecutor.book_refuse_reason
    assert refuse(0.04, 0.95, 0.006675, 5.0) is None       # FORBOT  real: pair 0.99, +0.67%
    assert refuse(0.33, 0.643821, 0.010127, 5.0) is None   # TOTSYD  real: pair 0.97, +1.01%
    assert refuse(0.76, 0.23, -0.0024, 5.0) is None        # CERBVB  real: pair 0.99, -0.24%
    assert refuse(0.62, 0.35, 0.03, 5.0) is None           # the "small loss" that was really +3%
    assert refuse(0.97, 0.0185, 0.0115, 5.0) is None       # BARCAA  real
    assert refuse(0.46, 0.53, 0.01, 5.0) is None           # a plain 1% pair
    assert refuse(0.50, None, 0.01, 5.0) is None           # no hedge price -> other paths own it


def test_pair_band_edges_are_the_sanity_ceiling_and_the_dollar():
    """The band is not a magic number: its floor IS the quoting ceiling (so the two rails can never
    disagree) and its cap is $1.00 plus tick/fee slack."""
    refuse = PregameLiveExecutor.book_refuse_reason
    assert refuse(0.50, 0.45, 0.05, 5.0) is None                        # pair 0.95 == 1 - ceiling
    assert refuse(0.50, 0.4499, 0.0501, 5.0) == "pair_out_of_band"      # a hair beyond it
    assert refuse(0.50, 0.50 + PAIR_SUM_TOL, -0.03, 5.0) is None        # pair 1.03 == the upper edge
    assert refuse(0.50, 0.5301, -0.0301, 5.0) == "pair_out_of_band"


def test_execution_floor_is_break_even():
    """Pinned so a future 'let it lose a little' change has to be deliberate."""
    assert HEDGE_EXECUTION_FLOOR == 0.0
