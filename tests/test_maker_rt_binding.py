"""Binding-constraint diagnostic: what actually limits a rest-leg's SIZE.

The HANHAL fill rested only $2.80 against a $20 cap — not because the cap was tight but because the
maker rests the pilot MINIMUM (5 shares) by design. These tests pin the diagnostic that makes that
legible on every quote (quote_usd_max | pair cap | hedge depth | book depth | venue minimum), so we
can tell whether raising caps would grow fills or whether depth / the minimum is the ceiling.
"""
from __future__ import annotations

from src.genz.maker_rt.alerts import digest_line
from src.genz.maker_rt.caps import binding_constraint

from .test_maker_rt_pregame import _Log, _Store, _cand, _dec, _exec


# --------------------------------------------------------------------------- #
# pure function                                                                 #
# --------------------------------------------------------------------------- #
def test_the_hanhal_shape_is_bound_by_the_minimum_not_the_cap():
    """5 sh @ 56c = $2.80 against a $20 cap: every resource allows >= 5, so the pilot MINIMUM binds —
    raising the cap would NOT grow the fill."""
    name, limit = binding_constraint(0.56, quote_usd_max=20.0, max_pair_stake_usd=100.0,
                                     hedge_ask=0.41, hedge_depth=500, book_depth=500, min_floor=5)
    assert name == "venue_minimum" and limit == 5.0


def test_quote_usd_max_binds_when_it_clamps_below_the_floor():
    name, limit = binding_constraint(0.56, quote_usd_max=2.0, max_pair_stake_usd=100.0,
                                     hedge_ask=0.41, hedge_depth=500, book_depth=500, min_floor=5)
    assert name == "quote_usd_max" and limit == 3.0        # floor(2.0/0.56) = 3


def test_pair_cap_binds():
    name, limit = binding_constraint(0.56, quote_usd_max=20.0, max_pair_stake_usd=2.0,
                                     hedge_ask=0.41, hedge_depth=500, book_depth=500, min_floor=5)
    assert name == "pair_cap" and limit == 2.0             # floor(2.0/0.97) = 2


def test_hedge_depth_binds_when_thinnest():
    name, limit = binding_constraint(0.56, quote_usd_max=20.0, max_pair_stake_usd=100.0,
                                     hedge_ask=0.41, hedge_depth=3, book_depth=500, min_floor=5)
    assert name == "hedge_depth" and limit == 3.0


def test_book_depth_binds_when_thinnest():
    name, limit = binding_constraint(0.56, quote_usd_max=20.0, max_pair_stake_usd=100.0,
                                     hedge_ask=0.41, hedge_depth=500, book_depth=2, min_floor=5)
    assert name == "book_depth" and limit == 2.0


def test_caps_break_ties_before_depth():
    """When several resources allow the SAME sub-floor count, a cap is reported before depth."""
    name, _ = binding_constraint(1.0, quote_usd_max=3.0, max_pair_stake_usd=100.0, hedge_ask=None,
                                 hedge_depth=3, book_depth=3, min_floor=5)
    assert name == "quote_usd_max"


# --------------------------------------------------------------------------- #
# executor integration: every placed quote is diagnosed + counted               #
# --------------------------------------------------------------------------- #
def test_place_records_and_logs_the_binding_constraint(tmp_path):
    ex, _ = _exec(tmp_path)
    ex.log = _Log()
    ex.place_or_reprice(_cand(), _dec(price=0.46, hedge_ask=0.50), None,
                        _Store(poly_best_ask=0.60, kalshi_ask=0.50), now=None, now_ts=1.0)
    # Default caps (quote $5, pair $25) + deep 500-share test books => the 5-share minimum binds.
    assert ex._binding_counts.get("venue_minimum") == 1
    assert any("binding=venue_minimum" in i for i in ex.log.infos)
    assert ex.snapshot(2.0)["binding_counts"] == {"venue_minimum": 1}


def test_thin_hedge_book_makes_hedge_depth_the_binding_constraint(tmp_path):
    ex, _ = _exec(tmp_path)
    ex.log = _Log()
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    store.kalshi_view = lambda t, s: __import__("types").SimpleNamespace(
        best_ask=0.50, best_bid=0.49, ask_ladder=[(0.50, 3)])       # only 3 hedge shares available
    ex.place_or_reprice(_cand(), _dec(price=0.46, hedge_ask=0.50), None, store, now=None, now_ts=1.0)
    assert ex._binding_counts.get("hedge_depth") == 1


# --------------------------------------------------------------------------- #
# digest line surfaces the dominant constraint                                  #
# --------------------------------------------------------------------------- #
def test_digest_line_shows_the_dominant_binding():
    line = digest_line(15, placed=12, cancelled=9, fills=0, open_now=1, max_open=2,
                       best_edge_pct=0.9, binding="venue_minimum")
    assert "size bound by min size" in line
    assert "size bound by" not in digest_line(15, placed=1, cancelled=0, fills=0, open_now=1, max_open=2)
