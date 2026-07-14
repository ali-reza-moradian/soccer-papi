"""Tests for src/og_sizing.py — honest walk-to-stake sizing of OG arbs."""
from __future__ import annotations

from src import bookmath, og_sizing as og


def test_all_fixed_reproduces_classic_arb():
    """Two fixed-odds books (no ladder): the walk degenerates to the classic limit-bound arb sizing —
    Pinnacle @2.10 (limit 1500) + 1xbet @2.05 -> both legs pay 3150, profit ~ $113 (the worked example)."""
    h = og.honest_size([
        {"outcome": "Over", "book": "pinnacle", "top_odds": 2.10, "limit": 1500},
        {"outcome": "Under", "book": "1xbet", "top_odds": 2.05, "limit": 5000},
    ], bankroll_cap=30000)
    assert h is not None
    assert abs(h.profit - 113.41) < 0.5
    pays = {lg.book: lg.payout for lg in h.legs}
    assert abs(pays["pinnacle"] - 3150) < 1 and abs(pays["1xbet"] - 3150) < 1
    assert all(lg.avg_fill_odds == lg.top_odds for lg in h.legs)      # flat books never degrade


def test_walked_profit_matches_independent_recompute():
    """Every leg's realized profit equals an INDEPENDENT bookmath.walk_book recomputation within 1e-6."""
    la = [(0.44, 300), (0.46, 500), (0.48, 5000)]     # polymarket
    lb = [(0.50, 400), (0.52, 9000)]                  # kalshi
    h = og.honest_size([
        {"outcome": "A", "book": "polymarket", "top_odds": 1 / 0.44, "ladder": la},
        {"outcome": "B", "book": "kalshi", "top_odds": 1 / 0.50, "ladder": lb},
    ], bankroll_cap=1e12)
    assert h is not None
    R = min(lg.payout for lg in h.legs)               # equal-payout target = shares on each exchange leg
    ca = bookmath.walk_book(bookmath.valid_asks(la), R).cost
    cb = bookmath.walk_book(bookmath.valid_asks(lb), R).cost
    assert abs(h.total_stake - (ca + cb)) < 1e-6
    assert abs(h.profit - (R - (ca + cb))) < 1e-6


def test_france_regression_shrinks_not_the_top_of_book_lie():
    """The France case: France 2.65@poly on a THIN cheap level, Draw@1xbet flat, Spain 3.36@poly thin.
    The scanner's top-of-book profit at the ~$3,000 T_max is a lie (France really fills worse and the
    'arb' loses). Honest sizing SHRINKS to the cheap-depth boundary with a real, much smaller profit —
    it must NOT report the naive top-of-book figure."""
    h = og.honest_size([
        {"outcome": "France", "book": "polymarket", "top_odds": 2.65, "ladder": [(0.3774, 800), (0.45, 100000)]},
        {"outcome": "Draw", "book": "1xbet", "top_odds": 3.15, "limit": 100000},
        {"outcome": "Spain", "book": "polymarket", "top_odds": 3.36, "ladder": [(0.2976, 800), (0.35, 100000)]},
    ], bankroll_cap=30000)
    assert h is not None
    assert h.total_stake < 1000                       # shrunk far below the naive ~$3,000 stake
    assert 0 < h.profit < 15                          # a real, small walked profit — not the naive top lie
    # profit is exactly min(leg payout) - total_stake (guaranteed, worst-leg)
    assert abs(h.profit - (min(lg.payout for lg in h.legs) - h.total_stake)) < 0.02


def test_ultra_thin_below_floor():
    """When the exchange cheap depth is tiny, the honest boundary is a few dollars — below the $20 stake
    floor — so the caller drops it (here we assert the boundary is below the floor)."""
    h = og.honest_size([
        {"outcome": "A", "book": "polymarket", "top_odds": 2.65, "ladder": [(0.3774, 6), (0.45, 1e5)]},
        {"outcome": "B", "book": "1xbet", "top_odds": 3.15, "limit": 1e5},
        {"outcome": "C", "book": "polymarket", "top_odds": 3.36, "ladder": [(0.2976, 6), (0.35, 1e5)]},
    ], bankroll_cap=30000)
    assert h is not None and h.t_max_honest < 20


def test_no_edge_returns_none():
    """A pairing whose top-of-book already sums to S >= 1 (not an arb) yields no honest size."""
    assert og.honest_size([
        {"outcome": "A", "book": "polymarket", "top_odds": 1 / 0.55, "ladder": [(0.55, 1000)]},
        {"outcome": "B", "book": "pinnacle", "top_odds": 1 / 0.50, "limit": 1000},
    ], bankroll_cap=30000) is None
