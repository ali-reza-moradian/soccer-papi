"""Sizing = the LARGEST size that fits every constraint; the venue minimum is a FLOOR, never a ceiling.

The old `min(floor_min, cap)` clamped every quote DOWN to 5 shares, so a $2.80 fill rested against a
$20 cap. `plan_size` now takes the smallest resource allowance (notional/pair/daily caps + hedge/book
depth) and refuses only when even the venue minimum won't fit. These tests pin the exact live numbers
from the diagnostic and the refusal path.
"""
from __future__ import annotations

from src.genz.maker_rt.alerts import digest_line
from src.genz.maker_rt.caps import plan_size

from .test_maker_rt_pregame import _Log, _Store, _cand, _cand_kalshi, _dec, _exec, _exec_kalshi

_BIG = 10 ** 9        # a depth that never binds


# --------------------------------------------------------------------------- #
# plan_size: the largest fitting size, minimum as a FLOOR                        #
# --------------------------------------------------------------------------- #
def test_hussein_live_numbers_size_to_the_cap_not_the_minimum():
    """The exact live case: 0.81 price, $20 cap, $100 pair, hedge 1386 / book 77285, min 5 -> size 24
    (cap-bound), NOT 5."""
    p = plan_size(0.81, 0.18, quote_usd_max=20.0, max_pair_stake_usd=100.0, daily_stake_headroom=300.0,
                  hedge_depth=1386, book_depth=77285, venue_minimum=5)
    assert p["size"] == 24 and p["binding"] == "quote_usd_max" and p["refused"] is False


def test_gibson_live_numbers_are_pair_bound():
    """0.16 price, $20 cap (=125 sh), $100 pair (=~102 sh), deep books, min 7 -> pair-bound at 102."""
    p = plan_size(0.16, 0.82, quote_usd_max=20.0, max_pair_stake_usd=100.0, daily_stake_headroom=300.0,
                  hedge_depth=8892, book_depth=31281, venue_minimum=7)
    assert p["size"] == 102 and p["binding"] == "pair_cap"


def test_hedge_depth_binds_when_thinnest():
    p = plan_size(0.50, 0.48, quote_usd_max=20.0, max_pair_stake_usd=100.0, daily_stake_headroom=300.0,
                  hedge_depth=9, book_depth=_BIG, venue_minimum=5)
    assert p["size"] == 9 and p["binding"] == "hedge_depth"


def test_book_depth_binds_when_thinnest():
    p = plan_size(0.50, 0.48, quote_usd_max=20.0, max_pair_stake_usd=100.0, daily_stake_headroom=300.0,
                  hedge_depth=_BIG, book_depth=8, venue_minimum=5)
    assert p["size"] == 8 and p["binding"] == "book_depth"


def test_daily_stake_headroom_binds_when_budget_nearly_used():
    """Only $6 of the daily budget left, pair ~$1/sh -> ~6 shares, below the cap's 40."""
    p = plan_size(0.50, 0.48, quote_usd_max=20.0, max_pair_stake_usd=100.0, daily_stake_headroom=6.0,
                  hedge_depth=_BIG, book_depth=_BIG, venue_minimum=5)
    assert p["size"] == 6 and p["binding"] == "daily_stake"


def test_below_venue_minimum_refuses_never_clamps_down():
    """If the largest fitting size is under the venue minimum, REFUSE — don't rest a sub-minimum order."""
    p = plan_size(0.50, 0.48, quote_usd_max=20.0, max_pair_stake_usd=100.0, daily_stake_headroom=300.0,
                  hedge_depth=3, book_depth=_BIG, venue_minimum=5)     # only 3 hedge shares, min 5
    assert p["refused"] is True and p["binding"] == "below_venue_minimum"
    assert p["max_fit"] == 3 and p["size"] == 0 and p["limiter"] == "hedge_depth"


def test_pair_cap_divides_by_the_pair_notional_so_it_never_breaches():
    """price 0.50 + hedge 0.50, $100 pair -> 100 shares (pair stake = 100*(0.5+0.5)=$100), not 200."""
    p = plan_size(0.50, 0.50, quote_usd_max=1000.0, max_pair_stake_usd=100.0, daily_stake_headroom=1e9,
                  hedge_depth=_BIG, book_depth=_BIG, venue_minimum=5)
    assert p["size"] == 100 and p["binding"] == "pair_cap"


# --------------------------------------------------------------------------- #
# executor integration: real sizing on placement                                #
# --------------------------------------------------------------------------- #
def test_place_sizes_up_to_the_cap_and_logs_binding(tmp_path):
    """Default caps: quote $20. At 0.46 with deep test books (500 hedge) the size is hedge-bound at 500
    unless the cap is lower; here cap = floor(20/0.46)=43 binds."""
    ex, cfg = _exec(tmp_path)
    ex.caps.quote_usd_max = 20.0
    ex.caps.max_pair_stake_usd = 100.0
    ex.caps.max_daily_stake_usd = 300.0
    ex.log = _Log()
    # _Store books: poly ask ladder 500 @ 0.55 (book depth), kalshi hedge 500 @ 0.50 (hedge depth).
    ex.place_or_reprice(_cand(), _dec(price=0.46, hedge_ask=0.50), None,
                        _Store(poly_best_ask=0.60, kalshi_ask=0.50), now=None, now_ts=1.0)
    lo = list(ex.open_orders.values())[0]
    assert lo.size == 43, "must size UP to the $20 cap (floor(20/0.46)=43), not the 5-share minimum"
    assert ex._binding_counts.get("quote_usd_max") == 1
    assert any("binding=quote_usd_max" in i for i in ex.log.infos)


def test_kalshi_rest_uses_one_contract_minimum_not_five(tmp_path):
    """A thin hedge that only supports 2 shares is REFUSED for Poly (min 5) but ALLOWED for Kalshi
    (min 1 contract)."""
    ex, _ = _exec_kalshi(tmp_path)
    ex.log = _Log()
    store = _Store(kalshi_ask=0.60)
    store.poly_view = lambda t: __import__("types").SimpleNamespace(
        best_ask=0.55, best_bid=0.53, ask_ladder=[(0.55, 2)])          # hedge (poly) depth = 2
    c = _cand_kalshi()
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.55), None, store, None, 1.0, "pre")
    assert c.key in ex.open_orders, "Kalshi min is 1 contract -> a 2-share fit is placeable"
    assert ex.open_orders[c.key].size == 2


def test_below_minimum_refuses_the_quote(tmp_path):
    ex, _ = _exec(tmp_path)          # rest-poly, min 5
    ex.log = _Log()
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    store.kalshi_view = lambda t, s: __import__("types").SimpleNamespace(
        best_ask=0.50, best_bid=0.49, ask_ladder=[(0.50, 3)])          # only 3 hedge shares, min 5
    ex.place_or_reprice(_cand(), _dec(price=0.46, hedge_ask=0.50), None, store, None, 1.0)
    assert not ex.open_orders, "a sub-minimum fit must be refused, not clamped to 5"
    assert ex._binding_counts.get("below_venue_minimum") == 1


# --------------------------------------------------------------------------- #
# digest line surfaces the dominant constraint                                  #
# --------------------------------------------------------------------------- #
def test_digest_line_explains_why_no_fills():
    line = digest_line(15, placed=12, cancelled=9, fills=0, open_now=1, max_open=2, best_edge_pct=0.9,
                       why_no_fills="sizes were limited by hedge/book depth")
    assert "Why no fills: sizes were limited by hedge/book depth" in line
    # once there ARE fills, the 'why no fills' line is dropped.
    assert "Why no fills" not in digest_line(15, placed=1, cancelled=0, fills=1, open_now=1, max_open=2)
