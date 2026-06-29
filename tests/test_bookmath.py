"""Pure book-math helper: walk_book parity with the executor, and the VWAP-within-band primitive."""
from __future__ import annotations

import pytest

from src import bookmath
from src.executor import fees_sizing


# --------------------------------------------------------------------------- #
# (c) The shared walk-book helper must match the executor's walk_book exactly   #
#     on shared inputs, so scanner pricing and executor fills can't drift.       #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("levels,size", [
    ([(0.10, 100.0), (0.11, 200.0), (0.12, 50.0)], 250.0),     # partial into 2nd level
    ([(0.10, 100.0), (0.11, 200.0)], 1000.0),                  # under-fill (ladder exhausted)
    ([(0.50, 10.0)], 10.0),                                    # exact single level
    ([(0.30, 0.0), (0.31, 5.0)], 4.0),                         # zero-size level skipped
    ([], 5.0),                                                 # empty ladder
    ([(0.20, 100.0)], 0.0),                                    # zero request
])
def test_walk_book_matches_executor(levels, size):
    a = bookmath.walk_book(levels, size)
    b = fees_sizing.walk_book(levels, size)
    assert a.filled == b.filled
    assert a.avg_price == b.avg_price
    assert a.cost == b.cost
    assert a.fully_filled == b.fully_filled
    assert a.levels_consumed == b.levels_consumed


# --------------------------------------------------------------------------- #
# vwap_within_band                                                              #
# --------------------------------------------------------------------------- #
def test_vwap_band_averages_in_worse_levels_within_band():
    # Best ask 0.10; within 5% (ceiling 0.105) include 0.10 and 0.105 but NOT 0.12.
    levels = [(0.10, 1000.0), (0.105, 1000.0), (0.12, 1_000_000.0)]
    vwap, shares = bookmath.vwap_within_band(levels, 5.0)
    assert shares == 2000.0                                # only the two in-band levels
    assert vwap == pytest.approx((0.10 * 1000 + 0.105 * 1000) / 2000)   # 0.1025
    assert 1.0 / vwap < 1.0 / 0.10                         # quoted odds are WORSE than the top tick


def test_vwap_band_deep_book_is_essentially_the_best_ask():
    # A wall of depth at ~the best ask -> VWAP ≈ best ask, large fillable size.
    levels = [(0.827, 50000.0), (0.828, 100000.0), (0.829, 200000.0)]
    vwap, shares = bookmath.vwap_within_band(levels, 2.0)
    assert shares == 350000.0
    assert abs((1.0 / vwap) - (1.0 / 0.827)) < 0.01        # within a tick of the best-ask odds


def test_vwap_band_zero_slippage_keeps_only_best_priced_levels():
    levels = [(0.20, 100.0), (0.20, 50.0), (0.21, 999.0)]
    vwap, shares = bookmath.vwap_within_band(levels, 0.0)
    assert shares == 150.0 and vwap == pytest.approx(0.20)  # 0.21 is worse than the best -> excluded


def test_vwap_band_unsorted_and_invalid_levels():
    # Out-of-order input + a junk level (price >= 1) is sorted/filtered; best ask is the true minimum.
    levels = [(0.13, 100.0), (0.11, 100.0), (1.4, 5.0), (0.12, 100.0)]
    vwap, shares = bookmath.vwap_within_band(levels, 100.0)  # huge band -> all three valid levels
    assert shares == 300.0
    assert vwap == pytest.approx((0.11 + 0.12 + 0.13) / 3 * 100 / 100)


def test_vwap_band_none_on_empty_or_no_valid_ask():
    assert bookmath.vwap_within_band([], 2.0) is None
    assert bookmath.vwap_within_band([(1.0, 5.0), (0.0, 5.0)], 2.0) is None
