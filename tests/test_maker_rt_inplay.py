"""In-play shadow collection tests for src/genz/maker_rt/ — the anti-phantom rails, per-sport/phase
reporting, achievable-net ladder, LiveGate in-play refusal, universe horizon, and CSV phase column.

CONFIG-PIN BAN: every config here is built from the DATACLASS DEFAULTS (MakerRtConfig()) or an empty
fixture — NEVER the live config.yaml, whose tunables drift.
"""
from __future__ import annotations

import csv as _csv
import os
from datetime import datetime, timezone

import pytest

from src.genz.maker_rt import config as mrt_config
from src.genz.maker_rt import live, parsing, quotes
from src.genz.maker_rt.driver import QuoteDriver
from src.genz.maker_rt.state import MakerState, _Bucket
from src.genz.maker_rt.store import BookStore
from src.genz.maker_rt.universe import _kickoff_ts, build_universe

_KO_ISO = "2026-07-17T20:00:00Z"
_KO_TS = _kickoff_ts(_KO_ISO)
_NOW = datetime(2026, 7, 17, 20, 1, 0, tzinfo=timezone.utc)   # a wall-clock stamp for the CSV/log


class _RecState:
    def __init__(self):
        self.rows = []
        self.achv = []

    def record(self, row, now):
        self.rows.append(row)

    def record_achievable(self, sport, phase, value, now):
        self.achv.append((sport, phase, value))


class _CapLog:
    def __init__(self):
        self.infos = []

    def info(self, fmt, *a):
        self.infos.append(fmt % a if a else fmt)

    def warning(self, fmt, *a):
        self.infos.append(fmt % a if a else fmt)


class _OkClient:
    def get_balance(self):
        return {"balance": 1000}

    def can_place_polymarket_orders(self):
        return (True, "ok")


def _ip_cfg():
    """The in-play rails at their DATACLASS defaults (fresh 10s, shock 0.05/10s, freeze 30s, persist
    1500ms) — never read from config.yaml."""
    return mrt_config.MakerRtConfig()


def _ml_tree(kickoff, game="MLB-G"):
    n = lambda side, tok, kt, ks: {  # noqa: E731
        "market_type": "ml2", "market_key": "ml2", "side": side, "line": None, "kind": "2way",
        "poly_token_id": tok, "poly_side": side.title(), "poly_fee_rate": 0.05,
        "kalshi_ticker": kt, "kalshi_side": ks}
    return {"games": {game: {"away": "A", "home": "B", "kickoff_utc": kickoff, "sport": "mlb",
            "nodes": [n("away", "T_A", "KX-1", "YES"), n("home", "T_B", "KX-1", "NO")]}}}


def _ml_books(store, now_ts, *, poly_bid="0.45", no_bid="0.5000"):
    """Rest-poly on T_A (bid poly_bid); hedge = lift the Kalshi NO complement. Freshness stamped now_ts."""
    store.apply_poly(parsing.parse_poly_market({"event_type": "book", "asset_id": "T_A",
        "bids": [{"price": poly_bid, "size": "300"}], "asks": [{"price": "0.55", "size": "300"}]}), now_ts)
    store.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
        "msg": {"market_ticker": "KX-1", "yes_dollars_fp": [["0.5000", "5000"]],
                "no_dollars_fp": [[no_bid, "5000"]]}}), now_ts)


# --------------------------------------------------------------------------- #
# Universe horizon + gap                                                         #
# --------------------------------------------------------------------------- #
def test_universe_admits_inplay_within_horizon_drops_beyond():
    trees = {"mlb": _ml_tree(_KO_ISO)}
    hz = {"mlb": 4.5}
    assert len(build_universe(trees, _KO_TS + 60, max_games=20, horizon_hours=hz)) == 1   # 1 min in -> admitted
    assert build_universe(trees, _KO_TS + 5 * 3600, max_games=20, horizon_hours=hz) == []  # 5h in -> dropped
    assert build_universe(trees, _KO_TS + 60, max_games=20) == []   # no horizon map -> pre-in-play (drop at kickoff)


def test_driver_gap_phase_places_no_quotes():
    """In the 120s gap (kickoff-120 .. kickoff) a candidate is disarmed and NEVER quotes."""
    uni = build_universe({"mlb": _ml_tree(_KO_ISO)}, _KO_TS - 60, max_games=20, horizon_hours={"mlb": 4.5})
    st = _RecState(); drv = QuoteDriver(_ip_cfg(), st); drv.set_universe(uni)
    store = BookStore(); _ml_books(store, _KO_TS - 60)
    assert drv.phase(_KO_TS, _KO_TS - 60) == "gap"
    drv.refresh_quotes(store, _NOW, _KO_TS - 60)
    assert not any(r["event"] == "quote" for r in st.rows)


# --------------------------------------------------------------------------- #
# Anti-phantom rails                                                             #
# --------------------------------------------------------------------------- #
def test_inplay_fresh_veto_no_quote_on_stale_book():
    """RAIL (a): a book older than fresh_s vetoes the in-play quote."""
    uni = build_universe({"mlb": _ml_tree(_KO_ISO)}, _KO_TS + 60, max_games=20, horizon_hours={"mlb": 4.5})
    st = _RecState(); drv = QuoteDriver(_ip_cfg(), st); drv.set_universe(uni)
    store = BookStore(); _ml_books(store, _KO_TS - 100)          # stamped 160s before 'now' -> STALE
    drv.refresh_quotes(store, _NOW, _KO_TS + 60)
    assert not any(r["event"] == "quote" for r in st.rows)


def test_inplay_fresh_books_quote_arms_after_persistence():
    """RAIL (c): fresh books + rails satisfied -> a quote arms, but ONLY after persist_ms of continuous
    viability."""
    uni = build_universe({"mlb": _ml_tree(_KO_ISO)}, _KO_TS + 60, max_games=20, horizon_hours={"mlb": 4.5})
    st = _RecState(); drv = QuoteDriver(_ip_cfg(), st); drv.set_universe(uni)
    store = BookStore(); t0 = _KO_TS + 60
    _ml_books(store, t0); drv.refresh_quotes(store, _NOW, t0)    # first viable tick -> timer starts
    assert not any(r["event"] == "quote" for r in st.rows)      # persist_ms=1500 not yet elapsed
    _ml_books(store, t0 + 2.0); drv.refresh_quotes(store, _NOW, t0 + 2.0)   # 2s later (> 1.5s)
    assert any(r["event"] == "quote" and r["phase"] == "inplay" for r in st.rows)


def test_inplay_shock_freeze_disarm_and_thaw():
    """RAIL (b): a mid move >= shock_move within shock_window freezes the node (disarm + no quotes);
    after freeze_s it thaws and can quote again."""
    uni = build_universe({"mlb": _ml_tree(_KO_ISO)}, _KO_TS + 60, max_games=20, horizon_hours={"mlb": 4.5})
    cfg = _ip_cfg(); cfg.inplay.persist_ms = 0                   # isolate the shock rail from persistence
    st = _RecState(); log = _CapLog(); drv = QuoteDriver(cfg, st, log=log); drv.set_universe(uni)
    store = BookStore(); t0 = _KO_TS + 60
    store.apply_poly(parsing.parse_poly_market({"event_type": "book", "asset_id": "T_A",
        "bids": [{"price": "0.45", "size": "300"}], "asks": [{"price": "0.55", "size": "300"}]}), t0)
    # Two Kalshi snapshots 1s apart with the YES mid jumping 0.50 -> 0.60 (>= shock_move) within the window.
    store.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
        "msg": {"market_ticker": "KX-1", "yes_dollars_fp": [["0.5000", "5000"]],
                "no_dollars_fp": [["0.5000", "5000"]]}}), t0)         # yes mid 0.50
    store.apply_kalshi(parsing.parse_kalshi({"type": "orderbook_snapshot", "sid": 1, "seq": 2,
        "msg": {"market_ticker": "KX-1", "yes_dollars_fp": [["0.6000", "5000"]],
                "no_dollars_fp": [["0.4000", "5000"]]}}), t0 + 1)     # yes mid 0.60 -> +0.10 shock
    drv.refresh_quotes(store, _NOW, t0 + 1)
    assert any("FREEZE" in r for r in log.infos)                 # shock logged
    assert not any(r["event"] == "quote" for r in st.rows)      # frozen -> no quotes
    assert drv.freeze_until.get(("mlb", "MLB-G", "ml2"), 0) > t0 + 1
    _ml_books(store, t0 + 6); st.rows.clear(); drv.refresh_quotes(store, _NOW, t0 + 6)
    assert not any(r["event"] == "quote" for r in st.rows)      # still frozen 5s later
    _ml_books(store, t0 + 40); drv.refresh_quotes(store, _NOW, t0 + 40)
    _ml_books(store, t0 + 41); st.rows.clear(); drv.refresh_quotes(store, _NOW, t0 + 41)
    assert any(r["event"] == "quote" for r in st.rows)          # thawed after freeze_s (persist 0)


def test_pre_phase_unaffected_by_inplay_rails():
    """A PRE-game candidate quotes normally regardless of book freshness (rails are in-play only)."""
    uni = build_universe({"mlb": _ml_tree("2027-01-01T00:00:00Z")}, 0.0, max_games=20, horizon_hours={"mlb": 4.5})
    st = _RecState(); drv = QuoteDriver(_ip_cfg(), st); drv.set_universe(uni)
    store = BookStore(); _ml_books(store, 0.0)                   # "stale" by wall-clock, but phase is pre
    drv.refresh_quotes(store, _NOW, 100.0)
    assert any(r["event"] == "quote" and r["phase"] == "pre" for r in st.rows)


# --------------------------------------------------------------------------- #
# LiveGate in-play refusal                                                       #
# --------------------------------------------------------------------------- #
def test_live_gate_refuses_inplay_even_when_armed(tmp_path):
    arm = tmp_path / "ARM_MAKER"; arm.write_text("1")
    cfg = mrt_config.MakerRtConfig(); cfg.live.enabled = True; cfg.live.arm_file = str(arm)
    gate = live.LiveGate(cfg, kalshi_client=_OkClient(), poly_client=_OkClient())
    assert gate.evaluate().armed is True                        # the gate IS armed
    assert gate.may_place("pre") is True                        # pre-game live would be allowed
    assert gate.may_place("inplay") is False                    # in-play refused even armed
    assert live.is_inplay("inplay") is True and live.is_inplay("pre") is False
    with pytest.raises(AssertionError):
        live.assert_live_allowed("inplay")
    live.assert_live_allowed("pre")                             # pre never raises


# --------------------------------------------------------------------------- #
# Achievable-net ladder + by_sport/by_phase + CSV phase                          #
# --------------------------------------------------------------------------- #
def test_achievable_net_formula():
    v = quotes.achievable_net(0.45, 0.50, "kalshi")
    assert abs(v - (1 - 0.45 - 0.50 - 0.0175)) < 1e-9
    vp = quotes.achievable_net(0.44, 0.50, "polymarket", 0.05)
    assert abs(vp - (1 - 0.44 - 0.50 - 0.05 * 0.50)) < 1e-9
    assert quotes.achievable_net(None, 0.5, "kalshi") is None
    assert quotes.achievable_net(0.5, None, "kalshi") is None


def test_achievable_ladder_percentiles_and_thresholds():
    import random as _r
    b = _Bucket(); rng = _r.Random(0)
    for v in (-0.02, -0.01, 0.0, 0.003, 0.006, 0.012):          # edge FRACTIONS
        b.add_achievable(v, rng)
    a = b.achievable()
    assert a["n"] == 6
    assert a["share_ge_0"] == round(4 / 6, 4)                   # 0.0, 0.003, 0.006, 0.012
    assert a["share_ge_25bp"] == round(3 / 6, 4)               # >= 0.0025
    assert a["share_ge_50bp"] == round(2 / 6, 4)              # >= 0.0050
    assert a["share_ge_100bp"] == round(1 / 6, 4)            # >= 0.0100
    assert a["p50"] is not None and a["p90"] is not None


def test_summary_by_sport_and_phase_math():
    st = MakerState()
    now = datetime(2026, 7, 17, 20, 0, 0, tzinfo=timezone.utc)
    st.record({"event": "quote", "sport": "mlb", "phase": "pre", "at_best": True}, now)
    st.record({"event": "quote", "sport": "mlb", "phase": "pre", "at_best": False}, now)
    st.record({"event": "fill", "sport": "mlb", "phase": "pre", "locked_net": 1.2, "locked_pnl": 0.6}, now)
    st.record({"event": "quote", "sport": "ufc", "phase": "inplay", "at_best": True}, now)
    st.record({"event": "behind", "sport": "ufc", "phase": "inplay"}, now)
    st.record_achievable("mlb", "pre", 0.004, now)
    summ = st.summary("shadow", {}, now)
    assert summ["schema"] == 2
    assert summ["by_sport"]["mlb"]["quotes"] == 2 and summ["by_sport"]["mlb"]["fills"] == 1
    assert summ["by_sport"]["mlb"]["at_best_share"] == 0.5
    assert summ["by_sport"]["ufc"]["quotes"] == 1 and summ["by_sport"]["ufc"]["behind_best"] == 1
    assert summ["by_phase"]["pre"]["quotes"] == 2 and summ["by_phase"]["inplay"]["quotes"] == 1
    assert summ["by_sport"]["mlb"]["achievable"]["n"] == 1
    assert summ["by_sport"]["mlb"]["achievable"]["share_ge_25bp"] == 1.0


def test_events_csv_has_phase_column(tmp_path, monkeypatch):
    from src.genz.maker_rt import config as mc
    monkeypatch.setattr(mc, "events_path_for", lambda day: os.path.join(str(tmp_path), f"maker_rt_{day}.csv"))
    st = MakerState()
    now = datetime(2026, 7, 17, 20, 0, 0, tzinfo=timezone.utc)
    st.record({"event": "quote", "sport": "mlb", "phase": "inplay", "at_best": True}, now)
    rows = list(_csv.DictReader(open(os.path.join(str(tmp_path), "maker_rt_20260717.csv"))))
    assert "phase" in rows[0] and rows[0]["phase"] == "inplay" and rows[0]["sport"] == "mlb"


def test_driver_achievable_sample_throttled_to_one_per_minute():
    """The driver accumulates achievable on EVERY eval but writes at most 1 achievable_sample CSV row
    per minute per market."""
    uni = build_universe({"mlb": _ml_tree("2027-01-01T00:00:00Z")}, 0.0, max_games=20, horizon_hours={"mlb": 4.5})
    st = _RecState(); drv = QuoteDriver(_ip_cfg(), st); drv.set_universe(uni)
    store = BookStore(); _ml_books(store, 0.0)
    for i in range(10):                                          # 10 refreshes within the same minute
        drv.refresh_quotes(store, _NOW, 100.0 + i)
    samples = [r for r in st.rows if r["event"] == "achievable_sample"]
    assert 1 <= len(samples) <= 2                               # <=1 per market (2 candidates share node3)
    assert len(st.achv) >= 10                                   # accumulated on every eval
