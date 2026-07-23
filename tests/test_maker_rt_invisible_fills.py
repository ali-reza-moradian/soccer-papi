"""Regression tests for the 2026-07-23 INVISIBLE-FILL incident.

Two resting Kalshi maker orders (mrt-84 SKCMIN-SKC3 50@$0.02, mrt-941 SJORL-ORL3 5@$0.59) filled at
the venue and the bot never knew. Three independent defects had to line up, and each one is pinned
here so it cannot come back:

  1. LOST CALLBACK — the universe-rebuild path re-created both feeds and re-attached only
     ``on_prints``, silently dropping ``on_fill``. The private fill channel was a no-op.
  2. v1 FIELD NAMES ON THE READ PATH — the v1->v2 order migration moved counts to ``*_fp``
     fixed-point strings, but ``_order_matched`` / ``_kalshi_position`` still read the bare v1 names,
     so the REST poll saw "no fill" and reconciliation saw "flat" on a real 50-contract position.
  3. FILLED READ AS "NOT CANCELLED" — a DELETE on a filled order 404s; the status read back
     'executed', which was neither "cancelled" nor "filled", so the order was kept tracked and the
     cancel retried for 11.5 hours (6,126 attempts) while the position sat naked.

Plus the hardening the incident demanded: an ORPHAN halt that does not depend on Telegram, and a
flap counter.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from src.executor.kalshi_exec import fp_num
from src.genz.maker_rt.pregame_exec import PregameLiveExecutor

from .test_maker_rt_pregame import (_KalshiExec, _KalshiOC, _Log, _Poly, _State, _Store, _cand_kalshi,
                                    _dec, _exec_kalshi, _Hedger)

NOW = __import__("datetime").datetime(2026, 7, 23, 3, 18, 29, tzinfo=__import__("datetime").timezone.utc)
#: books on which a 0.46 rest-kalshi fill hedges PROFITABLY on Poly (locked ~+9%), so the fill takes the
#: hedge path rather than the decline+unwind path.
_HEDGEABLE = _Store(poly_best_ask=0.45, kalshi_ask=0.60)


# --------------------------------------------------------------------------- #
# 1. fp_num — the v1/v2 field-name reader                                       #
# --------------------------------------------------------------------------- #
def test_fp_num_reads_v2_fixed_point_strings_and_v1_ints():
    # The EXACT shape GET /portfolio/orders returned for the invisible SJORL fill.
    v2 = {"fill_count_fp": "5.00", "initial_count_fp": "5.00", "remaining_count_fp": "0.00",
          "status": "executed"}
    assert fp_num(v2, "fill_count") == 5.0
    assert fp_num(v2, "remaining_count") == 0.0
    assert fp_num({"fill_count": 3}, "fill_count") == 3.0          # v1 still works
    assert fp_num({"status": "resting"}, "fill_count") is None     # genuinely absent -> None, not 0


def test_order_matched_reads_v2_payload(tmp_path):
    """THE bug: this returned None for 11.5h on a fully-executed order."""
    ex, _ = _exec_kalshi(tmp_path)
    lo = SimpleNamespace(rest_venue="kalshi", size=5.0, matched_seen=0.0)
    v2 = {"fill_count_fp": "5.00", "initial_count_fp": "5.00", "remaining_count_fp": "0.00",
          "status": "executed"}
    assert ex._order_matched(lo, v2) == 5.0
    # An 'executed' order with no readable count is FULLY filled — never "unknown".
    assert ex._order_matched(lo, {"status": "executed"}) == 5.0
    # A resting order with no fill is still None (so we do not invent a phantom fill).
    assert ex._order_matched(lo, {"status": "resting", "remaining_count_fp": "5.00",
                                  "initial_count_fp": "5.00"}) == 0.0


# --------------------------------------------------------------------------- #
# 2. the REST poll is a real, WS-independent fill authority                      #
# --------------------------------------------------------------------------- #
def _rest_kalshi_order(tmp_path, *, koc=None, kalshi=None, hedger=None, state=None):
    """Place one live rest-kalshi order (never-crossable: quote 0.46 under a 0.60 ask) and return
    (ex, key, koc, state)."""
    koc = koc or _KalshiOC()
    state = state or _State()
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kalshi, hedger=hedger, state=state)
    ex.roll_day(NOW)
    c = _cand_kalshi()
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.55), None, _Store(kalshi_ask=0.60), NOW, 1000.0, "pre")
    assert c.key in ex.open_orders, "order should be tracked after placement"
    return ex, c.key, koc, state


def test_poll_open_orders_detects_v2_fill_and_hedges(tmp_path):
    """The incident, replayed: order filled, socket silent. The REST poll alone must catch + hedge."""
    koc = _KalshiOC()
    ex, key, koc, state = _rest_kalshi_order(tmp_path, koc=koc)
    oid = ex.open_orders[key].order_id
    koc.status[oid] = {"order_id": oid, "status": "executed", "fill_count_fp": "5.00",
                       "initial_count_fp": "5.00", "remaining_count_fp": "0.00"}
    ex.poll_open_orders(_HEDGEABLE, NOW, 1010.0)
    assert key not in ex.open_orders, "a fully-filled order must stop being tracked"
    assert any(r["event"] == "fill" for r in state.rows), "the fill must reach the ledger"
    assert ex.hedger.calls, "a detected fill must be routed to the hedger"


def test_poll_kalshi_fills_routes_account_fill_without_socket(tmp_path):
    """Account-wide /portfolio/fills sweep: one call, no socket, catches the fill."""
    koc = _KalshiOC()
    ex, key, koc, state = _rest_kalshi_order(tmp_path, koc=koc)
    _prime(ex)                                        # first sweep only primes the dedupe set
    oid = ex.open_orders[key].order_id
    koc.fills = [{"fill_id": "f1", "order_id": oid, "count_fp": "5.00", "side": "yes",
                  "yes_price_dollars": "0.5900", "ticker": "KX-1"}]
    assert ex.poll_kalshi_fills(_HEDGEABLE, NOW, 1010.0) == 1
    assert ex.hedger.calls, "the swept fill must be hedged"
    # Idempotent: the same fill_id must never be routed (and hedged) twice.
    n = len(ex.hedger.calls)
    ex.poll_kalshi_fills(_Store(), NOW, 1020.0)
    assert len(ex.hedger.calls) == n


def _prime(ex):
    """Consume the priming sweep (pre-existing fills are recorded, never replayed as surprises)."""
    ex.poll_kalshi_fills(_Store(), NOW, 1000.0)


def test_first_sweep_primes_and_does_not_replay_history(tmp_path):
    """A restart must not re-scream every fill from the last hour."""
    state = _State()
    koc = _KalshiOC()
    kx = _KalshiExec()
    kx.positions["KX-OLD"] = 0.0
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kx, state=state)
    koc.fills = [{"fill_id": "old1", "order_id": "gone", "count_fp": "5.00", "ticker": "KX-OLD"}]
    assert ex.poll_kalshi_fills(_Store(), NOW, 1000.0) == 0
    assert ex.orphan is None, "pre-existing fills must not latch an orphan on startup"
    ex.poll_kalshi_fills(_Store(), NOW, 1010.0)
    assert ex.orphan is None, "and must stay deduped on the next sweep"


def test_untracked_fill_on_FLAT_position_is_ledgered_but_not_an_orphan(tmp_path):
    """A socket-routed fill reaches the sweep unmatched. Flat at the venue => not naked => no halt."""
    state = _State()
    koc = _KalshiOC()
    kx = _KalshiExec()
    kx.positions["KX-1"] = 0.0                       # venue says flat
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kx, state=state)
    _prime(ex)
    koc.fills = [{"fill_id": "zz", "order_id": "ghost", "count_fp": "5.00", "side": "yes",
                  "yes_price_dollars": "0.5900", "ticker": "KX-1"}]
    ex.poll_kalshi_fills(_Store(), NOW, 1010.0)
    assert any(r["event"] == "fill_untracked" for r in state.rows), "still ledgered"
    assert ex.orphan is None, "a FLAT position is not an orphan"
    assert ex.caps.halted is False


def test_untracked_venue_fill_on_naked_position_latches_orphan(tmp_path):
    """The incident shape: a fill we never tracked, and the venue still shows contracts -> halt."""
    state = _State()
    koc = _KalshiOC()
    kx = _KalshiExec()
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kx, state=state)
    _prime(ex)
    kx.positions["KXMLSTEAMTOTAL-26JUL22SKCMIN-SKC3"] = 50.0        # NAKED at the venue
    koc.fills = [{"fill_id": "zz", "order_id": "ghost-oid", "count_fp": "50.00", "side": "yes",
                  "yes_price_dollars": "0.0200", "ticker": "KXMLSTEAMTOTAL-26JUL22SKCMIN-SKC3"}]
    ex.poll_kalshi_fills(_Store(), NOW, 1010.0)
    assert ex.orphan is not None, "an untracked fill on a NON-FLAT position must latch the orphan"
    assert ex.caps.halted is True
    assert any(r["event"] == "fill_untracked" for r in state.rows)
    assert "KXMLSTEAMTOTAL-26JUL22SKCMIN-SKC3" in ex._traded_tickers, "must be added to reconcile scope"


def test_untracked_fill_fails_closed_when_position_unreadable(tmp_path):
    """Cannot prove flat => treat as naked. Never silently assume flat."""
    koc = _KalshiOC()
    kx = _KalshiExec()
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kx)
    _prime(ex)

    def _boom():
        raise RuntimeError("positions endpoint down")

    kx.get_positions = _boom
    koc.fills = [{"fill_id": "zz", "order_id": "ghost", "count_fp": "5.00", "ticker": "KX-1"}]
    ex.poll_kalshi_fills(_Store(), NOW, 1010.0)
    assert ex.orphan is not None, "an unreadable position must fail CLOSED (orphan), not open"


def test_reconnect_polls_before_trusting_the_stream(tmp_path):
    """On every DOWN->UP edge we REST-poll first — a stream never replays what it missed."""
    koc = _KalshiOC()
    ex, key, koc, state = _rest_kalshi_order(tmp_path, koc=koc)
    oid = ex.open_orders[key].order_id
    koc.status[oid] = {"order_id": oid, "status": "executed", "fill_count_fp": "5.00"}
    ex.on_feed_reconnect("kalshi", _Store(), NOW, 1010.0)
    assert any(r["event"] == "fill" for r in state.rows), "reconnect must poll open orders"


# --------------------------------------------------------------------------- #
# 3. a filled order is NOT a cancelled order                                     #
# --------------------------------------------------------------------------- #
def test_cancel_of_filled_order_is_not_confirmed_and_forces_fill_poll(tmp_path):
    """The 11.5-hour spin: DELETE 404s, status reads 'executed'. Must classify as FILLED, not retry."""
    koc = _KalshiOC()
    ex, key, koc, state = _rest_kalshi_order(tmp_path, koc=koc)
    lo = ex.open_orders[key]
    koc.status[lo.order_id] = {"order_id": lo.order_id, "status": "executed", "fill_count_fp": "5.00"}

    def _raise_404(oid):
        raise RuntimeError("404 on DELETE /portfolio/events/orders/%s: not_found" % oid)

    koc.cancel = _raise_404
    ex._force_fill_poll = False
    assert ex._cancel(key, NOW, "hedge_thin_cooldown") is False   # not cancelled...
    assert ex._force_fill_poll is True, "a fill-not-cancel must force an immediate fill poll"
    # ...and the very next poll settles it instead of spinning forever.
    ex.poll_open_orders(_Store(), NOW, 1010.0)
    assert key not in ex.open_orders
    assert any(r["event"] == "fill" for r in state.rows)


# --------------------------------------------------------------------------- #
# 4. reconciliation must not read a real position as flat                        #
# --------------------------------------------------------------------------- #
def test_kalshi_position_reads_v2_fp_counts(tmp_path):
    kx = _KalshiExec()
    ex, _ = _exec_kalshi(tmp_path, kalshi=kx)
    kx.get_positions = lambda: {"market_positions": [{"ticker": "KX-1", "position_fp": "50.00"}]}
    assert ex._kalshi_position("KX-1") == 50.0


def test_unreadable_position_row_is_none_not_flat(tmp_path):
    """A row we found but cannot parse must NEVER read as flat — that is how a naked 50 got pruned."""
    kx = _KalshiExec()
    ex, _ = _exec_kalshi(tmp_path, kalshi=kx)
    kx.get_positions = lambda: {"market_positions": [{"ticker": "KX-1", "some_future_name": "50.00"}]}
    assert ex._kalshi_position("KX-1") is None
    assert ex._kalshi_position("KX-ABSENT") == 0.0        # genuinely absent == flat


def test_reconcile_keeps_watching_on_unreadable_position(tmp_path):
    kx = _KalshiExec()
    ex, _ = _exec_kalshi(tmp_path, kalshi=kx)
    ex._traded_tickers.add("KX-1")
    kx.get_positions = lambda: {"market_positions": [{"ticker": "KX-1", "unknown": "50.00"}]}
    ex.reconcile_positions(NOW)
    assert "KX-1" in ex._traded_tickers, "an unreadable read must not prune the watch-set"


def test_reconcile_flags_kalshi_orphan(tmp_path):
    kx = _KalshiExec()
    ex, _ = _exec_kalshi(tmp_path, kalshi=kx)
    ex._traded_tickers.add("KX-1")
    kx.positions["KX-1"] = 50.0
    assert ex.reconcile_positions(NOW) is not None
    assert ex.caps.halted is True


# --------------------------------------------------------------------------- #
# 5. the ORPHAN halt does not depend on Telegram                                 #
# --------------------------------------------------------------------------- #
def test_orphan_halts_and_persists_even_when_telegram_raises(tmp_path):
    """Telegram 429'd 1,682x in the incident window. The halt must land regardless."""
    def _boom(_text):
        raise RuntimeError("429 Too Many Requests")

    kx = _KalshiExec()
    ex, _ = _exec_kalshi(tmp_path, kalshi=kx)
    ex.telegram = _boom
    ex.log = _Log()
    ex._orphan_detected("G1", "KX-1", "pre", 50.0, "test", NOW)
    assert ex.orphan is not None, "orphan must latch"
    assert ex.caps.halted is True, "halt must land even though Telegram raised"
    path = tmp_path / "maker_rt_ORPHAN.json"
    assert path.exists(), "the panel banner file must be written without Telegram"
    assert json.loads(path.read_text())["remaining"] == 50.0


def test_persisted_orphan_relatches_on_restart(tmp_path):
    kx = _KalshiExec()
    ex, cfg = _exec_kalshi(tmp_path, kalshi=kx)
    ex._orphan_detected("G1", "KX-1", "pre", 50.0, "test", NOW)
    # A brand-new executor over the SAME ops dir (i.e. a restart) must come up HALTED.
    ex2 = PregameLiveExecutor(cfg, gate=None, order_client=None, hedger=None,
                              caps=__import__("src.genz.maker_rt.caps", fromlist=["LiveCaps"])
                              .LiveCaps(cfg.live), poly=_Poly(), in_flight=None, telegram=None,
                              state=_State(), log=None)
    assert ex2.orphan is not None, "a naked position must survive a restart"
    assert ex2.caps.halted is True


def test_traded_ticker_scope_persists_across_restart(tmp_path):
    """Scope persistence: the reconcile watch-set must survive a real restart."""
    koc = _KalshiOC()
    ex, key, koc, _ = _rest_kalshi_order(tmp_path, koc=koc)
    assert ex._traded_tickers, "placing a rest-kalshi order must add it to the watch-set"
    ticker = next(iter(ex._traded_tickers))
    ex2, cfg2 = _exec_kalshi(tmp_path)      # same ops dir => same persisted file
    assert ticker in ex2._traded_tickers


# --------------------------------------------------------------------------- #
# 6. the lost-callback root cause: feeds are wired AT CONSTRUCTION                #
# --------------------------------------------------------------------------- #
def test_spawn_feeds_attaches_on_fill_at_construction(monkeypatch):
    """ROOT CAUSE. Callbacks are constructor arguments, so a respawn cannot silently drop on_fill.

    856 universe rebuilds happened in the incident log; each one re-created the feeds. Both invisible
    fills landed 1-2 minutes after a rebuild, into a KalshiFeed whose on_fill was the default no-op."""
    from src.genz.maker_rt import __main__ as mrt_main

    monkeypatch.setattr(mrt_main, "_kalshi_ws_auth", lambda: ("kid", lambda m: "sig"))
    seen: list = []
    prints: list = []

    def _on_prints(p):
        prints.append(p)

    cfg = SimpleNamespace(ping_s=10)
    pm, ks = mrt_main._spawn_feeds(object(), [], cfg, None,
                                   on_prints=_on_prints, on_fill=seen.append)
    ks.on_fill({"kind": "kalshi_fill", "order_id": "o1", "count": 5})
    assert seen, "KalshiFeed must carry on_fill from construction"
    assert pm.on_prints is _on_prints and ks.on_prints is _on_prints

    # And the default (rest-poly-only) stays a safe no-op rather than raising.
    _pm2, ks2 = mrt_main._spawn_feeds(object(), [], cfg, None)
    ks2.on_fill({"kind": "kalshi_fill"})


# --------------------------------------------------------------------------- #
# 7. flap counter                                                                #
# --------------------------------------------------------------------------- #
def test_flap_counter_counts_cycles_and_downtime(tmp_path):
    ex, _ = _exec_kalshi(tmp_path)
    ex.log = _Log()
    ex.note_flap("kalshi", False, 100.0)     # DOWN
    ex.note_flap("kalshi", True, 104.5)      # UP 4.5s later
    assert ex.flaps["kalshi"] == 1
    assert ex.flap_secs["kalshi"] == 4.5
    ex.note_flap("kalshi", True, 110.0)      # UP while already up -> not a flap
    assert ex.flaps["kalshi"] == 1
    assert ex.snapshot(200.0)["flaps"]["kalshi"] == 1
