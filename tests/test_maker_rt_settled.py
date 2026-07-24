"""Settled-P&L reconciliation (venue truth, BOTH legs): net_pnl + settled_row math, the reconciler that
nets a Kalshi settlement against the Poly complement redemption, and the state aggregation the panel /
summary read as the AUTHORITATIVE lifetime realized pnl (vs the fill-time estimate). Includes the TBTOR
+$0.18 backfill number."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.genz.maker_rt.settle import (SettledLeg, SettledPnlReconciler, kalshi_settle_dollars,
                                       net_pnl, sane_settled, settled_row)
from src.genz.maker_rt.state import MakerState

_DT = datetime(2026, 7, 23, 22, 41, 0, tzinfo=timezone.utc)


class _CritLog:
    """Logger that captures critical/error/warning lines for assertions."""
    def __init__(self):
        self.criticals: list = []
        self.errors: list = []
        self.warns: list = []

    def critical(self, msg, *a):
        self.criticals.append(msg % a if a else msg)

    def error(self, msg, *a):
        self.errors.append(msg % a if a else msg)

    def warning(self, msg, *a):
        self.warns.append(msg % a if a else msg)


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
    $0. Kalshi revenue is CENTS: 1000 => $10.00 (10 contracts won)."""
    rec = SettledPnlReconciler(kalshi=_KalshiSettle([{"ticker": "KX-TB", "market_result": "yes",
                                                      "revenue": 1000}]))    # 1000 cents == $10.00
    pairs = [{"sport": "mlb", "game": "G", "market_key": "ml2",
              "kalshi": {"ticker": "KX-TB", "side": "yes", "shares": 10, "cost": 6.0},
              "poly": {"token": "TOR", "shares": 10, "cost": 3.5}}]
    emitted = rec.reconcile(pairs, _DT)
    # kalshi: 10 - 6.0 = +4.0 ; poly: 0 (lost) - 3.5 = -3.5 ; net = +0.5
    assert emitted and emitted[0]["realized_pnl_usd"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# 100x UNIT BUG: Kalshi settlement money is CENTS, normalized centrally         #
# --------------------------------------------------------------------------- #
def test_kalshi_settle_dollars_always_divides_by_100():
    """The central normalizer: ALWAYS cents->dollars, no magnitude guessing. 500c == $5.00 (the value
    the old >=1000 heuristic let through as $500.00 -> the +$495.15 phantom)."""
    assert kalshi_settle_dollars(500) == pytest.approx(5.00)     # <-- the incident value
    assert kalshi_settle_dollars(0) == pytest.approx(0.0)
    assert kalshi_settle_dollars(1000) == pytest.approx(10.00)
    assert kalshi_settle_dollars("205") == pytest.approx(2.05)
    assert kalshi_settle_dollars(None) == pytest.approx(0.0)     # unparseable -> 0, never crash


def test_hanhal_cents_payload_reconciles_to_seven_cents():
    """HANHAL venue truth THROUGH the reconciler: Kalshi 5 HAL YES won, revenue 500c == $5.00 (NOT
    $500); cost $2.13 (incl. ~8c taker fee). Poly 5 lost, cost $2.80. NET +$0.07, ROI +1.42% on $4.93."""
    rec = SettledPnlReconciler(kalshi=_KalshiSettle([{"ticker": "KXATPMATCH-26JUL24HANHAL-HAL",
                                                      "market_result": "yes", "revenue": 500}]))
    pairs = [{"sport": "tennis", "game": "KXATPMATCH-26JUL24HANHAL", "market_key": "match_winner",
              "kalshi": {"ticker": "KXATPMATCH-26JUL24HANHAL-HAL", "side": "yes", "shares": 5, "cost": 2.13},
              "poly": {"token": "hanfmann", "shares": 5, "cost": 2.80}}]
    emitted = rec.reconcile(pairs, _DT)
    assert len(emitted) == 1
    assert emitted[0]["realized_pnl_usd"] == pytest.approx(0.07, abs=1e-9)
    assert emitted[0]["settled_cost_usd"] == pytest.approx(4.93)
    assert "+1.42%" in emitted[0]["reason"] and "$500" not in emitted[0]["reason"]


# --------------------------------------------------------------------------- #
# SANITY GUARD: an implausible settled net is REFUSED, never counted            #
# --------------------------------------------------------------------------- #
def test_sane_settled_refuses_the_495_dollar_bug_but_passes_real_trades():
    ok, why = sane_settled(495.15, 4.85, max_net_usd=100.0)      # the HANHAL corruption
    assert ok is False and "net" in why
    assert sane_settled(0.07, 4.93, max_net_usd=100.0)[0] is True     # HANHAL truth
    assert sane_settled(0.18, 17.19, max_net_usd=100.0)[0] is True    # TBTOR truth
    # A high ROI on a tiny cost is refused even when |net| is small.
    assert sane_settled(0.30, 0.10, max_net_usd=100.0)[0] is False


def test_reconciler_refuses_implausible_settlement_and_marks_settled():
    """If a bug ever produced a huge settled net (e.g. a future payload the cents fix didn't catch), the
    reconciler REFUSES to emit/record it, marks it settled so it never re-screams, and logs CRITICAL."""
    rows: list = []
    log = _CritLog()
    # 50000 cents -> $500.00 payout on a 5-share pair => net +$495.15 -> implausible.
    rec = SettledPnlReconciler(kalshi=_KalshiSettle([{"ticker": "KX-X", "market_result": "yes",
                                                      "revenue": 50000}]),
                               record=lambda r, n: rows.append(r), log=log, max_pair_stake_usd=100.0)
    pairs = [{"sport": "tennis", "game": "G", "market_key": "mw",
              "kalshi": {"ticker": "KX-X", "side": "yes", "shares": 5, "cost": 2.05},
              "poly": {"token": "T", "shares": 5, "cost": 2.80}}]
    assert rec.reconcile(pairs, _DT) == []                       # nothing emitted
    assert rows == []                                            # nothing recorded into lifetime pnl
    assert rec.already_settled("G", "mw") is True               # marked so it never re-screams
    assert any("REFUSED" in c and "CRITICAL" in c for c in log.criticals)


def test_state_aggregator_refuses_implausible_trade_settled():
    """Defense-in-depth: even a hand-fed corrupt trade_settled row must never touch lifetime pnl."""
    log = _CritLog()
    st = MakerState(log=log)
    st.settled_max_net_usd = 100.0
    st.record({"event": "trade_settled", "sport": "tennis", "phase": "settled",
               "game": "KXATPMATCH-26JUL24HANHAL", "market_key": "match_winner",
               "realized_pnl_usd": 495.15, "settled_cost_usd": 4.85,
               "reason": "HANHAL SETTLED net $+495.15"}, _DT)
    assert st.settled_pnl_lifetime == pytest.approx(0.0)
    assert st.settled_trades == 0
    assert any("REFUSED" in c and "CRITICAL" in c for c in log.criticals)


def test_recompute_totals_are_25_cents_from_venue_truth():
    """The recompute target: TBTOR +$0.18 and HANHAL +$0.07 net to +$0.25 lifetime across 2 trades."""
    tbtor = net_pnl([SettledLeg("kalshi", "KX-TB", "yes", 16, 0.98, 0.00),
                     SettledLeg("polymarket", "TOR", "buy", 17.4, 16.21, 17.37)])
    hanhal = net_pnl([SettledLeg("kalshi", "KXATPMATCH-26JUL24HANHAL-HAL", "yes", 5, 2.13, 5.00),
                      SettledLeg("polymarket", "hanfmann", "buy", 5, 2.80, 0.00)])
    assert tbtor["net"] == pytest.approx(0.18)
    assert hanhal["net"] == pytest.approx(0.07)
    assert round(tbtor["net"] + hanhal["net"], 2) == pytest.approx(0.25)
    assert round(tbtor["cost"] + hanhal["cost"], 2) == pytest.approx(22.12)


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
