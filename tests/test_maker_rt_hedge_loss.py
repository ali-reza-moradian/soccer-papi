"""Regression suite for the 2026-07-27 GUARANTEED-LOSS hedges (rest-kalshi -> hedge-poly).

Production evidence:
  (a) 11:11Z Golubic — rest Kalshi 54sh @37c hedged Poly @65c = $55.08 for a $54 payout = -$1.08
      (-2.00%), yet alerted "profit is GUARANTEED".
  (b) 15:10Z Golubic — rest Kalshi 25sh @79c hedged Poly @21c = $25.00 for $25.00 = 0.00%.
  (c) Control 14:43Z Draper rest-POLY @55c hedged Kalshi @42c = +1.28% — correct.

The quote-time math (compute_quote) is symmetric and correct; the failures were (1) the fill-time
pre-hedge gate passing on an optimistic/stale hedge-book walk while the actual FAK swept to a worse
price (adverse selection), and (2) the booking/alert celebrating a correctly-computed NEGATIVE net as
"GUARANTEED". These tests pin the shared pre-hedge gate, the LOCKED-only-when-profit alert guard, the
over-fill accounting, and the daily-caps persistence.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.genz.maker_rt import alerts
from src.genz.maker_rt import config as mrt_config
from src.genz.maker_rt.caps import LiveCaps
from src.genz.maker_rt.hedge import locked_net, mark_hedge
from src.genz.maker_rt.pregame_exec import HEDGE_DECLINE_FLOOR, PregameLiveExecutor
from src.genz.maker_rt.quotes import hedge_taker_fee

from .test_maker_rt_pregame import (_Hedger, _KalshiExec, _KalshiOC, _Poly, _Store, _cand, _cand_kalshi,
                                    _dec, _exec, _exec_kalshi)

_DT = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 1. THE shared pre-hedge gate (pure) — identical decision for both directions #
# --------------------------------------------------------------------------- #
def test_prehedge_gate_declines_below_floor_and_on_dollar_pair():
    d = PregameLiveExecutor._prehedge_declines
    # profitable pair -> hedge
    assert d(0.02, 0.46, 0.50) is False                       # +2%, pair 0.96 -> fire (rest-poly control)
    assert d(0.02, 0.20, 0.75) is False                       # +2%, pair 0.95 -> fire (rest-kalshi ok)
    # below the decline floor -> DO NOT hedge (either direction)
    assert d(HEDGE_DECLINE_FLOOR - 0.001, 0.37, 0.63) is True
    assert d(-0.05, 0.37, 0.65) is True                       # (a) walked accurately -> decline
    # rest + hedge >= $1.00/share == guaranteed loss even if the (optimistic) net cleared the floor
    assert d(0.005, 0.37, 0.65) is True                       # pair 1.02 -> decline despite +net estimate
    assert d(0.0, 0.79, 0.21) is True                         # (b) pair 1.00 -> decline
    # no readable hedge book -> can't prove a profit -> decline
    assert d(None, 0.37, 0.65) is True


def test_pair_is_profit_pure():
    p = PregameLiveExecutor._pair_is_profit
    assert p(0.46, 0.50, 0.02) is True                        # +2%, pair 0.96
    assert p(0.37, 0.65, -0.02) is False                      # (a) loss
    assert p(0.79, 0.21, 0.0) is False                        # (b) zero net
    assert p(0.50, 0.50, 0.05) is False                       # pair exactly $1.00 -> not a profit
    assert p(0.20, 0.75, None) is False                       # unknown net -> not a profit


def _decline_case(tmp_path, direction):
    """A fill whose walked hedge is clearly sub-floor must DECLINE + unwind + log hedge_declined — via the
    SAME code path — for rest-poly (hedge kalshi) AND rest-kalshi (hedge poly)."""
    if direction == "rest-poly":
        poly = _Poly(order_status="CANCELED", sell_price=0.30)
        poly.position = 0.0
        ex, _ = _exec(tmp_path, hedger=_Hedger(SimpleNamespace(status="locked"), poly=poly), poly=poly)
        c = _cand()
        store = _Store(poly_best_ask=0.60, kalshi_ask=0.66)   # hedge kalshi 0.66 -> 1-0.46-0.66 << floor
        ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.66), None, store, _DT, 1.0, "pre")
        oid = ex.order_client.rests[0]["oid"]
        ex.on_order_update({"order_id": oid, "size_matched": 5, "price": 0.46}, store, _DT, 2.0)
    else:  # rest-kalshi -> hedge poly
        koc = _KalshiOC(); kex = _KalshiExec()
        poly = _Poly(); poly.position = 0.0
        ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kex, poly=poly,
                             hedger=_Hedger(SimpleNamespace(status="locked"), poly=poly))
        c = _cand_kalshi(ticker="KX-G", htoken="TOKV")
        store = _Store(poly_best_ask=0.66, kalshi_ask=0.60)   # hedge poly 0.66 -> 1-0.46-0.66 << floor
        ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.66), None, store, _DT, 1.0, "pre")
        oid = koc.rests[0]["oid"]
        ex.on_kalshi_fill({"order_id": oid, "count": 5}, store, _DT, 2.0)
    return ex


def test_prehedge_decline_unwinds_both_directions(tmp_path):
    for direction in ("rest-poly", "rest-kalshi"):
        ex = _decline_case(tmp_path, direction)
        rows = [r["event"] for r in ex.state.rows]
        assert "hedge_declined" in rows, f"{direction}: a sub-floor hedge must DECLINE, not lock"
        assert "hedge_locked" not in rows, f"{direction}: must not book a locked (losing) hedge"


# --------------------------------------------------------------------------- #
# 2. LOCKED alert only when profit; a >= $1/share pair is a red ERROR, not GUARANTEED
# --------------------------------------------------------------------------- #
def test_losing_hedge_alerts_error_not_guaranteed(tmp_path):
    """Replay (a): the pre-hedge walk is optimistic (poly ask 0.62 -> passes), the FAK actually fills at
    0.65 (a -2% pair), the complement confirms fully hedged. It must book the loss and alert a red ERROR
    — NEVER 'profit is GUARANTEED'."""
    sent = []
    koc = _KalshiOC(); kex = _KalshiExec()
    poly = _Poly(); poly.position = 54.0                      # venue confirms the hedge is fully held
    hedger = _Hedger(SimpleNamespace(status="missed", hedged_shares=0, hedge_avg_price=0.65,
                                     hedge_fee=None, locked_pnl=None, unwind_cost=None,
                                     detail={"poly": {"order_id": "0xg"}}), poly=poly)
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kex, poly=poly, hedger=hedger)
    ex.telegram = sent.append
    ex.digest_min = 0.0
    ex.caps.quote_usd_max = 20.0                              # 20/0.37 ~= 54 shares
    ex.roll_day(_DT)
    store = _Store(poly_best_ask=0.62, kalshi_ask=0.60)       # optimistic pre-hedge walk (0.62) passes the gate
    c = _cand_kalshi(ticker="KX-G", htoken="TOKV")
    ex.place_or_reprice(c, _dec(0.37, hedge_ask=0.62), None, store, _DT, 100.0, "inplay")
    oid = koc.rests[0]["oid"]
    ex.on_kalshi_fill({"order_id": oid, "count": 54}, store, _DT, 101.0)
    # booked as a LOSS
    row = next(r for r in ex.state.rows if r["event"] == "hedge_locked")
    assert float(row["realized_pnl_usd"]) < 0, "the losing pair must book a negative pnl"
    assert ex.caps.pnl_today < 0
    # alert is a red ERROR, never GUARANTEED
    lock_alert = next(m for m in sent if "ERROR" in m or "LOCKED" in m or "GUARANTEED" in m)
    assert "🔴 ERROR" in lock_alert and "HEDGED AT A LOSS" in lock_alert
    assert "GUARANTEED" not in lock_alert


def test_profitable_hedge_still_alerts_locked(tmp_path):
    """The control path: a genuine profit (pair < $1, net > 0) still alerts the green LOCKED/GUARANTEED."""
    sent = []
    oc_poly = _Poly(order_status="CANCELED")
    hedger = _Hedger(SimpleNamespace(status="locked", hedged_shares=5, hedge_avg_price=0.50,
                                     hedge_fee=0.01, locked_pnl=0.18, unwind_cost=None))
    ex, _ = _exec(tmp_path, hedger=hedger, poly=oc_poly)
    ex.telegram = sent.append
    ex.digest_min = 0.0
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    ex.place_or_reprice(_cand(), _dec(0.46, hedge_ask=0.50), None, store, _DT, 1.0, "pre")
    ex.on_order_update({"order_id": ex.order_client.rests[0]["oid"], "size_matched": 5, "price": 0.46},
                       store, _DT, 2.0)
    assert any("✅ LOCKED" in m and "GUARANTEED" in m for m in sent)
    assert not any("🔴 ERROR" in m for m in sent)


def test_alert_locked_loss_format():
    msg = alerts.format_event("locked_loss", sport="tennis", teams="Katie Volynets vs Viktorija Golubic",
                              market_key="match_winner", side="golubic", venue="kalshi",
                              hedge_venue="polymarket", rest_price=0.37, rest_shares=54, hedge_price=0.65,
                              hedge_shares=54, pnl=-1.08, net_pct=-2.0)
    assert "🔴 ERROR" in msg and "HEDGED AT A LOSS" in msg and "GUARANTEED" not in msg
    assert "-$1.08" in msg and "$1.02/share" in msg           # the exact (a) numbers + the >=$1 warning


# --------------------------------------------------------------------------- #
# 3. quote-time edge math regression from (a) and (b)'s exact numbers          #
# --------------------------------------------------------------------------- #
def test_quote_time_math_is_negative_for_the_loss_cases():
    # (a) rest kalshi 0.37 + hedge poly 0.65 (+ poly taker fee) -> clearly negative net.
    fee_a = hedge_taker_fee("polymarket", 0.65, 0.05)
    net_a = locked_net(0.37, 0.65 + fee_a)
    assert net_a < -0.01, f"(a) must be a loss, got {net_a}"
    # (b) rest kalshi 0.79 + hedge poly 0.21 (+ fee) -> negative (pair is exactly $1 before fees).
    fee_b = hedge_taker_fee("polymarket", 0.21, 0.05)
    net_b = locked_net(0.79, 0.21 + fee_b)
    assert net_b < 0, f"(b) must be <= 0, got {net_b}"
    # (c) control: rest poly 0.55 + hedge kalshi 0.42 (+ kalshi fee) -> POSITIVE (the correct direction).
    fee_c = hedge_taker_fee("kalshi", 0.42, 0.05)
    net_c = locked_net(0.55, 0.42 + fee_c)
    assert net_c > 0.005, f"(c) control must be a profit, got {net_c}"


def test_mark_hedge_walk_matches_the_loss_case():
    """Walking the poly hedge ladder for 54 shares at 0.65 gives a cost/share that makes (a) a loss."""
    rm = mark_hedge([(0.65, 500)], 54, "polymarket", 0.05)
    assert rm is not None and rm["avg_price"] == pytest.approx(0.65)
    assert locked_net(0.37, rm["cost_per_share"]) < -0.01     # the shared gate would DECLINE this


# --------------------------------------------------------------------------- #
# 4. hedge over-fill accounting — register the ACTUAL venue-held shares         #
# --------------------------------------------------------------------------- #
def test_hedge_overfill_registers_actual_and_does_not_orphan(tmp_path):
    """A $-sized Poly hedge sweep fills MORE shares than requested (82.59 held vs 79 paired). The expected
    position must register the ACTUAL 82.59 held (not the clamped 79), so the next reconcile sees it
    explained — no orphan (the 2026-07-27 19:09 halt)."""
    koc = _KalshiOC(); kex = _KalshiExec()
    poly = _Poly(); poly.position = 82.59                     # venue truth: the hedge over-filled
    hedger = _Hedger(SimpleNamespace(status="missed", hedged_shares=0, hedge_avg_price=0.75,
                                     hedge_fee=None, locked_pnl=None, unwind_cost=None,
                                     detail={"poly": {"order_id": "0xh"}}), poly=poly)
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kex, poly=poly, hedger=hedger)
    ex.caps.quote_usd_max = 20.0
    ex.roll_day(_DT)
    store = _Store(poly_best_ask=0.20, kalshi_ask=0.60)       # cheap hedge -> pre-gate passes (a profit)
    c = _cand_kalshi(ticker="KX-G", htoken="TOKV")
    ex.place_or_reprice(c, _dec(0.20, hedge_ask=0.20), None, store, _DT, 100.0, "inplay")
    oid = koc.rests[0]["oid"]
    ex.on_kalshi_fill({"order_id": oid, "count": 79}, store, _DT, 101.0)
    # the ACTUAL held complement is registered, not the paired 79
    assert ex._expected_shares("polymarket", "TOKV") == pytest.approx(82.59)
    # ...so a reconcile against the same venue holding sees it fully explained -> no orphan
    ex._traded_tokens.add("TOKV")
    assert ex.reconcile_positions(_DT) is None and ex.caps.halted is False


# --------------------------------------------------------------------------- #
# 5. daily caps persist across a mid-day restart                                #
# --------------------------------------------------------------------------- #
def test_daily_caps_persist_and_restore_same_day(tmp_path):
    ex1, _ = _exec(tmp_path)
    ex1.caps.commit_stake(137.50)
    ex1.caps.on_fill(-0.42)                                   # fills_today 1, pnl -0.42
    ex1.persist_daily_caps()
    assert os.path.exists(mrt_config.runtime_path("daily_caps"))
    # a fresh executor over the SAME ops dir (a restart) restores today's committed budget.
    ex2, _ = _exec(tmp_path)
    assert ex2.caps.stake_today == pytest.approx(137.50)
    assert ex2.caps.fills_today == 1 and ex2.caps.pnl_today == pytest.approx(-0.42)
    # the first midnight-roll of the SAME day must NOT wipe the restored counters.
    ex2.roll_day(_DT)
    assert ex2.caps.stake_today == pytest.approx(137.50) and ex2.caps.fills_today == 1


def test_hedged_lifetime_reported_separately_from_untracked(tmp_path):
    """A hedged settled trade and an UNTRACKED naked windfall both land in settled_pnl_lifetime, but the
    HEDGED-only number (what the maker's edge actually earns) excludes the untracked luck."""
    from src.genz.maker_rt.settle import SettledLeg, settled_row
    from src.genz.maker_rt.state import MakerState, utcnow
    st = MakerState()
    now = utcnow()
    # a real hedged pair (kalshi leg won, poly leg lost): +$0.15 net on $9.85 cost
    hedged = settled_row(sport="tennis", game="G", market_key="match_winner", settled_ts="t",
                         legs=[SettledLeg("kalshi", "K", "yes", 10, 4.0, 10.0),
                               SettledLeg("polymarket", "P", "buy", 10, 5.85, 0.0)])
    st.record(hedged, now)
    # the UFC naked windfall: +$42 untracked
    ufc = settled_row(sport="ufc", game="U", market_key="fight_winner", settled_ts="t", untracked=True,
                      legs=[SettledLeg("kalshi", "UK", "yes", 140, 98.0, 140.0)])
    st.record(ufc, now)
    assert st.settled_pnl_lifetime == pytest.approx(42.15)
    assert st.settled_pnl_untracked_lifetime == pytest.approx(42.0)
    hb = st.heartbeat("live", {}, 0, now)
    assert hb["settled_pnl_hedged_lifetime"] == pytest.approx(0.15)
    assert hb["settled_pnl_untracked_lifetime"] == pytest.approx(42.0)


def test_daily_caps_prior_day_snapshot_is_ignored(tmp_path):
    import json
    path = mrt_config.runtime_path("daily_caps")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"day": "20200101", "stake_today": 999.0, "fills_today": 9, "pnl_today": -9.0}, fh)
    ex, _ = _exec(tmp_path)                                   # a prior-day snapshot must NOT restore
    assert ex.caps.stake_today == 0.0 and ex.caps.fills_today == 0 and ex.caps.pnl_today == 0.0
