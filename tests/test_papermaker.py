"""Tests for src/genz/papermaker.py — the PAPER-MAKER dry-run (synthetic maker quotes, no real orders)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from src import bookmath
from src.genz import papermaker as pm
from src.genz.engine import Market, PricedVenue

NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


class _MD:
    def __init__(self, poly_bid=0.48, kalshi_bid=0.46):
        self._pb, self._kb = poly_bid, kalshi_bid

    def poly_best_bid(self, token):
        return self._pb

    def kalshi_best_bid(self, ticker, side="YES"):
        return self._kb


def _market(k_o=("K_O", "YES"), p_o="P_O", k_u=("K_U", "NO"), p_u="P_U"):
    na = {"kalshi_ticker": k_o[0], "kalshi_side": k_o[1], "poly_token_id": p_o, "tick_size": 0.01,
          "poly_fee_enabled": True, "poly_fee_rate": 0.05}
    nb = {"kalshi_ticker": k_u[0], "kalshi_side": k_u[1], "poly_token_id": p_u, "tick_size": 0.01,
          "poly_fee_enabled": True, "poly_fee_rate": 0.05}
    return Market(game="G", away="A", home="H", market_type="corners", market_key="corners|8.5",
                  line=8.5, kind="2way", confidence="high", kickoff="2026-07-15T19:00:00Z",
                  sides={"over": na, "under": nb})


# --------------------------------------------------------------------------- #
# Pure economics                                                                #
# --------------------------------------------------------------------------- #
def test_floor_math_both_directions():
    """Confirmed fee formulas: rest-on-POLY hedge-take KALSHI@0.47 -> floor 0.50 at ~+1.3% net."""
    f = pm.floor_price(0.47, "kalshi", target_net_pct=1.0, tick=0.01)
    assert f == 0.50
    assert abs(pm.combo_net(f, 0.47, "kalshi") * 100.0 - 1.256) < 0.05          # +1.3% at the floor
    # rest-on-KALSHI hedge-take POLY@0.51 (rate 0.05) -> floor 0.45, +1.55% net
    f2 = pm.floor_price(0.51, "polymarket", poly_rate=0.05, target_net_pct=1.0, tick=0.01)
    assert f2 == 0.45
    assert abs(pm.combo_net(f2, 0.51, "polymarket", 0.05) * 100.0 - 1.55) < 0.05


def test_fill_rule_strictly_below():
    assert pm.is_filled(0.50, 0.50) is False        # ask == quote -> NOT filled (queue unknown)
    assert pm.is_filled(0.50, 0.49) is True          # ask one tick below -> traded through -> filled
    assert pm.is_filled(0.50, None) is False


def test_quote_never_above_floor_or_crossing():
    assert pm.quote_from_floor(0.50, 0.48, 0.01) == 0.49     # join/improve the bid, below the floor
    assert pm.quote_from_floor(0.50, 0.60, 0.01) == 0.50     # capped at the floor, never above
    assert pm.quote_from_floor(0.005, None, 0.01) is None    # floor below a tick -> no quote


def test_mark_hedge_equals_independent_walk_plus_fee():
    ladder = [(0.47, 60), (0.49, 1000)]
    marked = pm.mark_hedge(ladder, 100, "kalshi")
    w = bookmath.walk_book(bookmath.valid_asks(ladder), 100)
    fee = 0.07 * w.avg_price * (1.0 - w.avg_price) * w.filled
    assert abs(marked["cost"] - (w.cost + fee)) < 1e-6
    assert abs(marked["cost_per_share"] - (w.cost + fee) / w.filled) < 1e-6


# --------------------------------------------------------------------------- #
# State machine                                                                 #
# --------------------------------------------------------------------------- #
def _priced(poly_over_ask=0.55, kalshi_under_ask=0.47):
    return {
        ("kalshi", "K_O", "YES"): PricedVenue("kalshi", "K_O", 0.47, 0.47, 4700.0, ladder=[(0.47, 1000)]),
        ("poly", "P_O", "BUY"): PricedVenue("poly", "P_O", poly_over_ask, poly_over_ask, 5500.0, ladder=[(poly_over_ask, 1000)]),
        ("kalshi", "K_U", "NO"): PricedVenue("kalshi", "K_U", kalshi_under_ask, kalshi_under_ask, 4700.0, ladder=[(kalshi_under_ask, 1000)]),
        ("poly", "P_U", "BUY"): PricedVenue("poly", "P_U", 0.55, 0.55, 5500.0, ladder=[(0.55, 1000)]),
    }


def test_observe_quotes_and_at_best(tmp_path):
    p = pm.PaperMaker(target_net_pct=1.0, ref_shares=100, poly_rate=0.05)
    p.observe([_market()], _priced(), _MD(), NOW, None,
              csv_path=str(tmp_path / "pm.csv"), summary_path=str(tmp_path / "sum.json"))
    assert p.n_quotes == 4 and len(p.quotes) == 4           # 2 sides x 2 directions
    # rest-on-poly over: floor 0.50, bid 0.48 -> quote 0.49, at_best; never above floor
    q = p.quotes[("G", "corners|8.5", "over", "rest-poly")]
    assert q.quote_price == 0.49 and q.at_best is True and q.quote_price <= q.floor


def test_conservative_fill_and_hedge_marking(tmp_path):
    p = pm.PaperMaker(target_net_pct=1.0, ref_shares=100, poly_rate=0.05)
    csv_path = str(tmp_path / "pm.csv")
    # cycle 1: quote rest-poly over at 0.49 (poly ask 0.55, no fill)
    p.observe([_market()], _priced(poly_over_ask=0.55), _MD(), NOW, None, csv_path=csv_path)
    assert p.n_fills == 0
    # cycle 2: poly over ask trades to 0.48 (< 0.49) -> the over/rest-poly quote FILLS; hedge under@kalshi
    p.observe([_market()], _priced(poly_over_ask=0.48, kalshi_under_ask=0.47), _MD(), NOW, None, csv_path=csv_path)
    assert p.n_fills == 1
    # net at fill = 1 - quote(0.49) - (0.47 + kalshi_fee(0.47)) ~ +1.26%
    assert abs(p.fill_nets[0] - (1.0 - 0.49 - 0.47 - 0.07 * 0.47 * 0.53) * 100.0) < 1e-6
    rows = list(__import__("csv").DictReader(open(csv_path, encoding="utf-8")))
    assert any(r["event"] == "quote" for r in rows) and any(r["event"] == "fill" for r in rows)


def test_kickoff_expiry(tmp_path):
    p = pm.PaperMaker(target_net_pct=1.0, ref_shares=100, poly_rate=0.05)
    p.observe([_market()], _priced(), _MD(), NOW, None, csv_path=str(tmp_path / "pm.csv"))
    assert len(p.quotes) == 4
    # kickoff: the game is gated out upstream -> observe with NO markets -> all quotes expire.
    p.observe([], _priced(), _MD(), NOW, None, csv_path=str(tmp_path / "pm.csv"))
    assert len(p.quotes) == 0 and p.n_expired_unfilled == 4


def test_summary_and_csv_round_trip(tmp_path):
    p = pm.PaperMaker(target_net_pct=1.0, ref_shares=100, poly_rate=0.05)
    summ = str(tmp_path / "sum.json")
    p.observe([_market()], _priced(), _MD(), NOW, None, csv_path=str(tmp_path / "pm.csv"), summary_path=summ)
    s = json.loads(open(summ, encoding="utf-8").read())
    assert s["quotes"] == 4 and s["fills"] == 0 and s["at_best_share"] == 0.5 and s["paper"] is True
    # state round-trips across the fresh-process loop
    state = str(tmp_path / "state.json")
    p.save_state(state)
    p2 = pm.PaperMaker.load(state, target_net_pct=1.0, ref_shares=100, poly_rate=0.05)
    assert p2.n_quotes == 4 and len(p2.quotes) == 4 and p2.day == p.day
    assert p2.quotes[("G", "corners|8.5", "over", "rest-poly")].quote_price == 0.49
