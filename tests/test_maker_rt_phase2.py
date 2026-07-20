"""maker_rt PHASE 2 — the measurement fixes + the LOCKED in-play live executor.

Covers: the paired fill_drift writer (fake clock + delta math), the blank-fill artifact guard, the
rails_ok achievable gating math, and the InplayLiveExecutor (armed only via its OWN gate, cool-off
timer, hedge re-verify decline -> unwind, one-in-flight shared with pre-game, daily auto-halt, fill
cap).

CONFIG-PIN BAN: every config is built from MakerRtConfig() dataclass defaults, NEVER config.yaml.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from src.genz.maker_rt import config as mrt_config
from src.genz.maker_rt import parsing
from src.genz.maker_rt.books import SideView
from src.genz.maker_rt.driver import Candidate, QuoteDriver
from src.genz.maker_rt.fills import FillEvent
from src.genz.maker_rt.hedge import HedgeResult
from src.genz.maker_rt.inplay_exec import InFlightGuard, InplayLiveExecutor
from src.genz.maker_rt.live import LiveGate
from src.genz.maker_rt.state import MakerState
from src.genz.maker_rt.store import BookStore
from src.genz.maker_rt.universe import _kickoff_ts, build_universe

NOW = datetime(2026, 7, 20, 20, 1, 0, tzinfo=timezone.utc)


class _RecState:
    def __init__(self):
        self.rows = []

    def record(self, row, now):
        self.rows.append(row)

    def record_achievable(self, sport, phase, value, now, rails_ok=True):
        pass


class _CapLog:
    def __init__(self):
        self.msgs = []

    def info(self, fmt, *a):
        self.msgs.append(fmt % a if a else fmt)

    def warning(self, fmt, *a):
        self.msgs.append(fmt % a if a else fmt)


# --------------------------------------------------------------------------- #
# 1a) DRIFT WRITER — paired fill_drift, fake clock, adverse-selection deltas     #
# --------------------------------------------------------------------------- #
def _ml_tree(kickoff):
    n = lambda side, tok, kt, ks: {  # noqa: E731
        "market_type": "ml2", "market_key": "ml2", "side": side, "line": None, "kind": "2way",
        "poly_token_id": tok, "poly_side": side.title(), "poly_fee_rate": 0.05,
        "kalshi_ticker": kt, "kalshi_side": ks}
    return {"games": {"G1": {"away": "A", "home": "B", "kickoff_utc": kickoff, "sport": "mlb",
            "nodes": [n("away", "TOK_A", "KX-1", "YES"), n("home", "TOK_B", "KX-1", "NO")]}}}


def _kalshi_snapshot(store, ts, *, yes_top, no_top, seq=1):
    store.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_snapshot", "sid": 1, "seq": seq,
        "msg": {"market_ticker": "KX-1", "yes_dollars_fp": [[yes_top, "5000"]],
                "no_dollars_fp": [[no_top, "5000"]]}}), ts)


def test_fill_drift_paired_event_with_fake_clock_and_deltas():
    """A shadow fill schedules +1/5/30s hedge-mid reads; once the last mark is due a PAIRED fill_drift
    event is emitted, linked by fill_ts, with drift = mid(t) - mid_at_fill. Driven by an explicit clock
    (no wall time); the hedge mid is moved before the 30s mark so drift_30 is a real non-zero delta."""
    uni = build_universe({"mlb": _ml_tree("2027-01-01T00:00:00Z")}, 0.0, max_games=20)
    st = _RecState(); drv = QuoteDriver(mrt_config.MakerRtConfig(), st); drv.set_universe(uni)
    bs = BookStore()
    bs.apply_poly(parsing.parse_poly_market({"event_type": "book", "asset_id": "TOK_A",
        "bids": [{"price": "0.45", "size": "300"}], "asks": [{"price": "0.55", "size": "300"}]}), 100.0)
    _kalshi_snapshot(bs, 100.0, yes_top="0.5000", no_top="0.4500")   # hedge = NO; mid0 = (0.45 + 0.50)/2 = 0.475
    drv.refresh_quotes(bs, NOW, 100.0)
    drv.consume_prints([(("polymarket", "TOK_A", "BUY"), 0.44, 10.0)], bs, NOW, 100.0)   # fill (traded through)
    assert any(r["event"] == "fill" for r in st.rows)

    drv.process_drift(bs, NOW, 101.5)                               # age 1.5 -> mark 1 = 0.475 (unchanged)
    drv.process_drift(bs, NOW, 106.0)                              # age 6 -> mark 5 = 0.475
    _kalshi_snapshot(bs, 130.0, yes_top="0.4000", no_top="0.4500", seq=2)   # YES ask -> 0.60; mid -> 0.525
    assert not any(r["event"] == "fill_drift" for r in st.rows)     # nothing emitted before the LAST mark
    drv.process_drift(bs, NOW, 131.0)                              # age 31 -> mark 30 = 0.525 -> emit

    fd = [r for r in st.rows if r["event"] == "fill_drift"]
    assert len(fd) == 1
    row = fd[0]
    assert row["game"] == "G1" and row["market_key"] == "ml2" and row["fill_ts"]      # paired to the fill
    assert abs(row["drift_1"] - 0.0) < 1e-6 and abs(row["drift_5"] - 0.0) < 1e-6      # hedge unmoved early
    assert abs(row["drift_30"] - 0.05) < 1e-6                                          # +0.05 adverse by 30s


def test_flush_drift_emits_partial_on_shutdown():
    """A HEAD-change/restart before the 30s mark must NOT drop the fill's drift: flush_drift emits a
    partial fill_drift (marks reached so far) tagged reason='partial'."""
    uni = build_universe({"mlb": _ml_tree("2027-01-01T00:00:00Z")}, 0.0, max_games=20)
    st = _RecState(); drv = QuoteDriver(mrt_config.MakerRtConfig(), st); drv.set_universe(uni)
    bs = BookStore()
    bs.apply_poly(parsing.parse_poly_market({"event_type": "book", "asset_id": "TOK_A",
        "bids": [{"price": "0.45", "size": "300"}], "asks": [{"price": "0.55", "size": "300"}]}), 100.0)
    _kalshi_snapshot(bs, 100.0, yes_top="0.5000", no_top="0.4500")
    drv.refresh_quotes(bs, NOW, 100.0)
    drv.consume_prints([(("polymarket", "TOK_A", "BUY"), 0.44, 10.0)], bs, NOW, 100.0)
    drv.process_drift(bs, NOW, 102.0)                              # only mark 1 reached
    drv.flush_drift(NOW)                                           # shutdown before marks 5/30
    fd = [r for r in st.rows if r["event"] == "fill_drift"]
    assert len(fd) == 1 and fd[0]["reason"] == "partial"
    assert fd[0]["drift_1"] is not None and fd[0]["drift_30"] is None   # partial: last mark never taken
    assert drv.drift_pending == []


# --------------------------------------------------------------------------- #
# 1b) BLANK-FILL ARTIFACT GUARD                                                  #
# --------------------------------------------------------------------------- #
def test_fill_artifact_guard_drops_blank_fill(tmp_path, monkeypatch):
    """A fill row with no game/market (the schema-transition artifact) is WARNed + DROPPED — never
    aggregated (no fills/pnl inflation), never written to the CSV. A well-formed fill is kept."""
    monkeypatch.setattr(mrt_config, "events_path_for",
                        lambda day: os.path.join(str(tmp_path), f"maker_rt_{day}.csv"))
    log = _CapLog(); st = MakerState(log=log)
    st.record({"event": "fill", "sport": "mlb", "phase": "pre", "locked_net": 1.2, "locked_pnl": 0.6}, NOW)
    assert st.n_fills == 0 and st.pnl_today == 0.0                # dropped -> no aggregate impact
    assert any("malformed fill" in m for m in log.msgs)          # WARNING emitted
    csv_path = os.path.join(str(tmp_path), f"maker_rt_{NOW.strftime('%Y%m%d')}.csv")
    assert not os.path.exists(csv_path)                          # nothing written at all
    st.record({"event": "fill", "sport": "mlb", "phase": "pre", "game": "G1", "market_key": "ml2",
               "locked_net": 0.7, "locked_pnl": 0.35}, NOW)
    assert st.n_fills == 1 and abs(st.pnl_today - 0.35) < 1e-9   # a real fill still counts


# --------------------------------------------------------------------------- #
# 1c) RAILS-GATED ACHIEVABLE LADDER                                              #
# --------------------------------------------------------------------------- #
def test_achievable_rails_gating_excludes_failed_from_ladder():
    """Only rails_ok (both-books-fresh AND not-frozen) evaluations feed the summary ladder; rails-failed
    phantom edges are counted in 'gated' and kept OUT of the percentiles/thresholds — the ghost-inflation
    kill. Without gating share_ge_100bp would be 3/5; gated it is 1/3."""
    st = MakerState()
    for v in (0.004, 0.006, 0.012):
        st.record_achievable("mlb", "inplay", v, NOW, rails_ok=True)
    for v in (0.5, 0.9):                                         # stale/frozen phantom edges
        st.record_achievable("mlb", "inplay", v, NOW, rails_ok=False)
    a = st.summary("shadow", {}, NOW)["by_sport"]["mlb"]["achievable"]
    assert a["n"] == 3 and a["gated"] == 2
    assert a["share_ge_100bp"] == round(1 / 3, 4)               # only 0.012 clears 1%; phantoms excluded
    assert a["share_ge_25bp"] == round(3 / 3, 4)


# --------------------------------------------------------------------------- #
# 2) IN-PLAY LIVE EXECUTOR (LOCKED) — gate, cool-off, re-verify, in-flight, halt  #
# --------------------------------------------------------------------------- #
class _OkClient:
    def get_balance(self):
        return {"balance": 1000}

    def can_place_polymarket_orders(self):
        return (True, "ok")


class _Poly:
    def __init__(self):
        self.sold = []

    def place_market_sell(self, token, shares, **kw):
        self.sold.append((token, shares))
        return {"status": "filled", "avg_price": 0.44}


class _HedgerLocks:
    def __init__(self):
        self.poly = _Poly(); self.calls = []

    def hedge(self, fill, hedge):
        self.calls.append((fill, hedge))
        return HedgeResult("locked", hedged_shares=fill["size"], hedge_avg_price=0.50, locked_pnl=2.0)


class _HedgerMiss:
    def __init__(self):
        self.poly = _Poly(); self.calls = []

    def hedge(self, fill, hedge):
        self.calls.append((fill, hedge))
        return HedgeResult("unwound", hedged_shares=0, unwind_cost=3.0, freeze_market=True)


def _armed(tmp_path, *, ip=True):
    cfg = mrt_config.MakerRtConfig()
    arm = tmp_path / "ARM_MAKER_INPLAY"; arm.write_text("1")
    cfg.live_inplay.enabled = ip
    cfg.live_inplay.arm_file = str(arm)
    gate = LiveGate(cfg, kalshi_client=_OkClient(), poly_client=_OkClient())
    return cfg, gate


def _ctx():
    return {"phase": "inplay", "hedge_venue": "kalshi", "poly_rate": 0.05,
            "lookup": {"venue": "kalshi", "ticker": "KX-1", "side": "no"}}


def _fe(price=0.46, size=50.0, token="TOK_A"):
    return FillEvent(key=("mlb", "G1", "ml2", "away", "rest-poly"), quote_price=price, size=size,
                     at_best=True, quote_age_s=1.0, trigger="traded_through", hedge_ctx={}, ts=100.0,
                     rest_ref=("polymarket", token, "BUY"))


def _hv(best_ask, ladder):
    return SideView(best_bid=best_ask - 0.05, best_ask=best_ask, bid_sizes={}, ask_ladder=ladder)


def test_executor_armed_only_via_its_own_inplay_gate(tmp_path):
    cfg, gate = _armed(tmp_path, ip=True)
    assert InplayLiveExecutor(cfg, gate, _HedgerLocks()).armed() is True
    cfg2, gate2 = _armed(tmp_path, ip=False)                    # its flag off -> LOCKED
    assert InplayLiveExecutor(cfg2, gate2, _HedgerLocks()).armed() is False


def test_hedge_reverify_ok_fires_and_records(tmp_path):
    cfg, gate = _armed(tmp_path)
    st = _RecState(); hedger = _HedgerLocks()
    ex = InplayLiveExecutor(cfg, gate, hedger, state=st)
    res = ex.on_fill(_fe(0.46, 50), _ctx(), None, NOW, 100.0, hedge_view=_hv(0.50, [(0.50, 10000)]))
    assert res.action == "hedged" and hedger.calls and ex.fills_today == 1 and ex.pnl_today == 2.0
    assert any(r["event"] == "inplay_live" and r["reason"] == "hedge_locked" for r in st.rows)


def test_hedge_reverify_declines_and_unwinds_below_floor(tmp_path):
    """At the fill, the walked hedge nets below hedge_decline_floor (-1%) -> DECLINE the hedge and
    immediately market-unwind the poly fill ('hedge_declined'); the hedge order is NEVER fired."""
    cfg, gate = _armed(tmp_path)
    st = _RecState(); hedger = _HedgerLocks()
    ex = InplayLiveExecutor(cfg, gate, hedger, state=st)
    res = ex.on_fill(_fe(0.46, 50), _ctx(), None, NOW, 100.0, hedge_view=_hv(0.60, [(0.60, 10000)]))
    assert res.action == "hedge_declined"
    assert hedger.calls == []                                   # hedge NOT fired
    assert hedger.poly.sold == [("TOK_A", 50.0)]               # naked poly fill flattened
    assert ex.fills_today == 0
    assert any(r["reason"] == "hedge_declined" for r in st.rows)


def test_one_in_flight_shared_with_pregame(tmp_path):
    """The in-flight token is SHARED: while the pre-game path holds it, an in-play fill is refused; once
    released, it proceeds."""
    cfg, gate = _armed(tmp_path)
    guard = InFlightGuard(); guard.acquire(("pre", "somekey"))  # pre-game is mid-hedge
    ex = InplayLiveExecutor(cfg, gate, _HedgerLocks(), in_flight=guard)
    assert ex.on_fill(_fe(), _ctx(), None, NOW, 100.0, hedge_view=_hv(0.50, [(0.50, 10000)])).action == "refused_inflight"
    guard.release()
    assert ex.on_fill(_fe(), _ctx(), None, NOW, 100.0, hedge_view=_hv(0.50, [(0.50, 10000)])).action == "hedged"


def test_fill_cap_refuses_after_max(tmp_path):
    cfg, gate = _armed(tmp_path)
    ex = InplayLiveExecutor(cfg, gate, _HedgerLocks())
    ex.fills_today = cfg.live_inplay.max_fills_per_day          # 4
    assert ex.on_fill(_fe(), _ctx(), None, NOW, 100.0, hedge_view=_hv(0.50, [(0.50, 10000)])).action == "refused_cap"


def test_daily_auto_halt_after_loss(tmp_path):
    """Once pnl_today <= -max_daily_loss_usd the executor HALTS: the crossing fill still processes, then
    every subsequent fill is refused for the day."""
    cfg, gate = _armed(tmp_path)
    st = _RecState(); ex = InplayLiveExecutor(cfg, gate, _HedgerMiss(), state=st)   # each miss loses $3
    ex.pnl_today = -19.0                                        # one $3 loss tips past -$20
    res = ex.on_fill(_fe(), _ctx(), None, NOW, 100.0, hedge_view=_hv(0.50, [(0.50, 10000)]))
    assert res.action == "unwound" and ex.pnl_today == -22.0 and ex.halted is True
    res2 = ex.on_fill(_fe(), _ctx(), None, NOW, 100.0, hedge_view=_hv(0.50, [(0.50, 10000)]))
    assert res2.action == "refused_halt"


def test_cooloff_timer(tmp_path):
    """cooloff_ok: never-frozen+fresh -> ok; currently frozen -> no; thawed < cool-off ago -> no; thawed
    >= cool-off + fresh -> ok; a stale book -> no."""
    cfg, gate = _armed(tmp_path)                                # freeze_cooloff_s default 10, fresh_s 10
    ex = InplayLiveExecutor(cfg, gate, _HedgerLocks())
    c = Candidate(key=("mlb", "G1", "ml2", "away", "rest-poly"), sport="mlb", game="G1", market_key="ml2",
                  rest_side="away", direction="rest-poly", rest_ref=("polymarket", "TOK_A", "BUY"),
                  rest_venue="polymarket", hedge_venue="kalshi", tick=0.01, hedge_tick=0.01, poly_rate=0.05,
                  hedge_lookup={"venue": "kalshi", "ticker": "KX-1", "side": "no"}, kickoff_ts=0.0)
    store = BookStore()
    store._touch("TOK_A", 100.0, 0.5); store._touch("KX-1", 100.0, 0.5)   # both fresh at t=100
    assert ex.cooloff_ok(store, c, 0.0, 100.0) is True          # never frozen + fresh
    assert ex.cooloff_ok(store, c, 110.0, 100.0) is False       # currently frozen (freeze until 110)
    assert ex.cooloff_ok(store, c, 95.0, 100.0) is False        # thawed at 95, only 5s ago (< 10 cool-off)
    store._touch("TOK_A", 106.0, 0.5); store._touch("KX-1", 106.0, 0.5)   # refresh both to t=106
    assert ex.cooloff_ok(store, c, 95.0, 106.0) is True         # thawed 11s ago + fresh -> ok
    assert ex.cooloff_ok(store, c, 0.0, 200.0) is False         # books last touched 106 -> stale at 200
