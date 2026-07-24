"""Settled-P&L reconciliation (venue truth, BOTH legs): net_pnl + settled_row math, the reconciler that
nets a Kalshi settlement against the Poly complement redemption, and the state aggregation the panel /
summary read as the AUTHORITATIVE lifetime realized pnl (vs the fill-time estimate). Includes the TBTOR
+$0.18 backfill number."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.genz.maker_rt.settle import SettledLeg, SettledPnlReconciler, net_pnl, settled_row
from src.genz.maker_rt.state import MakerState

_DT = datetime(2026, 7, 23, 22, 41, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# pure netting + row builder                                                    #
# --------------------------------------------------------------------------- #
def test_net_pnl_nets_both_legs_tbtor():
    """TBTOR venue truth: Kalshi 16 TB @6.1c settled $0 (lost) = -$0.98; Poly 17.4 TOR @93.3c redeemed
    $17.37 = +$1.16. NET +$0.18 on $17.19 cost = +1.05% ROI. The realized pnl MUST net BOTH legs incl.
    settlement/redemption, not just the rest leg."""
    legs = [SettledLeg("kalshi", "KX-TB", "yes", 16, 0.98, 0.00),
            SettledLeg("polymarket", "TOR", "buy", 17.4, 16.21, 17.37)]
    n = net_pnl(legs)
    assert n["net"] == pytest.approx(0.18, abs=1e-9)
    assert n["cost"] == pytest.approx(17.19)
    assert n["revenue"] == pytest.approx(17.37)
    assert n["roi"] == pytest.approx(0.18 / 17.19, rel=1e-4)


def test_settled_row_shape_and_reason():
    legs = [SettledLeg("kalshi", "KX-TB", "yes", 16, 0.98, 0.00),
            SettledLeg("polymarket", "TOR", "buy", 17.4, 16.21, 17.37)]
    row = settled_row(sport="mlb", game="26JUL231507TBTOR", market_key="ml2", legs=legs,
                      settled_ts="2026-07-23T22:41:00Z", market_id="KX-TB")
    assert row["event"] == "trade_settled" and row["phase"] == "settled"
    assert row["realized_pnl_usd"] == pytest.approx(0.18)
    assert row["settled_cost_usd"] == pytest.approx(17.19)
    assert "SETTLED net $+0.18" in row["reason"] and "+1.05%" in row["reason"]


# --------------------------------------------------------------------------- #
# reconciler: net a Kalshi settlement against the Poly complement redemption    #
# --------------------------------------------------------------------------- #
class _KalshiSettle:
    def __init__(self, settlements):
        self._s = settlements

    def get_settlements(self, *, limit=None):
        return {"settlements": self._s}


def test_reconciler_nets_kalshi_settlement_and_poly_complement():
    """The Kalshi leg lost (market_result 'no' on a 'yes' bet, revenue $0); the Poly complement (opposite
    outcome) therefore WON and redeems 1:1. One settlement read resolves both legs -> a trade_settled row."""
    rows = []
    rec = SettledPnlReconciler(kalshi=_KalshiSettle([{"ticker": "KX-TB", "market_result": "no",
                                                      "revenue": 0.0}]),
                               record=lambda r, n: rows.append(r))
    pairs = [{"sport": "mlb", "game": "G", "market_key": "ml2",
              "kalshi": {"ticker": "KX-TB", "side": "yes", "shares": 10, "cost": 6.0},
              "poly": {"token": "TOR", "shares": 10, "cost": 3.5}}]
    emitted = rec.reconcile(pairs, _DT)
    assert len(emitted) == 1
    # kalshi: 0 - 6.0 = -6.0 ; poly: 10 (redeemed) - 3.5 = +6.5 ; net = +0.5
    assert emitted[0]["realized_pnl_usd"] == pytest.approx(0.5)
    assert rows == emitted
    # IDEMPOTENT: a second pass over the same (still-settled) market emits nothing.
    assert rec.reconcile(pairs, _DT) == []


def test_reconciler_skips_unsettled_market():
    """A market with no settlement yet is left for the next pass (no row, not an error)."""
    rec = SettledPnlReconciler(kalshi=_KalshiSettle([]))       # nothing settled
    pairs = [{"sport": "mlb", "game": "G", "market_key": "ml2",
              "kalshi": {"ticker": "KX-TB", "side": "yes", "shares": 10, "cost": 6.0},
              "poly": {"token": "TOR", "shares": 10, "cost": 3.5}}]
    assert rec.reconcile(pairs, _DT) == []
    assert not rec.already_settled("G", "ml2")


def test_reconciler_kalshi_leg_won_uses_revenue():
    """When the Kalshi leg WON the venue revenue is used directly, and the Poly complement (lost) redeems
    $0."""
    rec = SettledPnlReconciler(kalshi=_KalshiSettle([{"ticker": "KX-TB", "market_result": "yes",
                                                      "revenue": 10.0}]))
    pairs = [{"sport": "mlb", "game": "G", "market_key": "ml2",
              "kalshi": {"ticker": "KX-TB", "side": "yes", "shares": 10, "cost": 6.0},
              "poly": {"token": "TOR", "shares": 10, "cost": 3.5}}]
    emitted = rec.reconcile(pairs, _DT)
    # kalshi: 10 - 6.0 = +4.0 ; poly: 0 (lost) - 3.5 = -3.5 ; net = +0.5
    assert emitted and emitted[0]["realized_pnl_usd"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# state aggregation: settled truth becomes the lifetime realized number         #
# --------------------------------------------------------------------------- #
def test_state_aggregates_trade_settled_into_lifetime():
    st = MakerState()
    st.record({"event": "trade_settled", "sport": "mlb", "phase": "settled", "game": "G",
               "market_key": "ml2", "realized_pnl_usd": 0.18, "settled_cost_usd": 17.19,
               "reason": "TBTOR SETTLED net $+0.18"}, _DT)
    assert st.settled_pnl_lifetime == pytest.approx(0.18)
    assert st.settled_cost_lifetime == pytest.approx(17.19)
    assert st.settled_trades == 1
    summ = st.summary("live", {}, _DT)
    assert summ["settled_pnl_lifetime"] == pytest.approx(0.18)
    assert summ["settled_trades"] == 1
    assert summ["settled_roi"] == pytest.approx(0.18 / 17.19, abs=1e-4)   # summary rounds ROI to 4 dp
    hb = st.heartbeat("live", {}, 0, _DT)
    assert hb["settled_pnl_lifetime"] == pytest.approx(0.18) and hb["settled_trades"] == 1


def test_settled_lifetime_survives_daily_roll():
    st = MakerState(day="20260723")
    st.record({"event": "trade_settled", "sport": "mlb", "phase": "settled", "game": "G",
               "market_key": "ml2", "realized_pnl_usd": 0.18, "settled_cost_usd": 17.19}, _DT)
    st._roll("20260724")                                    # new UTC day
    assert st.settled_pnl_lifetime == pytest.approx(0.18) and st.settled_trades == 1
