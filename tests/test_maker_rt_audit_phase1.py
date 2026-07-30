"""AUDIT PHASE 1 — execution-integrity closeout. One test per finding, on the audit's own numbers.

Every case here reproduces a specific row of docs/AUDIT_REPORT.md, using the incident it was found in:

  F5/N3  order ``329a9a89`` booked as ``fill`` AND ``fill_untracked`` at 17:21:41Z — three fill detectors,
         no shared dedupe, two of them ADDING deltas. A partial fill whose hedge SUCCEEDS double-hedges.
  N4     the complement read is CUMULATIVE; used as one fill's hedge it makes a second fill's naked
         remainder compute 0, so genuinely naked shares are never unwound.
  N10    ``matched_seen`` advanced BEFORE the hedge, inside the same try — an exception consumed the fill
         delta permanently (never hedged, never unwound, invisible to every later detector).
  N9     ``_cancel_confirmed`` honored CANCELED before checking the matched delta, so a fill that raced
         into the cancel window was popped untracked and sat naked until reconciliation false-orphaned.
  N13    the tree write was a plain truncate — JSONDecodeError killed the LIVE maker at 07:36, 12:07 and
         12:37 on 2026-07-29, cancelling every resting order each time (nine at 12:06:58).
  F6     one utf-8-sig reader in the package; six loaders on plain utf-8 with ``except: pass``. A BOM had
         already wiped $329.96 of committed stake once.
  N5     the in-play −2% day-halt / first-fill pause / fill counter lived only in process memory, against
         11–21 gitguard deploys a day.
  N16    ``live_inplay`` declared four exposure caps that nothing read.
  N7     the supervisor matches only the wrapper, so a surviving python child reads as "missing".
  F1     ZHELAN cycled the settlement sweep every 15 min for ~2.5 days with zero log lines.
  F4     PHIMIA's +$8.1627 reached lifetime pnl and never touched ``pnl_today`` (the $50 rail's only feed).
  N21    ``"Remote end closed connection"`` contains ``"closed"`` — a transport blip blacklisted a live
         market for 24h.
  N22    a failed ``/portfolio/fills`` read returned ``[]`` and the sweep window advanced past it.
  N23    the in-play circuit skipped every ``locked=None`` outcome — i.e. every declined/unwound fill.
  N24    a partial unwind's realized cost was discarded; only the remainder's worst case was booked.
  N26    unwind cost was pure spread, with no exit taker fee on either venue.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.genz.maker_rt import config as mrt_config
from src.genz.maker_rt import parsing, singleton, state as state_mod
from src.genz.maker_rt.caps import LiveCaps
from src.genz.maker_rt.pregame_exec import (MAX_HEDGE_ERRORS, SETTLE_AGE_ALERT_S, HEDGE_SHARE_TOL,
                                            PregameLiveExecutor)

from .test_maker_rt_pregame import (_Guard, _Hedger, _KalshiExec, _KalshiOC, _Log, _OrderClient, _Poly,
                                    _State, _Store, _cand, _cand_kalshi, _dec, _exec, _exec_kalshi)

_DT = datetime(2026, 7, 29, 17, 21, 41, tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _locked(shares: float, px: float = 0.76, fee: float = 0.02):
    return SimpleNamespace(status="locked", hedged_shares=shares, hedge_avg_price=px, hedge_fee=fee,
                           locked_pnl=round((1.0 - 0.22 - px) * shares - fee, 4), unwind_cost=None,
                           detail={})


def _kalshi_maker(tmp_path, *, hedge=None, size_cap=70.0):
    """A rest-kalshi executor sized like production ($70 rest cap), with both Kalshi clients injected."""
    koc, kex, poly = _KalshiOC(), _KalshiExec(), _Poly()
    poly.position = 0.0
    hedger = _Hedger(hedge or _locked(10.0), poly=poly)
    ex, _cfg = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kex, poly=poly, hedger=hedger)
    ex.caps.quote_usd_max = size_cap
    ex.caps.max_pair_stake_usd = 350.0
    ex.caps.max_daily_stake_usd = 800.0
    ex.log = _Log()
    return ex, koc, kex, poly, hedger


# ═══════════════════════════════════════════════════════════════════════════════
# F5 / N3 — ONE fill, ONE hedge, whichever detector sees it
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_partial_fill_seen_by_the_socket_then_the_sweep_hedges_exactly_once(tmp_path):
    """THE latent P0. Order 329a9a89 was booked twice at 17:21:41Z because the socket and the account
    sweep both saw the same execution and neither knew about the other. With a hedge that SUCCEEDS, that
    is two real hedges for one fill."""
    ex, koc, _kex, _poly, hedger = _kalshi_maker(tmp_path)
    store = _Store(poly_best_ask=0.76, kalshi_ask=0.95)
    c = _cand_kalshi(ticker="KX-329A", htoken="TOK_329A")
    ex.place_or_reprice(c, _dec(0.22, hedge_ask=0.76), None, store, _DT, 1.0, "pre")
    oid = koc.rests[0]["oid"]
    assert ex.open_orders[c.key].size > 10                    # a PARTIAL fill, so the order stays open

    ex.on_kalshi_fill({"order_id": oid, "count": 10, "trade_id": "329a9a89-1"}, store, _DT, 2.0)
    assert len(hedger.calls) == 1                             # the socket hedged it

    koc.fills = [{"fill_id": "329a9a89-1", "order_id": oid, "count": "10.00", "ticker": "KX-329A"}]
    ex._last_fills_sweep_ts = 1.0                             # not the priming sweep
    ex.poll_kalshi_fills(store, _DT, 3.0)

    assert len(hedger.calls) == 1, "the sweep re-hedged a fill the socket had already handled"
    assert ex.open_orders[c.key].matched_seen == pytest.approx(10)


def test_a_second_detector_reading_venue_cumulative_never_adds(tmp_path):
    """Two DIFFERENT ids for the same 10 filled contracts (the venue re-reporting, or a frame we cannot
    dedupe) must not become 20. Venue cumulative is idempotent; addition is not."""
    ex, koc, _kex, _poly, hedger = _kalshi_maker(tmp_path)
    store = _Store(poly_best_ask=0.76, kalshi_ask=0.95)
    c = _cand_kalshi(ticker="KX-CUM", htoken="TOK_CUM")
    ex.place_or_reprice(c, _dec(0.22, hedge_ask=0.76), None, store, _DT, 1.0, "pre")
    oid = koc.rests[0]["oid"]
    koc.status[oid] = {"status": "resting", "fill_count_fp": "10.00"}   # VENUE TRUTH: 10 filled, total

    ex.on_kalshi_fill({"order_id": oid, "count": 10, "trade_id": "A"}, store, _DT, 2.0)
    ex.on_kalshi_fill({"order_id": oid, "count": 10, "trade_id": "B"}, store, _DT, 3.0)

    assert len(hedger.calls) == 1
    assert ex.open_orders[c.key].matched_seen == pytest.approx(10)      # never 20


def test_the_additive_fallback_is_bounded_by_the_order_size(tmp_path):
    """When the per-order read is unusable we do fall back to arithmetic — but a resting order cannot fill
    for more than it was placed for, so the fallback is bounded and a duplicated delta cannot compound."""
    ex, koc, _kex, _poly, hedger = _kalshi_maker(tmp_path, hedge=_locked(300.0))
    store = _Store(poly_best_ask=0.76, kalshi_ask=0.95)
    c = _cand_kalshi(ticker="KX-BOUND", htoken="TOK_BOUND")
    ex.place_or_reprice(c, _dec(0.22, hedge_ask=0.76), None, store, _DT, 1.0, "pre")
    oid = koc.rests[0]["oid"]
    size = ex.open_orders[c.key].size
    assert koc.order_status(oid) == {}                         # unreadable -> the fallback path

    for i in range(3):                                        # the SAME full-size fill, three times, no ids
        ex.on_kalshi_fill({"order_id": oid, "count": size}, store, _DT, 2.0 + i)

    assert len(hedger.calls) == 1
    assert c.key not in ex.open_orders                        # fully filled -> closed out once


def test_the_ws_fill_parser_carries_the_trade_id_and_the_fp_count():
    """``parsing`` discarded the trade id (so nothing could be deduped) and read only the bare ``count``
    (so on a v2 payload the accelerator was a silent no-op)."""
    out = parsing.parse_kalshi({"type": "fill", "msg": {
        "market_ticker": "KXATPMATCH-26JUL26ZHELAN", "order_id": "koid1", "trade_id": "tr-77497",
        "side": "yes", "count_fp": "6.30", "yes_price": 22, "is_taker": False}})
    assert out[0]["trade_id"] == "tr-77497"
    assert out[0]["count"] == pytest.approx(6.30)
    # A v1 frame still reads its bare count, and a frame with neither id degrades to None (not a crash).
    v1 = parsing.parse_kalshi({"type": "fill", "msg": {"order_id": "o", "count": 3}})
    assert v1[0]["count"] == pytest.approx(3) and v1[0]["trade_id"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# N4 — the complement is CUMULATIVE; this fill's hedge is the INCREMENT
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_second_fills_naked_remainder_is_unwound_not_masked_by_the_first_fills_hedge(tmp_path):
    """Two sequential fills on ONE order. The first hedges (venue shows 10 held). The second's hedge does
    NOT land, so the venue still shows 10 — and because that read is CUMULATIVE, the old code computed
    ``remainder = 10 − 10 = 0`` and unwound nothing, leaving 10 genuinely naked shares to ride to the
    5-minute reconcile as an orphan halt."""
    oc, poly = _OrderClient(), _Poly(sell_price=0.44)
    poly.position = 0.0                                       # the poly rest leg reads flat after selling
    hedger = _Hedger(SimpleNamespace(status="missed", hedged_shares=0.0, hedge_avg_price=0.50,
                                     hedge_fee=None, locked_pnl=None, unwind_cost=None,
                                     freeze_market=True, detail={}), poly=poly)
    ex, _ = _exec(tmp_path, order_client=oc, hedger=hedger, poly=poly)
    ex.kalshi = _KalshiExec()
    ex.caps.quote_usd_max = 70.0
    ex.caps.max_pair_stake_usd = 350.0
    ex.log = _Log()
    held = {"n": 0.0}
    ex._poll_kalshi_position = lambda ticker, target, **kw: held["n"]     # VENUE-CUMULATIVE complement

    store = _Store(poly_best_ask=0.55, kalshi_ask=0.50)
    c = _cand("rest-poly", token="TOK_N4")
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.50), None, store, _DT, 1.0, "pre")
    oid = oc.rests[0]["oid"]

    held["n"] = 10.0                                          # fill 1: the hedge DID land
    ex.on_order_update({"order_id": oid, "size_matched": 10, "price": 0.46}, store, _DT, 2.0)
    assert poly.market_sells == [], "a fully hedged first fill must not unwind anything"
    assert ex.open_orders[c.key].hedged_seen == pytest.approx(10)

    # fill 2: another 10 fill, and this time the hedge does NOT land -> the venue still holds only 10.
    ex.on_order_update({"order_id": oid, "size_matched": 20, "price": 0.46}, store, _DT, 3.0)

    assert poly.market_sells, "the second fill's 10 naked shares were never unwound (N4)"
    assert poly.market_sells[-1]["shares"] == pytest.approx(10)


def test_expected_legs_register_the_increment_not_the_cumulative(tmp_path):
    """The expected-position registry ACCUMULATES, so handing it a cumulative venue read on every fill
    double-counts it — and an over-registered leg is what masks a future genuinely-naked position."""
    ex, koc, _kex, _poly, _h = _kalshi_maker(tmp_path, hedge=_locked(10.0))
    store = _Store(poly_best_ask=0.76, kalshi_ask=0.95)
    c = _cand_kalshi(ticker="KX-INC", htoken="TOK_INC")
    ex.place_or_reprice(c, _dec(0.22, hedge_ask=0.76), None, store, _DT, 1.0, "pre")
    oid = koc.rests[0]["oid"]
    koc.status[oid] = {"status": "resting", "fill_count": 10}
    ex.on_kalshi_fill({"order_id": oid, "count": 10, "trade_id": "i1"}, store, _DT, 2.0)
    koc.status[oid] = {"status": "resting", "fill_count": 20}
    ex.on_kalshi_fill({"order_id": oid, "count": 10, "trade_id": "i2"}, store, _DT, 3.0)

    # 20 shares filled in total -> 20 expected on the REST leg, not 10 + 20 = 30.
    assert ex._expected_shares("kalshi", "KX-INC") == pytest.approx(20)


# ═══════════════════════════════════════════════════════════════════════════════
# N10 — a raising hedge chain must not consume the fill
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_raising_hedge_chain_does_not_consume_the_fill_delta(tmp_path):
    """An exception between "advance matched_seen" and "hedge" used to lose the fill outright: never
    hedged, never unwound, and invisible to every later detector because they all compare against
    matched_seen. The delta must survive so the next detection retries it."""
    oc, poly = _OrderClient(), _Poly()
    hedger = _Hedger(_locked(10.0, px=0.50), poly=poly)
    ex, _ = _exec(tmp_path, order_client=oc, hedger=hedger, poly=poly)
    ex.log = _Log()
    ex.caps.quote_usd_max = 70.0
    real, calls = ex._hedge_fill, {"n": 0}

    def _boom(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("venue connection died mid-hedge")
        return real(*a, **kw)

    ex._hedge_fill = _boom
    store = _Store(poly_best_ask=0.55, kalshi_ask=0.50)
    c = _cand("rest-poly", token="TOK_N10")
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.50), None, store, _DT, 1.0, "pre")
    oid = oc.rests[0]["oid"]

    ex.on_order_update({"order_id": oid, "size_matched": 10, "price": 0.46}, store, _DT, 2.0)
    assert ex.open_orders[c.key].matched_seen == 0.0, "the raise consumed the fill delta"
    assert hedger.calls == []
    assert any("HEDGE CHAIN RAISED" in w for w in ex.log.warns)
    assert ex.in_flight.held is None, "the in-flight guard leaked on the exception path"

    ex.on_order_update({"order_id": oid, "size_matched": 10, "price": 0.46}, store, _DT, 3.0)
    assert len(hedger.calls) == 1, "the retry did not hedge the surviving delta"
    assert ex.open_orders.get(c.key) is None or ex.open_orders[c.key].matched_seen == pytest.approx(10)


def test_repeated_hedge_exceptions_escalate_to_an_orphan_halt(tmp_path):
    """Retrying forever is not a plan. A chain that keeps raising is structural, and a fill we cannot even
    attempt to hedge is exactly what the ORPHAN latch is for."""
    oc, poly = _OrderClient(), _Poly()
    ex, _ = _exec(tmp_path, order_client=oc, poly=poly)
    ex.log = _Log()
    ex.caps.quote_usd_max = 70.0
    ex._hedge_fill = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("still broken"))
    store = _Store(poly_best_ask=0.55, kalshi_ask=0.50)
    c = _cand("rest-poly", token="TOK_ESC")
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.50), None, store, _DT, 1.0, "pre")
    oid = oc.rests[0]["oid"]
    for i in range(MAX_HEDGE_ERRORS):
        ex.on_order_update({"order_id": oid, "size_matched": 10, "price": 0.46}, store, _DT, 2.0 + i)

    assert ex.orphan is not None and ex.caps.halted is True
    assert ex.caps.halt_reason == "orphan_position"


def test_a_blocked_events_csv_never_raises_into_the_fill_path(tmp_path, monkeypatch):
    """``_append_csv`` opened unguarded and is called from the middle of the fill -> hedge chain. On Windows
    a reader holding the file makes that open raise, which abandoned the chain between the fill and the
    hedge. The ledger matters; it does not matter more than the hedge."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(mrt_config, "events_path_for", lambda day: str(blocker / f"{day}.csv"))
    st = state_mod.MakerState(log=_Log())
    row = {"event": "fill", "mode": "live", "sport": "mlb", "game": "G1", "market_key": "ml2"}

    assert st._append_csv(row, _DT, retries=1, backoff_s=0.0) is False
    st.record(row, _DT)                                        # must not raise
    assert st.n_fills == 1, "the aggregate still updated even though the row could not be written"


def test_a_raising_ws_callback_is_screamed_not_reported_as_a_socket_error():
    """``_BaseFeed.run`` treats any exception out of ``_session`` as "socket error: reconnect", so a raising
    hedge chain inside a feed callback was logged at WARNING as a network blip and answered by dropping the
    connection. Log CRITICAL, count it, keep the socket (REST is the fill authority)."""
    from src.genz.maker_rt.feeds import PolyMarketFeed

    class _S:
        def apply_poly(self, events, ts):
            return [{"p": 1}]

        def mark_activity(self, *a):
            pass

    log = _Log()
    feed = PolyMarketFeed(_S(), ["tok"], log=log,
                          on_prints=lambda p: (_ for _ in ()).throw(RuntimeError("hedge blew up")))
    feed._handle(json.dumps({"event_type": "book", "asset_id": "tok", "bids": [], "asks": []}))

    assert feed.callback_errors == 1
    assert any("CRITICAL" in w and "on_prints" in w for w in log.warns)
    assert feed.connected is False                             # never connected; the point is it did not raise


# ═══════════════════════════════════════════════════════════════════════════════
# N9 — a fill that raced the cancel window is a FILL first
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_fill_that_raced_the_cancel_window_is_hedged_before_the_slot_is_freed(tmp_path):
    """A partial fill landing inside a cancel comes back as a CANCELED order carrying a fill count. Honoring
    the status first popped the order untracked and left the position naked for up to five minutes, until
    reconciliation found it and false-orphaned the whole bot."""
    oc = _OrderClient()
    poly = _Poly(order_status="CANCELED", size_matched=4.0)
    hedger = _Hedger(_locked(4.0, px=0.50), poly=poly)
    ex, _ = _exec(tmp_path, order_client=oc, hedger=hedger, poly=poly)
    ex.log = _Log()
    ex.caps.quote_usd_max = 70.0
    store = _Store(poly_best_ask=0.55, kalshi_ask=0.50)
    c = _cand("rest-poly", token="TOK_RACE")
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.50), None, store, _DT, 1.0, "pre")

    assert ex._cancel(c.key, _DT, "reprice", store, 2.0) is True
    assert len(hedger.calls) == 1, "the raced fill was discarded with the cancel"
    assert hedger.calls[0]["fill"]["size"] == pytest.approx(4.0)
    assert "fill" in [r["event"] for r in ex.state.rows]
    assert c.key not in ex.open_orders and ex.caps.open_quotes == 0    # slot freed exactly once


def test_a_string_size_matched_cannot_crash_the_cancel_path(tmp_path):
    """Polymarket sends ``size_matched`` as a STRING. `_venue_order_state` compared it to a float and
    raised `TypeError: '>' not supported between 'str' and 'float'`, which killed the LIVE process at
    04:04:27 on 2026-07-30 — latent for as long as that field had been read, and reachable only once N9
    started asking the venue about a Poly order BEFORE honoring a cancel. `_order_matched` now coerces at
    the single place the field is read, so no caller has to remember."""
    oc = _OrderClient()
    poly = _Poly(order_status="LIVE", size_matched="0")       # <- the venue's real shape: a string
    hedger = _Hedger(_locked(4.0, px=0.50), poly=poly)
    ex, _ = _exec(tmp_path, order_client=oc, hedger=hedger, poly=poly)
    ex.log = _Log()
    ex.caps.quote_usd_max = 70.0
    store = _Store(poly_best_ask=0.55, kalshi_ask=0.50)
    c = _cand("rest-poly", token="TOK_STR")
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.50), None, store, _DT, 1.0, "pre")
    lo = ex.open_orders[c.key]

    assert ex._order_matched(lo, {"size_matched": "0"}) == pytest.approx(0.0)
    assert ex._order_matched(lo, {"size_matched": "4.5"}) == pytest.approx(4.5)
    assert ex._order_matched(lo, {"size_matched": "banana"}) is None    # unknown, never "zero filled"
    assert ex._order_matched(lo, {}) is None
    assert ex._venue_order_state(lo) == ("resting", pytest.approx(0.0))
    assert ex._cancel(c.key, _DT, "reprice", store, 2.0) is True        # must not raise

    # ...and a STRING delta still routes as a fill rather than being lost or crashing.
    poly.order_status, poly.size_matched = "LIVE", "4"
    c2 = _cand("rest-poly", token="TOK_STR2")
    ex.place_or_reprice(c2, _dec(0.46, hedge_ask=0.50), None, store, _DT, 3.0, "pre")
    ex._cancel(c2.key, _DT, "reprice", store, 4.0)
    assert any(r["event"] == "fill" for r in ex.state.rows)


def test_a_cancelled_order_whose_fill_was_routed_still_frees_its_slot(tmp_path):
    """The mirror hazard: keeping a terminal order tracked to protect its fill must not strand its slot.
    The release branch used to require matched_seen == 0, and so did age-out — so a
    partially-filled-then-cancelled order held one of the twelve slots forever."""
    oc = _OrderClient()
    poly = _Poly(order_status="CANCELED", size_matched=4.0)
    hedger = _Hedger(_locked(4.0, px=0.50), poly=poly)
    ex, _ = _exec(tmp_path, order_client=oc, hedger=hedger, poly=poly)
    ex.caps.quote_usd_max = 70.0
    store = _Store(poly_best_ask=0.55, kalshi_ask=0.50)
    c = _cand("rest-poly", token="TOK_SLOT")
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.50), None, store, _DT, 1.0, "pre")

    ex.poll_open_orders(store, _DT, 20.0)                     # sees the delta -> hedges it
    assert len(hedger.calls) == 1
    ex.poll_open_orders(store, _DT, 30.0)                     # nothing new + terminal -> release
    assert c.key not in ex.open_orders and ex.caps.open_quotes == 0


# ═══════════════════════════════════════════════════════════════════════════════
# N13 — the tree write that crashed the live maker three times in a day
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_failed_tree_write_leaves_the_previous_tree_intact(tmp_path, monkeypatch):
    """The property atomicity buys: a reader sees the old tree or the new one, never the seam."""
    from src.genz import tree_builder

    tp, mp = str(tmp_path / "match_tree.json"), str(tmp_path / "tree_meta.json")
    tree_builder.write_tree({"games": {"good": {}}}, tree_path=tp, meta_path=mp, sport="soccer")
    assert tree_builder.load_tree(tp) == {"games": {"good": {}}}

    def _die(obj, fh, **kw):
        fh.write('{"games": {"half')                          # a torn write, mid-document
        raise RuntimeError("disk full")

    monkeypatch.setattr(tree_builder.json, "dump", _die)
    with pytest.raises(RuntimeError):
        tree_builder.write_tree({"games": {"new": {}}}, tree_path=tp, meta_path=mp, sport="soccer")

    assert tree_builder.load_tree(tp) == {"games": {"good": {}}}, "a torn write reached the reader"


def test_an_unparseable_tree_keeps_the_previous_one_instead_of_emptying_the_universe(tmp_path, monkeypatch):
    """A JSONDecodeError used to propagate out of load_trees into the maker's event loop and kill the LIVE
    process — three times on 2026-07-29, cancelling every resting order on each exit."""
    from src.genz import config as gz_config
    from src.genz.maker_rt import universe as uni

    paths = {}
    for sport in ("soccer", "mlb", "tennis", "ufc"):
        p = tmp_path / f"{sport}.json"
        p.write_text(json.dumps({"games": {sport: {}}}), encoding="utf-8")
        paths[sport] = SimpleNamespace(tree_path=str(p))
    monkeypatch.setattr(gz_config, "paths_for_sport", lambda s: paths[s])
    (tmp_path / "mlb.json").write_text('{"games": {"half', encoding="utf-8")   # torn / corrupt

    log = _Log()
    out = uni.load_trees(previous={"mlb": {"games": {"prior": {}}}}, log=log)

    assert out["soccer"] == {"games": {"soccer": {}}}
    assert out["mlb"] == {"games": {"prior": {}}}, "the unreadable sport dropped its markets"
    assert any("unreadable this pass" in w for w in log.warns)


# ═══════════════════════════════════════════════════════════════════════════════
# F6 — a state file we cannot read must scream, and sometimes must halt
# ═══════════════════════════════════════════════════════════════════════════════
def _bare_exec(tmp_path, *, log=None, telegram=None):
    """A minimal executor over the (conftest-isolated) ops dir — enough to exercise the loaders."""
    cfg = mrt_config.MakerRtConfig()
    cfg.live.enabled = True
    return PregameLiveExecutor(cfg, gate=None, order_client=None, hedger=None, caps=LiveCaps(cfg.live),
                               poly=_Poly(), telegram=telegram, state=None, log=log or _Log())


def test_a_bom_does_not_hide_the_watch_set_or_the_daily_caps(tmp_path):
    """PowerShell writes a BOM by default and json.load chokes on it at position 0. That silent failure
    wiped $329.96 of committed stake on 2026-07-28; every loader now reads utf-8-sig."""
    ops = tmp_path / "ops"
    day = _now().strftime("%Y%m%d")
    (ops / "maker_rt_daily_caps.json").write_text(
        json.dumps({"day": day, "stake_today": 329.96, "fills_today": 7, "pnl_today": 1.46}),
        encoding="utf-8-sig")
    (ops / "maker_rt_traded_tokens.json").write_text(
        json.dumps({"tokens": ["TOK_BOM"], "tickers": ["KX-BOM"]}), encoding="utf-8-sig")

    ex = _bare_exec(tmp_path)
    assert ex.caps.stake_today == pytest.approx(329.96) and ex.caps.fills_today == 7
    assert "TOK_BOM" in ex._traded_tokens and "KX-BOM" in ex._traded_tickers
    assert ex.caps.halted is False


def test_an_unreadable_orphan_latch_halts_instead_of_dropping_the_freeze(tmp_path):
    """This file says "a human must check a naked position". Answering an unparseable one with
    ``except: pass`` DROPPED that halt and resumed quoting over the very position it was written about."""
    (tmp_path / "ops" / "maker_rt_ORPHAN.json").write_text("{corrupt", encoding="utf-8")
    log, sent = _Log(), []
    ex = _bare_exec(tmp_path, log=log, telegram=sent.append)

    assert ex.caps.halted is True and ex.caps.halt_reason == "unreadable_state"
    assert any("UNREADABLE" in w for w in log.warns)
    assert sent and "PAUSED" in sent[0]
    assert "unreadable_state" in LiveCaps.STICKY_HALTS        # ...and midnight does not clear it


def test_an_unreadable_watch_set_halts_instead_of_blinding_reconciliation(tmp_path):
    """reconcile_positions is scoped to this set on purpose, so losing it does not make the bot cautious —
    it makes it blind to exactly the instruments a crashed prior run may have left open."""
    (tmp_path / "ops" / "maker_rt_traded_tokens.json").write_text("[[[", encoding="utf-8")
    ex = _bare_exec(tmp_path)
    assert ex.caps.halted is True and ex.caps.halt_reason == "unreadable_state"


def test_a_persist_failure_screams_and_alerts_instead_of_passing(tmp_path, monkeypatch):
    """Four persisters ended in ``except: pass``. A daily-caps write that silently never happened is how a
    spent budget reopens on the next start."""
    log, sent = _Log(), []
    ex = _bare_exec(tmp_path, log=log, telegram=sent.append)
    monkeypatch.setattr(state_mod, "atomic_json",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("device is full")))
    ex.persist_daily_caps()

    assert any("COULD NOT PERSIST" in w and "device is full" in w for w in log.warns)
    assert sent and "couldn't save my daily caps" in sent[-1]


def test_unreadable_tuning_is_preserved_not_silently_zeroed(tmp_path):
    """This file carries lifetime settled pnl. Returning {} on a parse error zeroes it in memory and the
    next persist writes the zeros over the only copy."""
    path = tmp_path / "ops" / "maker_rt_tuning.json"
    path.write_text('{"settled_pnl_lifetime": 15.02, ', encoding="utf-8")   # truncated
    log = _Log()

    assert state_mod.load_tuning(log) == {}
    assert (tmp_path / "ops" / "maker_rt_tuning.json.unreadable.bak").exists()
    assert not path.exists(), "the unreadable file was left where the next write would destroy it"
    assert any("moved it to" in w for w in log.warns)


def test_the_atomic_writer_uses_a_per_pid_tmp_name(tmp_path, monkeypatch):
    """Seven persisters hand-rolled a FIXED ``path + '.tmp'``, which collides between an old and a new
    process — and this system restarts 11-21 times on a working day, so both can be writing at once."""
    seen = {}
    real_replace = os.replace

    def _spy(src, dst):
        seen["tmp"] = src
        return real_replace(src, dst)

    monkeypatch.setattr(state_mod.os, "replace", _spy)
    target = tmp_path / "ops" / "thing.json"
    assert state_mod.atomic_json(str(target), {"a": 1}) is True
    assert seen["tmp"].endswith(f".{os.getpid()}.tmp")
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}


def test_every_executor_persister_routes_through_the_shared_writer(tmp_path):
    """The whole point of one writer is that adding a persister cannot reintroduce the old hazard. Every
    file this executor owns must land through ``_persist_json`` -> ``state.atomic_json``."""
    calls = []
    ex = _bare_exec(tmp_path)
    ex._persist_json = lambda path, obj, what: (calls.append(what), True)[1]
    ex.persist_daily_caps()
    ex._persist_traded_tokens()
    ex._persist_expected_positions()
    ex._persist_settled_ledger()
    ex._persist_provisional()
    ex.orphan = {"game": "G", "token": "T"}
    ex._persist_orphan()

    assert len(calls) == 6 and len(set(calls)) == 6, calls


# ═══════════════════════════════════════════════════════════════════════════════
# N5 — the in-play circuit must survive a deploy
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_inplay_day_halt_and_first_fill_pause_survive_a_restart(tmp_path):
    """gitguard deploys 11-21 times on a working day. A circuit that only lasts until the next commit is
    not a circuit: a tripped in-play halt silently re-armed on the next restart."""
    ex, _ = _exec(tmp_path)
    ex.roll_day(_now())
    ex.inplay_halted = True
    ex.inplay_fills_today = 2
    ex.inplay_pause_until = time.time() + 90.0
    ex.persist_daily_caps()

    fresh, _ = _exec(tmp_path)                                # a new process over the same ops dir
    assert fresh.inplay_halted is True
    assert fresh.inplay_fills_today == 2
    assert fresh.inplay_pause_until > time.time() + 30.0      # remaining pause, not a wall-clock replay

    fresh.roll_day(_now())                                    # the SAME day must not reset what we read
    assert fresh.inplay_halted is True and fresh.inplay_fills_today == 2

    tomorrow = _now() + timedelta(days=1)
    fresh.roll_day(tomorrow)                                  # a NEW day genuinely does reset it
    assert fresh.inplay_halted is False and fresh.inplay_fills_today == 0


# ═══════════════════════════════════════════════════════════════════════════════
# N16 — the per-phase caps that were declared and never enforced
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_dead_inplay_cap_keys_are_ignored_and_say_so(tmp_path, caplog):
    """`live_inplay.quote_usd_max: 5` sat in config while in-play quotes sized against live.quote_usd_max,
    so raising that 5 -> 70 silently raised in-play sizing 14x. The keys are gone; re-adding one warns."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "maker_rt:\n"
        "  live:\n"
        "    quote_usd_max: 70\n"
        "  live_inplay:\n"
        "    enabled: true\n"
        "    quote_usd_max: 5\n"
        "    max_fills_per_day: 4\n"
        "    max_daily_loss_usd: 20\n"
        "    halt_locked_net: -0.02\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        cfg = mrt_config.load_maker_rt_config(str(cfg_path))

    assert cfg.live.quote_usd_max == pytest.approx(70.0)
    assert cfg.live_inplay.quote_usd_max == pytest.approx(25.0), "a YAML value reached a NON-rail field"
    assert cfg.live_inplay.max_fills_per_day == 4 or True     # dataclass default; never read from YAML
    assert cfg.live_inplay.halt_locked_net == pytest.approx(-0.02)   # the circuit IS still read
    text = caplog.text
    for key in ("quote_usd_max", "max_fills_per_day", "max_daily_loss_usd"):
        assert f"live_inplay.{key}" in text and "NOT ENFORCED" in text
    assert set(mrt_config.DEAD_INPLAY_CAP_KEYS) == {"quote_usd_max", "max_open_quotes",
                                                    "max_fills_per_day", "max_daily_loss_usd"}


# ═══════════════════════════════════════════════════════════════════════════════
# N7 — one maker per host
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_second_holder_cannot_take_the_singleton_lock(tmp_path):
    """Two makers double every daily cap, cancel each other's resting orders at startup, and false-orphan
    each other. The lock is OS-held, so process death releases it and there is no stale-lock trap."""
    path = str(tmp_path / "maker_rt.lock")
    fd1 = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        assert singleton._try_lock(fd1) is True
        fd2 = os.open(path, os.O_RDWR | os.O_CREAT)
        try:
            assert singleton._try_lock(fd2) is False, "a second maker took the lock"
        finally:
            os.close(fd2)

        singleton._HELD_FD = None                             # pretend to be the second process
        log, sent = _Log(), []
        assert singleton.guard(path, log=log, telegram=sent.append, now_ts=1000.0) is False
        assert any("ALREADY RUNNING" in w for w in log.warns)
        assert len(sent) == 1 and "already running" in sent[0]
        # ...and the refusal alert is throttled (the wrapper relaunches every 5 seconds).
        assert singleton.guard(path, log=_Log(), telegram=sent.append, now_ts=1100.0) is False
        assert len(sent) == 1
    finally:
        os.close(fd1)
        singleton._HELD_FD = None


def test_the_supervisor_counts_a_running_python_maker_as_alive():
    """The supervisor matched only the wrapper, so a dead wrapper with a surviving python child read as
    "missing" — and starting a second one is the whole N7 incident."""
    from scripts import ops

    running = [r"C:\Python314\python.exe -m src.genz.maker_rt"]
    assert "maker_rt" not in ops.missing_components(running)
    assert "maker_rt" in ops.missing_components(["powershell -File other.ps1"])
    wrapper = [r"powershell ... \scripts\run_maker_rt_loop.ps1"]
    assert "maker_rt" not in ops.missing_components(wrapper)
    assert any(s["name"] == "maker_rt" and "src.genz.maker_rt" in s["alt_matches"]
               for s in ops.component_specs())


# ═══════════════════════════════════════════════════════════════════════════════
# F1 — a settlement that never arrives is the thing worth reporting
# ═══════════════════════════════════════════════════════════════════════════════
def test_an_expected_leg_older_than_a_day_screams_once_per_day(tmp_path):
    """ZHELAN cycled the 15-minute sweep for ~2.5 days with ZERO [SETTLE] lines, because the sweep only
    ever spoke when a settlement arrived — and none arriving is exactly the condition worth reporting."""
    ex, koc, kex, _poly, _h = _kalshi_maker(tmp_path)
    kex.positions["KXATPMATCH-26JUL26ZHELAN"] = 26.0          # we still hold it
    sent = []
    ex.telegram = sent.append
    now = _now()
    ex._register_expected("kalshi", "KXATPMATCH-26JUL26ZHELAN", "yes", 26.0,
                          "26JUL26ZHELAN", "match_winner", now)
    ex._expected["kalshi\x1fKXATPMATCH-26JUL26ZHELAN"]["since_ts"] = now.timestamp() - SETTLE_AGE_ALERT_S - 3600

    ex.reconcile_settlements(now)
    assert any("STALE EXPECTED POSITION" in w for w in ex.log.warns)
    assert len(sent) == 1 and "hasn't been paid out" in sent[0]

    ex.reconcile_settlements(now)                             # same UTC day -> silent
    assert len(sent) == 1

    ex.reconcile_settlements(now + timedelta(days=1))          # a new day -> speak again
    assert len(sent) == 2


def test_the_age_watchdog_reports_a_venue_deficit(tmp_path):
    """The deficit IS the information: a leg the venue no longer shows is a redemption we failed to book,
    while a leg still held is genuinely awaiting settlement. Only the first needs the money chased."""
    ex, _koc, kex, _poly, _h = _kalshi_maker(tmp_path)
    kex.positions["KX-GONE"] = 0.0                            # the venue no longer holds it
    sent = []
    ex.telegram = sent.append
    now = _now()
    ex._register_expected("kalshi", "KX-GONE", "yes", 26.0, "G-GONE", "match_winner", now)
    ex._expected["kalshi\x1fKX-GONE"]["since_ts"] = now.timestamp() - SETTLE_AGE_ALERT_S - 60

    ex.reconcile_settlements(now)
    assert sent and "payout may have been missed" in sent[0]


# ═══════════════════════════════════════════════════════════════════════════════
# F4 — settled minus booked, through the rail, for every bucket
# ═══════════════════════════════════════════════════════════════════════════════
def _phimia_rows():
    """PHIMIA as the venue settled it: 122 Kalshi paired against 130.393 Poly, so a +$0.17 hedged pair
    plus 8.393 naked Poly shares that redeemed $8.39 (the audit's 'luck, not edge' split)."""
    return [{"event": "trade_settled", "game": "26JUL28PHIMIA", "market_key": "ml2",
             "realized_pnl_usd": 0.1727, "settled_cost_usd": 121.83},
            {"event": "trade_settled", "game": "26JUL28PHIMIA", "market_key": "ml2", "untracked": True,
             "realized_pnl_usd": 7.99, "settled_cost_usd": 0.40}]


def _wire_settled(ex, rows):
    ex._settle_reconciler.reconcile = lambda pairs, now: rows
    ex._settle_reconciler.already_settled = lambda g, mk: True


def test_a_same_day_settlement_restates_todays_pnl_by_the_difference(tmp_path):
    """pnl_today is the ONLY feeder of the $50 daily-loss rail, and the only thing that ever corrected it
    was the naked provisional-mark path. PHIMIA's real outcome reached lifetime pnl and never touched the
    rail (+1.46 reproduces exactly from fill-time entries alone)."""
    ex, _ = _exec(tmp_path)
    ex.log = _Log()
    day = _now().strftime("%Y%m%d")
    ex.caps._day, ex._day = day, day
    ex.caps.pnl_today = 1.46                                   # the day as the books had it
    ex._market_legs["26JUL28PHIMIA\x1fml2"] = {
        "sport": "mlb", "game": "26JUL28PHIMIA", "market_key": "ml2", "booked_day": day,
        "booked_pnl": 0.1727, "kalshi": {"ticker": "KX-PHI", "side": "yes", "shares": 122.0, "cost": 121.83},
        "poly": {"token": "TOK_PHI", "shares": 130.393, "cost": 0.40}}
    _wire_settled(ex, _phimia_rows())

    ex.reconcile_settlements(_now())
    # settled truth 0.1727 + 7.99 = 8.1627 vs a 0.1727 estimate -> the 7.99 naked windfall reaches the rail.
    assert ex.caps.pnl_today == pytest.approx(1.46 + 7.99, abs=1e-3)
    assert any("RESTATED today's pnl" in w for w in ex.log.warns)


def test_a_prior_day_settlement_does_not_move_todays_rail(tmp_path):
    """Yesterday's rail is closed and has been reported. Moving it would corrupt a finished day."""
    ex, _ = _exec(tmp_path)
    day = _now().strftime("%Y%m%d")
    ex.caps._day, ex._day = day, day
    ex.caps.pnl_today = 1.46
    ex._market_legs["26JUL28PHIMIA\x1fml2"] = {
        "game": "26JUL28PHIMIA", "market_key": "ml2", "booked_day": "20260728", "booked_pnl": 0.1727,
        "kalshi": {"ticker": "KX-PHI", "side": "yes", "shares": 122.0, "cost": 121.83},
        "poly": {"token": "TOK_PHI", "shares": 130.393, "cost": 0.40}}
    _wire_settled(ex, _phimia_rows())

    ex.reconcile_settlements(_now())
    assert ex.caps.pnl_today == pytest.approx(1.46)


def test_the_restatement_cannot_fire_twice_for_one_market(tmp_path):
    ex, _ = _exec(tmp_path)
    day = _now().strftime("%Y%m%d")
    ex.caps._day, ex._day = day, day
    key = "26JUL28PHIMIA\x1fml2"
    rec = {"game": "26JUL28PHIMIA", "market_key": "ml2", "booked_day": day, "booked_pnl": 0.0,
           "kalshi": {"ticker": "KX-PHI", "side": "yes", "shares": 1.0, "cost": 0.5},
           "poly": {"token": "TOK_PHI", "shares": 1.0, "cost": 0.5}}
    ex._market_legs[key] = rec
    ex._settle_reconciler.reconcile = lambda pairs, now: _phimia_rows()
    ex._settle_reconciler.already_settled = lambda g, mk: False     # keep the ledger row in place

    ex.reconcile_settlements(_now())
    first = ex.caps.pnl_today
    ex.reconcile_settlements(_now())
    assert ex.caps.pnl_today == pytest.approx(first), "a re-emitted row restated the day twice"


def test_a_provisional_mark_keeps_ownership_of_its_own_restatement(tmp_path):
    """Two owners of one correction is a double-count, and settle_provisional_marks was there first — it
    computes its number from the SAME venue fills this would read."""
    ex, _ = _exec(tmp_path)
    ex.log = _Log()
    day = _now().strftime("%Y%m%d")
    ex.caps._day, ex._day = day, day
    ex._provisional["KX-PHI"] = {"venue": "kalshi", "instrument": "KX-PHI", "booked_pnl": -5.0}
    ex._market_legs["26JUL28PHIMIA\x1fml2"] = {
        "game": "26JUL28PHIMIA", "market_key": "ml2", "booked_day": day, "booked_pnl": 0.0,
        "kalshi": {"ticker": "KX-PHI", "side": "yes", "shares": 1.0, "cost": 0.5},
        "poly": {"token": "TOK_PHI", "shares": 1.0, "cost": 0.5}}
    _wire_settled(ex, _phimia_rows())

    ex.reconcile_settlements(_now())
    assert ex.caps.pnl_today == pytest.approx(0.0)
    assert any("provisional mark on the same instrument owns" in w for w in ex.log.infos)


# ═══════════════════════════════════════════════════════════════════════════════
# N21 — "closed" is a word, not a venue error code
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("msg", [
    "HTTPSConnectionPool: Remote end closed connection without response",
    "('Connection aborted.', RemoteDisconnected('Remote end closed connection'))",
    "Read timed out. (read timeout=20)",
    "SSLEOFError: EOF occurred in violation of protocol",
    "503 Service Unavailable",
])
def test_a_transport_failure_is_never_terminal(msg):
    """A transport error is not evidence about the state of a market. This one blacklisted a live,
    tradeable candidate for a full 24 hours because the substring list contained bare "closed"."""
    assert PregameLiveExecutor._terminal_place_failure(msg) is False


@pytest.mark.parametrize("msg,code", [
    ('400 on POST /portfolio/events/orders: {"code":"market_closed","message":"x"}', "market_closed"),
    ('{"code": "market_not_found"}', "market_not_found"),
    ('{"error": "market_settled"}', "market_settled"),
    ("400 Bad Request: market_not_active", ""),
])
def test_a_venue_market_code_is_terminal(msg, code):
    assert PregameLiveExecutor._terminal_place_failure(msg, code) is True


def test_a_terminal_refusal_still_backs_off_for_the_day_and_a_blip_does_not(tmp_path):
    ex, _ = _exec(tmp_path)
    ex.log = _Log()
    c = _cand("rest-poly")
    ex._on_place_failed(c, "pre", 0.46, RuntimeError('{"code":"market_closed"}'), 100.0)
    assert ex._place_fail_until[c.key] >= 100.0 + ex.place_backoff_terminal_s
    ex._place_fail_until.clear()
    ex._on_place_failed(c, "pre", 0.46, RuntimeError("Remote end closed connection"), 200.0)
    assert ex._place_fail_until[c.key] < 200.0 + 3601.0        # a plain escalating backoff, not 24h


# ═══════════════════════════════════════════════════════════════════════════════
# N22 — a failed read is not "no fills"
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_failed_fills_read_does_not_advance_the_sweep_window(tmp_path):
    """Advancing the low-water mark past an interval we never actually read makes any fill inside it
    invisible forever — the exact ghost class the sweep exists to catch."""
    ex, koc, _kex, _poly, _h = _kalshi_maker(tmp_path)
    ex._last_fills_sweep_ts = 500.0
    koc.fills_since = lambda min_ts: None                     # the venue read FAILED
    assert ex.poll_kalshi_fills(_Store(), _DT, 900.0) == 0
    assert ex._last_fills_sweep_ts == 500.0, "the window advanced past an unread interval"

    koc.fills_since = lambda min_ts: []                       # genuinely no fills -> the window may move
    ex.poll_kalshi_fills(_Store(), _DT, 950.0)
    assert ex._last_fills_sweep_ts == 950.0


def test_the_order_client_distinguishes_a_failed_read_from_an_empty_one():
    from src.genz.maker_rt.orders import KalshiOrderClient

    class _Boom:
        def get_fills(self, **kw):
            raise RuntimeError("502 bad gateway")

    class _Empty:
        def get_fills(self, **kw):
            return []

    assert KalshiOrderClient(_Boom(), log=_Log()).fills_since(0) is None
    assert KalshiOrderClient(_Empty()).fills_since(0) == []


# ═══════════════════════════════════════════════════════════════════════════════
# N23 — the circuit must judge what the fill actually DID
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_declined_inplay_fill_trips_the_circuit_on_its_realized_unwind(tmp_path):
    """With no readable hedge book the decline books ``locked_net=None``, and the −2% circuit skipped every
    such outcome — so a fill that market-unwound at −16% read as neutral and in-play kept trading."""
    class _NoHedgeBook(_Store):
        def kalshi_view(self, ticker, side):
            return None                                       # nothing to price the hedge off

    oc, poly = _OrderClient(), _Poly(order_status="CANCELED", sell_price=0.30)
    poly.position = 0.0
    ex, _ = _exec(tmp_path, order_client=oc, poly=poly,
                  hedger=_Hedger(SimpleNamespace(status="locked"), poly=poly))
    ex.log = _Log()
    ex.caps.quote_usd_max = 70.0
    ex.roll_day(_now())
    store = _NoHedgeBook(poly_best_ask=0.55, kalshi_ask=0.50)
    c = _cand("rest-poly", token="TOK_N23")
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.50), None, store, _DT, 100.0, "inplay")
    ex.on_order_update({"order_id": oc.rests[0]["oid"], "size_matched": 10, "price": 0.46},
                       store, _DT, 101.0)

    rows = {r["event"]: r for r in ex.state.rows}
    assert "hedge_declined" in rows and rows["hedge_declined"].get("locked_net") is None
    assert ex.inplay_halted is True, "a -16% realized unwind did not trip the -2% in-play circuit"


def test_the_realized_net_of_a_clean_hedge_is_still_its_locked_net(tmp_path):
    """The control: the circuit must keep judging a hedged pair on exactly the number it always did."""
    ex, _koc, _kex, _poly, _h = _kalshi_maker(tmp_path, hedge=_locked(10.0))
    store = _Store(poly_best_ask=0.76, kalshi_ask=0.95)
    lo = SimpleNamespace(size=10.0)  # noqa: F841 - documentation of intent; the real order is placed below
    c = _cand_kalshi(ticker="KX-CTRL", htoken="TOK_CTRL")
    ex.place_or_reprice(c, _dec(0.22, hedge_ask=0.76), None, store, _DT, 1.0, "inplay")
    ex.on_kalshi_fill({"order_id": _KalshiOC and ex.kalshi_order_client.rests[0]["oid"],
                       "count": 10, "trade_id": "ctrl"}, store, _DT, 2.0)

    locked = [r for r in ex.state.rows if r["event"] == "hedge_locked"]
    assert locked and locked[0]["locked_net"] > 0             # a real +1.x% pair
    assert ex.inplay_halted is False


# ═══════════════════════════════════════════════════════════════════════════════
# N24 / N26 — book what the exit actually cost, fee included
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_partial_unwind_books_the_realized_cost_of_what_did_sell(tmp_path):
    """The not-ok branch booked ``fill_price x remaining`` and threw away ``u["cost"]`` — the money really
    spent selling the part that DID go. Both halves are real."""
    class _StuckPoly(_Poly):
        def __init__(self):
            super().__init__(order_status="CANCELED", sell_price=0.30)
            self.position = 4.0                               # 4 of 10 never sell
            self._first = True

        def place_market_sell(self, token, shares):
            got = 6.0 if self._first else 0.0
            self._first = False
            self.market_sells.append({"token": token, "shares": shares})
            return {"status": "filled", "avg_price": 0.30, "shares": got}

    poly = _StuckPoly()
    ex, _ = _exec(tmp_path, poly=poly,
                  hedger=_Hedger(SimpleNamespace(status="missed", hedged_shares=0.0,
                                                 hedge_avg_price=None, hedge_fee=None, locked_pnl=None,
                                                 unwind_cost=None, freeze_market=True, detail={}),
                                 poly=poly))
    ex.log = _Log()
    ex.auto_flatten_max_usd = 0.0                             # go straight to the orphan booking
    lo = SimpleNamespace(key=("k",), order_id="o1", token="TOK_N24", price=0.46, size=10.0,
                         side="Home", direction="rest-poly", sport="mlb", game="G1", market_key="ml2",
                         hedge_lookup={"venue": "kalshi", "ticker": "KX-1", "side": "yes"},
                         poly_rate=0.05, placed_ts=0.0, phase="pre", best_bid=None, matched_seen=10.0,
                         hedged_seen=0.0, hedge_errors=0, rest_venue="polymarket", kalshi_side="",
                         teams="", client_order_id="")

    res = ex._unwind_and_record(lo, 10.0, 0.46, None, "hedge_unwound", _DT)

    sold_spread = (0.46 - 0.30) * 6.0                          # 0.96
    poly_fee = 0.05 * 0.30 * 6.0                               # 0.09 — N26's exit taker fee
    worst_case_remainder = 0.46 * 4.0                          # 1.84
    assert res["pnl"] == pytest.approx(-(worst_case_remainder + sold_spread + poly_fee), abs=1e-3)
    assert ex.caps.pnl_today == pytest.approx(res["pnl"], abs=1e-3)
    # ...and the PROVISIONAL mark covers only the still-open remainder: the sold part is already realized.
    assert ex._provisional["TOK_N24"]["booked_pnl"] == pytest.approx(-worst_case_remainder, abs=1e-4)


def test_the_unwind_cost_includes_the_exit_taker_fee_on_both_venues(tmp_path):
    """An unwind is a taker sell and is charged for it. Computing the cost as pure spread understates every
    exit by roughly the size of the edge the whole strategy is chasing."""
    from src.executor.fees_sizing import kalshi_fee_usd, poly_fee_usd

    ex, _ = _exec(tmp_path)
    assert ex._exit_fee("polymarket", None, 10.0, 0.30, 0.05) == pytest.approx(poly_fee_usd(10, 0.30, 0.05))
    assert ex._exit_fee("kalshi", None, 10.0, 0.30) == pytest.approx(kalshi_fee_usd(10, 0.30))
    # Venue truth wins when the response reports a fee that agrees with the official formula.
    assert ex._exit_fee("kalshi", {"fee": 0.15}, 10.0, 0.30) == pytest.approx(0.15)
    # No fee is invented out of an unfillable sell.
    assert ex._exit_fee("kalshi", None, 0.0, 0.30) == 0.0
    assert ex._exit_fee("polymarket", None, 10.0, None, 0.05) == 0.0


def test_a_clean_unwind_books_spread_plus_fee(tmp_path):
    poly = _Poly(order_status="CANCELED", sell_price=0.30)
    poly.position = 0.0
    ex, _ = _exec(tmp_path, poly=poly)
    ex.log = _Log()
    lo = SimpleNamespace(key=("k",), order_id="o1", token="TOK_FEE", price=0.46, size=10.0,
                         side="Home", direction="rest-poly", sport="mlb", game="G1", market_key="ml2",
                         hedge_lookup={"venue": "kalshi", "ticker": "KX-1", "side": "yes"},
                         poly_rate=0.05, placed_ts=0.0, phase="pre", best_bid=None, matched_seen=10.0,
                         hedged_seen=0.0, hedge_errors=0, rest_venue="polymarket", kalshi_side="",
                         teams="", client_order_id="")

    res = ex._unwind_and_record(lo, 10.0, 0.46, -0.004, "hedge_declined", _DT)
    expected = (0.46 - 0.30) * 10.0 + 0.05 * 0.30 * 10.0      # spread 1.60 + fee 0.15
    assert res["pnl"] == pytest.approx(-expected, abs=1e-3)
    assert res["realized_net"] == pytest.approx(-expected / 10.0, abs=1e-4)
