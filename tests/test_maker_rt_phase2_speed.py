"""AUDIT PHASE 2 (speed) regression suite — one or more test per item, built on the measured numbers.

The audit's speed findings were all the same shape: something synchronous on a single event loop, run
far more often than anyone had counted. So every test here pins a COUNT or an ATTRIBUTION, not a
duration — a wall-clock assertion would be a flake, while "40 ticks issue one DELETE" and "the paper
maker fetches zero extra books" are exactly the claims that were false before.

  * item 1  N18  cancel-retry storm: geometric backoff from a 5s floor, no venue I/O inside the window,
                 the DELETE off-loop and DECIDED on the loop, ``force`` for sweeps
  * item 2  F11  the paper maker reads the bid pricing already fetched
  * item 3  F10  the fill poll batched onto <=2 list calls and moved off the loop
  * item 4  Telegram queue + sender thread (ordering, non-blocking, never raises, flushed)
  * item 5  F12  the CSV expire row rides the same 300s throttle as its log line
  * item 6  the wrapper restarts in 1s after a deliberate exit-0 deploy and still backs off on a crash
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from src.executor.kalshi_exec import KalshiExecError
from src.genz import papermaker
from src.genz.engine import PricedVenue, _price_one
from src.genz.maker_rt import alerts
from src.genz.maker_rt.offloop import Worker
from src.genz.maker_rt.orders import KalshiOrderClient
from src.genz.maker_rt.pregame_exec import (CANCEL_RETRY_FLOOR_S, CANCEL_RETRY_MAX_S,
                                            REFUSE_LOG_EVERY_S, cancel_backoff_s)
from src.genz.maker_rt.tgqueue import TelegramQueue

from .test_maker_rt_pregame import (_KalshiOC, _Log, _Poly, _Store, _cand, _cand_kalshi, _dec, _exec,
                                    _exec_kalshi)

NOW = datetime(2026, 7, 30, 19, 0, 0, tzinfo=timezone.utc)
_STORE = _Store(kalshi_ask=0.60, poly_best_ask=0.55)
#: A rest-POLY decision whose hedge ask MATCHES the store's Kalshi book, so hedge depth is real and the
#: order actually rests (sizing refuses below the venue minimum when the hedge ladder is out of band).
_PDEC = lambda: _dec(0.38, hedge_ask=0.60)          # noqa: E731


def _settle(ex, tries: int = 300) -> None:
    """Let the off-loop worker finish, then drain on the (test's) loop thread."""
    for _ in range(tries):
        if ex._worker.pending() == 0:
            break
        time.sleep(0.01)
    ex._drain_cancel_results(_STORE, NOW, 0.0)


# --------------------------------------------------------------------------- #
# item 1 — N18: the cancel-retry storm                                          #
# --------------------------------------------------------------------------- #
def test_cancel_backoff_is_geometric_from_a_five_second_floor():
    """1.36 DELETE+GET per second was the measured storm. The FLOOR is the fix: no order's cancel is
    re-attempted faster than every 5s, and a persistently stuck one settles at the 120s ceiling."""
    assert cancel_backoff_s(1) == CANCEL_RETRY_FLOOR_S
    assert [cancel_backoff_s(n) for n in (2, 3, 4, 5)] == [10.0, 20.0, 40.0, 80.0]
    assert cancel_backoff_s(99) == CANCEL_RETRY_MAX_S
    assert cancel_backoff_s(0) == CANCEL_RETRY_FLOOR_S, "an unknown attempt count still gets the floor"


class _KVenue:
    """A Kalshi account whose DELETE can 404 while the order stays resting, counting every read."""

    def __init__(self, *, cancel_404: bool = True) -> None:
        self.resting: dict = {}
        self.terminal: dict = {}          # a real venue still reports a cancelled order (with its fills)
        self.cancel_404 = cancel_404
        self.cancels: list = []
        self.single_reads = 0
        self.list_reads = 0
        self._n = 0

    def place_order(self, ticker, side, count, price, *, action="buy", time_in_force=None,
                    post_only=False, client_order_id=None):
        self._n += 1
        oid = f"kx-{self._n}"
        self.resting[oid] = {"order_id": oid, "client_order_id": client_order_id, "ticker": ticker,
                             "side": side, "status": "resting", "count": count,
                             "remaining_count": count, "fill_count": 0}
        return {"status": "resting", "fill_count": 0, "avg_price": price, "order_id": oid}

    def cancel_order(self, oid):
        self.cancels.append(oid)
        if self.cancel_404:
            raise KalshiExecError('404 on DELETE /portfolio/orders/%s: {"code":"not_found"}' % oid)
        o = self.resting.pop(oid, None)
        if o is not None:
            self.terminal[oid] = dict(o, status="canceled")
        return {"order": {"order_id": oid, "status": "canceled"}}

    def get_order(self, oid):
        self.single_reads += 1
        return dict(self.resting.get(oid) or self.terminal.get(oid) or {})

    def get_orders(self, *, status=None, ticker=None):
        return {"orders": list(self.resting.values())}

    def list_resting(self, *, ticker=None, limit=200, max_pages=5):
        self.list_reads += 1
        return [o for o in self.resting.values() if ticker is None or o["ticker"] == ticker]

    def get_fills(self, *, min_ts=None, **kw):
        return []


def _kx(tmp_path, venue):
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=KalshiOrderClient(venue), kalshi=venue)
    ex.roll_day(NOW)
    return ex


def _ck(ticker="KX-1"):
    c = _cand_kalshi(ticker=ticker)
    c.key = ("mlb", "G1", "ml2", "Home", "rest-kalshi")
    return c


def test_a_tick_inside_the_backoff_window_does_no_venue_io_at_all(tmp_path):
    """This is N18's whole cost: the loop asked the venue about the same dead order ~4x/second. Inside
    the window the answer comes from nowhere — no DELETE, no GET, no resting list."""
    venue = _KVenue()
    ex = _kx(tmp_path, venue)
    c = _ck()
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.55), None, _STORE, NOW, 100.0, "pre")
    d2 = _dec(0.44, hedge_ask=0.55)
    d2.floor = 0.44                                       # resting 0.46 is above floor -> MANDATORY reprice
    ex.place_or_reprice(c, d2, None, _STORE, NOW, 140.0, "pre")     # attempt 1: inline, as always
    reads_after_first = venue.single_reads + venue.list_reads
    assert len(venue.cancels) == 1
    for i in range(1, 20):                                # 19 further ticks, all inside the 5s floor
        ex.place_or_reprice(c, d2, None, _STORE, NOW, 140.0 + i * 0.25, "pre")
    assert len(venue.cancels) == 1, "the storm is gone: no second DELETE inside the window"
    assert venue.single_reads + venue.list_reads == reads_after_first, "and no reads either"
    assert len(ex.open_orders) == 1 and ex.caps.open_quotes == 1, "the slot is still held"


def test_the_retry_delete_runs_off_loop_and_is_decided_on_the_loop(tmp_path):
    """(c) of the fix. The DELETE and its confirming reads happen on the worker; the slot is only freed
    when the LOOP drains the result — the one thread allowed to touch caps and open_orders."""
    venue = _KVenue()
    ex = _kx(tmp_path, venue)
    c = _ck()
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.55), None, _STORE, NOW, 100.0, "pre")
    d2 = _dec(0.44, hedge_ask=0.55)
    d2.floor = 0.44
    ex.place_or_reprice(c, d2, None, _STORE, NOW, 140.0, "pre")     # inline attempt 1 (404s)
    venue.cancel_404 = False                                        # the venue will honour the next one
    ex.place_or_reprice(c, d2, None, _STORE, NOW, 150.0, "pre")     # attempt 2: off-loop
    assert c.key in ex.open_orders, "nothing is freed until the LOOP decides"
    _settle(ex)
    assert c.key not in ex.open_orders and ex.caps.open_quotes == 0
    assert len(venue.cancels) == 2


def test_force_bypasses_the_backoff_so_a_shutdown_sweep_always_reaches_the_venue(tmp_path):
    """The one thing a backoff must never do: leave an order resting at shutdown. cancel_all forces."""
    venue = _KVenue()
    ex = _kx(tmp_path, venue)
    c = _ck()
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.55), None, _STORE, NOW, 100.0, "pre")
    d2 = _dec(0.44, hedge_ask=0.55)
    d2.floor = 0.44
    ex.place_or_reprice(c, d2, None, _STORE, NOW, 140.0, "pre")     # burns attempt 1 -> now backing off
    n = len(venue.cancels)
    venue.cancel_404 = False
    ex.cancel_all("shutdown", NOW)
    assert len(venue.cancels) == n + 1, "the shutdown DELETE went out DESPITE the open backoff window"
    assert not ex.open_orders and not venue.resting


def test_cancel_not_confirmed_warning_is_throttled_to_one_per_window(tmp_path):
    """22,942 WARNING lines in one day for a handful of orders is the storm wearing a log's clothes."""
    venue = _KVenue()
    ex = _kx(tmp_path, venue)
    ex.log = _Log()
    c = _ck()
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.55), None, _STORE, NOW, 100.0, "pre")
    d2 = _dec(0.44, hedge_ask=0.55)
    d2.floor = 0.44
    for i in range(20):                        # 20 ticks = the whole 5s floor, one window
        ex.place_or_reprice(c, d2, None, _STORE, NOW, 140.0 + i * 0.25, "pre")
    lines = [w for w in ex.log.warns if "cancel NOT confirmed" in w]
    assert len(lines) == 1, f"20 ticks in one window produced {len(lines)} lines"
    assert "next retry in" in lines[0], "the line has to say when it WILL retry"
    ex.place_or_reprice(c, d2, None, _STORE, NOW, 146.0, "pre")   # a REAL retry -> speaks again
    assert len([w for w in ex.log.warns if "cancel NOT confirmed" in w]) == 2


def test_a_confirmed_cancel_forgets_its_retry_state(tmp_path):
    """Otherwise a re-placed order inherits the dead one's attempt count and skips its inline first try."""
    venue = _KVenue(cancel_404=False)
    ex = _kx(tmp_path, venue)
    c = _ck()
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.55), None, _STORE, NOW, 100.0, "pre")
    assert ex.cancel(c, NOW, "reprice") is True
    assert c.key not in ex._cancel_attempts and c.key not in ex._cancel_next_ts


def test_a_raced_fill_is_still_routed_before_the_cancel_is_honoured(tmp_path):
    """N9 must survive the rework. The INLINE first attempt keeps its own venue read, so a fill that
    raced into the cancel window is hedged before any decision about the cancel is made."""
    venue = _KVenue(cancel_404=False)
    ex = _kx(tmp_path, venue)
    c = _ck()
    ex.place_or_reprice(c, _dec(0.46, hedge_ask=0.55), None, _STORE, NOW, 100.0, "pre")
    oid = ex.open_orders[c.key].order_id
    # The N9 shape: the fill lands while our DELETE is in flight, so the venue reports the order CANCELED
    # and carrying a fill count. The cancel must not be honoured until that fill has been hedged.
    venue.resting[oid] = dict(venue.resting[oid], fill_count=1, remaining_count=0)
    d2 = _dec(0.44, hedge_ask=0.55)
    d2.floor = 0.44                                   # mandatory reprice -> the real cancel path, with a book
    ex.place_or_reprice(c, d2, None, _STORE, NOW, 200.0, "pre")
    # ROUTED is the claim, not which way the chain then went (this book is below the decline floor, so it
    # unwinds rather than hedges). The pre-N9 bug discarded the delta entirely and left it naked.
    assert [r for r in ex.state.rows if r.get("event") == "fill"], "the raced fill reached the chain"
    assert ex._force_fill_poll is True, "and the poll was forced to re-read it as a belt"


# --------------------------------------------------------------------------- #
# item 3 — F10: the fill poll, batched and off the loop                          #
# --------------------------------------------------------------------------- #
class _BatchPoly(_Poly):
    """A PolyExec stand-in that can LIST its resting orders (``GET /data/orders``) and counts both reads."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.open: dict = {}                  # oid -> order row
        self.list_reads = 0
        self.single_reads = 0
        self.list_raises = False

    def open_orders(self, **kw):
        self.list_reads += 1
        if self.list_raises:
            raise RuntimeError("data/orders unreachable")
        return list(self.open.values())

    def get_order(self, oid):
        self.single_reads += 1
        return dict(self.open.get(oid) or {})


def _poly_exec(tmp_path, poly):
    ex, _ = _exec(tmp_path, poly=poly)
    ex.roll_day(NOW)
    # Room for the fixture's several concurrent orders. A batched poll is only interesting with N open
    # orders in it, and the default unit-test caps allow two.
    ex.caps.max_open_quotes = 20
    ex.caps.max_daily_stake_usd = 10_000.0
    return ex


def _rest_poly(ex, poly, *, n=6, matched=0.0):
    """Place ``n`` tracked rest-poly orders and mirror them into the venue's open-orders list."""
    keys = []
    for i in range(n):
        c = _cand(token=f"TOK{i}")
        c.key = ("mlb", f"G{i}", "ml2", "Home", "rest-poly")
        c.game = f"G{i}"                       # distinct games: this fixture is about batching, not concentration
        ex.place_or_reprice(c, _PDEC(), None, _STORE, NOW, 100.0, "pre")
        lo = ex.open_orders[c.key]
        poly.open[lo.order_id] = {"id": lo.order_id, "status": "LIVE", "size_matched": matched,
                                  "price": lo.price}
        keys.append(c.key)
    return keys


def test_the_batched_poll_replaces_n_per_order_reads_with_one_list_call(tmp_path):
    """The audit's dominant stall: one synchronous per-order GET for every open order, every 10s. Six
    open orders used to be six reads; now it is ONE list call and zero per-order reads."""
    poly = _BatchPoly()
    ex = _poly_exec(tmp_path, poly)
    _rest_poly(ex, poly, n=6)
    poly.single_reads = poly.list_reads = 0
    ex.submit_fill_poll(200.0)
    _settle(ex)
    assert poly.list_reads == 1
    assert poly.single_reads == 0, "no per-order read for an order the batch already described"
    assert len(ex.open_orders) == 6, "nothing was released"


def test_an_order_absent_from_the_batch_gets_its_authoritative_per_order_read(tmp_path):
    """Absence from the resting list is exactly when 'did it FILL or was it cancelled?' matters, so the
    batch is not allowed to answer it — the per-order read still happens (off the loop) for that order
    alone. Without this, a fully-filled order would look identical to a cancelled one."""
    poly = _BatchPoly()
    ex = _poly_exec(tmp_path, poly)
    keys = _rest_poly(ex, poly, n=3)
    gone = ex.open_orders[keys[0]]
    poly.open.pop(gone.order_id)                       # terminal: no longer listed by /data/orders
    poly.single_reads = poly.list_reads = 0
    ex.submit_fill_poll(200.0)
    _settle(ex)
    assert poly.list_reads == 1
    assert poly.single_reads == 1, "exactly the ONE absent order was read individually"


def test_a_batched_fill_is_routed_exactly_once(tmp_path):
    """All fill-routing semantics unchanged: matched_seen stays a venue high-water mark, so re-applying
    the same batch cannot hedge twice."""
    poly = _BatchPoly()
    ex = _poly_exec(tmp_path, poly)
    keys = _rest_poly(ex, poly, n=1)
    lo = ex.open_orders[keys[0]]
    poly.open[lo.order_id]["size_matched"] = "5"       # Poly sends this as a STRING
    ex.submit_fill_poll(200.0)
    _settle(ex)
    assert len(ex.hedger.calls) == 1
    ex.submit_fill_poll(210.0)                          # same state observed again
    _settle(ex)
    assert len(ex.hedger.calls) == 1, "the second observation of one execution hedges nothing"


def test_a_failed_list_read_degrades_to_per_order_reads_and_frees_nothing(tmp_path):
    """A batch is an optimisation, never a new way to lose a read: if the list call fails the job falls
    back to exactly the old per-order reads (still off the loop)."""
    poly = _BatchPoly()
    ex = _poly_exec(tmp_path, poly)
    _rest_poly(ex, poly, n=4)
    poly.list_raises = True
    poly.single_reads = 0
    ex.log = _Log()
    ex.submit_fill_poll(200.0)
    _settle(ex)
    assert poly.single_reads == 4, "every order still got an authoritative read"
    assert len(ex.open_orders) == 4, "and no slot was freed on a venue we could not list"


def test_an_unreadable_order_never_frees_its_slot_in_the_batched_path(tmp_path):
    """FAIL CLOSED is the whole ghost-order lesson: 'we could not read it' must never become 'it is
    gone'. A per-order read that raised comes back as unreadable and the order stays tracked."""
    poly = _BatchPoly()
    ex = _poly_exec(tmp_path, poly)
    keys = _rest_poly(ex, poly, n=1)
    lo = ex.open_orders[keys[0]]
    batch = {"index": {}, "per_order": {str(lo.order_id): None}, "venue_ok": {"polymarket": True}}
    ex.poll_open_orders(_STORE, NOW, 100.0 + ex._stale_grace_s + 1, snapshot=batch)
    assert keys[0] in ex.open_orders and ex._slot_released == 0


def test_an_order_the_venue_positively_does_not_list_is_released(tmp_path):
    """The other half of fail-closed: a SUCCESSFUL list that does not mention our order is venue truth
    that it is no longer resting, and holding that slot forever was its own (measured) leak."""
    poly = _BatchPoly()
    ex = _poly_exec(tmp_path, poly)
    keys = _rest_poly(ex, poly, n=1)
    batch = {"index": {}, "per_order": {}, "venue_ok": {"polymarket": True}}
    ex.poll_open_orders(_STORE, NOW, 100.0 + ex._stale_grace_s + 1, snapshot=batch)
    assert keys[0] not in ex.open_orders and ex._slot_released == 1


def test_the_fills_low_water_mark_advances_to_the_read_ts_not_the_apply_ts(tmp_path):
    """N22 with a new edge: an off-loop read makes 'when we looked' and 'when we decided' different
    timestamps for the first time. Advancing to the later one would skip a window nobody read."""
    koc = _KalshiOC()
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc)
    ex.roll_day(NOW)
    ex._last_fills_sweep_ts = 1000.0
    batch = {"fills": [], "fills_read": True, "since": 995, "polled_ts": 1010.0}
    ex.poll_kalshi_fills(_STORE, NOW, 1099.0, batch=batch)
    assert ex._last_fills_sweep_ts == 1010.0, "the mark is where the READ happened"


def test_a_failed_fills_read_does_not_advance_the_window(tmp_path):
    koc = _KalshiOC()
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc)
    ex.roll_day(NOW)
    ex._last_fills_sweep_ts = 1000.0
    ex.poll_kalshi_fills(_STORE, NOW, 1100.0,
                         batch={"fills": None, "fills_read": True, "since": 995, "polled_ts": 1010.0})
    assert ex._last_fills_sweep_ts == 1000.0


def test_a_forced_fill_poll_is_consumed_by_the_submit_not_the_apply(tmp_path):
    """A cancel that raced a fill sets the force flag. If the APPLY cleared it, a request raised between
    submit and apply would be silently dropped — the flag has to be consumed where it is acted on."""
    poly = _BatchPoly()
    ex = _poly_exec(tmp_path, poly)
    _rest_poly(ex, poly, n=1)
    ex._force_fill_poll = True
    assert ex.needs_fill_poll() is True
    ex.submit_fill_poll(200.0)
    assert ex.needs_fill_poll() is False
    ex._force_fill_poll = True                      # raised AFTER the submit
    _settle(ex)
    assert ex.needs_fill_poll() is True, "the later request survived the apply"


def test_the_legacy_inline_poll_still_works_without_a_snapshot(tmp_path):
    """The old path is the fallback for any client that cannot list, and every existing test drives it."""
    poly = _Poly(order_status="LIVE", size_matched=5.0)
    ex = _poly_exec(tmp_path, poly)
    c = _cand()
    ex.place_or_reprice(c, _PDEC(), None, _STORE, NOW, 100.0, "pre")
    assert c.key in ex.open_orders
    ex.poll_open_orders(_STORE, NOW, 200.0)
    assert len(ex.hedger.calls) == 1


# --------------------------------------------------------------------------- #
# the off-loop worker itself                                                     #
# --------------------------------------------------------------------------- #
def test_the_worker_dedupes_by_key_so_retries_cannot_stack():
    """The property the cancel path depends on: a tick that asks again while a DELETE is in flight must
    not queue a second one. That is how five identical bids stacked on 2026-07-25."""
    w = Worker()
    gate = threading.Event()
    assert w.submit("k", gate.wait) is True
    assert w.submit("k", gate.wait) is False, "a key already in flight is not queued again"
    gate.set()
    for _ in range(300):
        if w.pending() == 0:
            break
        time.sleep(0.01)
    assert w.submit("k", lambda: 1) is True, "once it lands, the key is submittable again"
    w.close()


def test_a_raising_job_is_a_result_not_a_dead_worker():
    w = Worker()
    w.submit("boom", lambda: (_ for _ in ()).throw(ValueError("venue 500")))
    for _ in range(300):
        got = w.drain()
        if got:
            break
        time.sleep(0.01)
    assert got and got[0][0] == "boom" and isinstance(got[0][2], ValueError)
    assert w.submit("next", lambda: "alive") is True
    w.close()


def test_the_worker_thread_starts_only_on_first_submit():
    """A shadow run (or a test that merely builds an executor) must not grow a thread it never uses."""
    w = Worker()
    assert w._thread is None
    w.submit("x", lambda: 1)
    assert w._thread is not None
    w.close()


# --------------------------------------------------------------------------- #
# item 4 — Telegram: a chat outage is not a trading outage                       #
# --------------------------------------------------------------------------- #
def test_enqueue_returns_immediately_even_when_the_send_blocks():
    """The measured worst case was a 64s block, taken at exactly a fill/halt moment."""
    gate = threading.Event()
    q = TelegramQueue(lambda t: gate.wait(5.0))
    t0 = time.monotonic()
    for i in range(5):
        q(f"alert {i}")
    assert time.monotonic() - t0 < 0.5, "the loop paid ~nothing"
    gate.set()
    q.close()


def test_order_is_preserved_by_the_single_sender_thread():
    """fill -> hedge -> halt has to arrive in that order; a pool would not guarantee it."""
    got: list = []
    q = TelegramQueue(got.append)
    for i in range(50):
        q(f"m{i}")
    q.close()
    assert got == [f"m{i}" for i in range(50)]


def test_a_failing_send_never_raises_into_the_caller():
    def _boom(_t):
        raise RuntimeError("telegram 500")
    log = _Log()
    q = TelegramQueue(_boom, log=log)
    q("anything")
    q.close()
    assert q.failed == 1 and q.sent == 0
    assert any("telegram send failed" in w for w in log.warns)


def test_close_flushes_the_backlog():
    """A deploy is exactly when the last alert matters, so 'queued' must not become 'lost'."""
    got: list = []
    q = TelegramQueue(got.append)
    for i in range(20):
        q(f"m{i}")
    assert q.close() == 0
    assert len(got) == 20


def test_a_send_after_close_goes_out_inline():
    """Past shutdown there is no thread to hand it to; the shutdown alert still has to be delivered."""
    got: list = []
    q = TelegramQueue(got.append)
    q.close()
    q("shutdown cancel-all: 3 orders")
    assert got == ["shutdown cancel-all: 3 orders"]


def test_a_full_queue_drops_the_oldest_not_the_newest():
    """If Telegram is down, the newest state of a live trading process is the one an operator needs."""
    gate = threading.Event()
    q = TelegramQueue(lambda t: gate.wait(5.0), max_queue=3)
    for i in range(10):
        q(f"m{i}")
    assert q.dropped >= 6
    gate.set()
    q.close()


# --------------------------------------------------------------------------- #
# item 5 — F12: the CSV expire row rides the log's throttle                       #
# --------------------------------------------------------------------------- #
def test_the_slot_refusal_csv_row_rides_the_same_300s_throttle_as_its_log(tmp_path):
    """The log was throttled to 1/300s and the CSV row next to it was not, so the same refusal still
    wrote ~117,000 identical rows a day — 58% of the file. One row per window says the same thing."""
    ex, _ = _exec(tmp_path)
    ex.roll_day(NOW)
    ex.caps.max_open_quotes = 0                    # every candidate is refused for a slot
    c = _cand()
    for i in range(120):                           # 30s of driver ticks
        ex.place_or_reprice(c, _PDEC(), None, _STORE, NOW, 500.0 + i * 0.25, "pre")
    rows = [r for r in ex.state.rows if r.get("event") == "expire"]
    assert len(rows) == 1, f"{len(rows)} expire rows for one throttled refusal"
    assert rows[0]["reason"] == "max_open_quotes"
    assert ex._digest["refuse_suppressed"] == 119, "and every suppressed hit is still counted"
    ex.place_or_reprice(c, _PDEC(), None, _STORE, NOW, 500.0 + REFUSE_LOG_EVERY_S + 1, "pre")
    assert len([r for r in ex.state.rows if r.get("event") == "expire"]) == 2, "a new window records again"


def test_a_non_slot_refusal_is_never_throttled(tmp_path):
    """below_venue_minimum is a per-candidate economic fact, not a recurring full-slot condition — it
    keeps writing its row every time so the binding-constraint record stays complete."""
    ex, _ = _exec(tmp_path)
    ex.roll_day(NOW)
    ex.caps.quote_usd_max = 0.01                   # nothing fits the venue minimum
    c = _cand()
    for i in range(5):
        ex.place_or_reprice(c, _PDEC(), None, _STORE, NOW, 500.0 + i, "pre")
    rows = [r for r in ex.state.rows if r.get("reason") == "below_venue_minimum"]
    assert len(rows) == 5


def test_suppressed_slot_refusals_surface_in_the_digest_line():
    """It was incremented and never read — the only signal that says 'more slots would mean more offers'."""
    line = alerts.digest_line(15, placed=3, cancelled=1, fills=0, open_now=12, max_open=12,
                              refuse_suppressed=3042)
    assert "3042" in line and "every slot was busy" in line
    assert "every slot was busy" not in alerts.digest_line(
        15, placed=3, cancelled=1, fills=0, open_now=1, max_open=12), "silent when there is nothing to say"


# --------------------------------------------------------------------------- #
# item 2 — F11: the paper maker stops re-downloading books pricing already read   #
# --------------------------------------------------------------------------- #
class _CountingMD:
    """An md that reports BOTH sides from one read (the new contract) and counts every book fetch."""

    def __init__(self, *, bid=0.44, ask=0.46) -> None:
        self.bid, self.ask = bid, ask
        self.book_reads = 0
        self.bid_only_reads = 0

    def poly_quote(self, token):
        self.book_reads += 1
        return [(self.ask, 500.0)], self.bid

    def kalshi_quote(self, ticker, side="YES"):
        self.book_reads += 1
        return [(self.ask, 500.0)], self.bid

    def poly_ask_ladder(self, token):
        self.book_reads += 1
        return [(self.ask, 500.0)]

    def kalshi_ask_ladder(self, ticker, side="YES"):
        self.book_reads += 1
        return [(self.ask, 500.0)]

    def poly_best_bid(self, token):
        self.bid_only_reads += 1
        return self.bid

    def kalshi_best_bid(self, ticker, side="YES"):
        self.bid_only_reads += 1
        return self.bid


class _AskOnlyMD(_CountingMD):
    """An md from before the change (or an injected double): ask ladders only, no combined reader."""
    poly_quote = None
    kalshi_quote = None


def test_priced_venue_carries_the_bid_from_the_same_book_read():
    """Both venues answer one request with bids AND asks; pricing used to throw the bids away."""
    md = _CountingMD(bid=0.44, ask=0.46)
    pv = _price_one(md, "poly", "TOK", "BUY", 100.0)
    assert (pv.best_ask, pv.best_bid, pv.bid_read) == (0.46, 0.44, True)
    assert md.book_reads == 1 and md.bid_only_reads == 0


def test_an_unpriced_node_still_carries_its_bid():
    """An empty ASK ladder does not invalidate the bid that came back with it, and the paper maker's
    join decision is a bid-side question."""
    class _NoAsks(_CountingMD):
        def poly_quote(self, token):
            self.book_reads += 1
            return [], 0.44
    md = _NoAsks()
    pv = _price_one(md, "poly", "TOK", "BUY", 100.0)
    assert pv.priced is False and pv.best_bid == 0.44 and pv.bid_read is True


def test_bid_read_distinguishes_an_empty_bid_side_from_never_asked():
    """None is a legitimate answer, so a consumer must not use it to mean 'not plumbed' — only the flag
    can tell 'the venue says there are no bids' from 'nobody looked'."""
    plumbed = PricedVenue("poly", "T", 0.46, 0.46, 1.0, best_bid=None, bid_read=True)
    legacy = PricedVenue("poly", "T", 0.46, 0.46, 1.0)
    md = _CountingMD()
    assert papermaker._bid_for(plumbed, md, "polymarket", {"poly_token_id": "T"}) is None
    assert md.bid_only_reads == 0, "an empty bid side is an ANSWER — never re-fetch it"
    assert papermaker._bid_for(legacy, md, "polymarket", {"poly_token_id": "T"}) == 0.44
    assert md.bid_only_reads == 1, "only an md that cannot report bids pays for a read"


def test_the_paper_maker_fetches_zero_extra_books_when_the_bid_is_plumbed():
    """The measurement: 847s of a 997s soccer cycle was this redundant serial re-download."""
    md = _CountingMD(bid=0.44, ask=0.55)
    m = SimpleNamespace(game="G1", market_key="ml2", sides={
        "Home": {"poly_token_id": "TA", "kalshi_ticker": "KA", "kalshi_side": "yes", "tick_size": 0.01},
        "Away": {"poly_token_id": "TB", "kalshi_ticker": "KB", "kalshi_side": "no", "tick_size": 0.01}})
    priced = {}
    for venue, ident, side in (("poly", "TA", "BUY"), ("poly", "TB", "BUY"),
                               ("kalshi", "KA", "yes"), ("kalshi", "KB", "no")):
        priced[(venue, ident, side)] = _price_one(md, venue, ident, side, 100.0)
    priced_reads = md.book_reads
    pm = papermaker.PaperMaker(target_net_pct=1.0)
    pm.observe([m], priced, md, NOW)
    assert md.bid_only_reads == 0, "not one bid-only order-book fetch"
    assert md.book_reads == priced_reads, "the paper maker added no venue read of any kind"
    assert pm.n_quotes > 0, "and it still quoted — this is not a no-op"


def test_the_paper_maker_still_works_against_an_ask_only_md():
    """Backwards compatible on purpose: an md without the combined reader keeps its old behaviour."""
    md = _AskOnlyMD(bid=0.44, ask=0.55)
    m = SimpleNamespace(game="G1", market_key="ml2", sides={
        "Home": {"poly_token_id": "TA", "kalshi_ticker": "KA", "kalshi_side": "yes", "tick_size": 0.01},
        "Away": {"poly_token_id": "TB", "kalshi_ticker": "KB", "kalshi_side": "no", "tick_size": 0.01}})
    priced = {(v, i, s): _price_one(md, v, i, s, 100.0)
              for v, i, s in (("poly", "TA", "BUY"), ("poly", "TB", "BUY"),
                              ("kalshi", "KA", "yes"), ("kalshi", "KB", "no"))}
    assert all(pv.bid_read is False for pv in priced.values())
    pm = papermaker.PaperMaker(target_net_pct=1.0)
    pm.observe([m], priced, md, NOW)
    assert md.bid_only_reads > 0 and pm.n_quotes > 0


def test_market_data_reads_one_book_for_both_sides():
    """The real MarketData: kalshi_quote/poly_quote must hit the venue ONCE, not once per side."""
    from src.executor.resolve import MarketData

    class _K:
        def __init__(self):
            self.n = 0

        def orderbook(self, ticker, **kw):
            self.n += 1
            return {"orderbook": {"yes": [[44, 100]], "no": [[54, 100]]}}

    class _P:
        def __init__(self):
            self.n = 0

        def book(self, token):
            self.n += 1
            return {"asks": [{"price": "0.46", "size": "500"}],
                    "bids": [{"price": "0.44", "size": "500"}]}

    k, p = _K(), _P()
    md = MarketData(kalshi_client=k, poly_client=p)
    asks, bid = md.poly_quote("TOK")
    assert asks == [(0.46, 500.0)] and bid == 0.44 and p.n == 1
    md.kalshi_quote("KX", "yes")
    assert k.n == 1


# --------------------------------------------------------------------------- #
# item 6 — restart economics                                                     #
# --------------------------------------------------------------------------- #
def test_the_wrapper_restarts_fast_after_a_deliberate_exit_zero():
    """Exit 0 is a HEAD-change deploy that already cancelled every resting order, 11-21x/day. A crash
    (or the singleton's exit 3) must still back off, or a crash-loop spins."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "scripts", "run_maker_rt_loop.ps1"),
               encoding="utf-8").read()
    assert "$LASTEXITCODE -eq 0" in src
    assert "Start-Sleep -Seconds 1" in src and "Start-Sleep -Seconds 5" in src
    assert "Start-Sleep -Seconds 5 }" not in src.replace("else { Start-Sleep -Seconds 5 }", ""), (
        "the unconditional 5s sleep is gone")


def test_a_stalled_off_loop_fill_poll_screams(tmp_path):
    """The price of moving the fill authority off the loop, paid deliberately. A venue read that HANGS
    used to freeze the whole loop — catastrophic, but impossible to miss. On a worker thread the same
    hang is SILENT: quoting, repricing and the heartbeat all keep working while the primary fill
    detector is simply gone. So the loop watches the clock on it."""
    poly = _BatchPoly()
    ex = _poly_exec(tmp_path, poly)
    ex.log = _Log()
    alerts_sent: list = []
    ex.telegram = alerts_sent.append
    _rest_poly(ex, poly, n=1)
    ex.submit_fill_poll(1000.0)
    ex.drain_offloop(_STORE, NOW, 1000.0)               # a fresh poll is not a stall
    assert not [w for w in ex.log.warns if "STALLED" in w]
    ex._fill_poll_applied_ts = 1000.0                   # nothing has landed since
    ex.drain_offloop(_STORE, NOW, 1000.0 + 4 * ex.fill_poll_s + 1)
    assert any("STALLED" in w for w in ex.log.warns)
    assert alerts_sent and "safety net" in alerts_sent[0]


def test_the_stall_alert_is_throttled(tmp_path):
    """It fires every tick while the condition holds; the operator needs one line, not 240 a minute."""
    poly = _BatchPoly()
    ex = _poly_exec(tmp_path, poly)
    ex.log = _Log()
    ex.telegram = lambda t: None
    ex._fill_poll_applied_ts = 1000.0
    for i in range(50):
        ex.drain_offloop(_STORE, NOW, 1000.0 + 4 * ex.fill_poll_s + 1 + i * 0.25)
    assert len([w for w in ex.log.warns if "STALLED" in w]) == 1


def test_a_failed_batch_is_not_a_stall(tmp_path):
    """The watchdog is about SILENCE. A venue that answers with an error is answering."""
    poly = _BatchPoly()
    ex = _poly_exec(tmp_path, poly)
    ex.log = _Log()
    ex._fill_poll_applied_ts = 1000.0
    ex._apply_fill_poll_batch(None, RuntimeError("venue 500"), _STORE, NOW, 1000.0 + 90.0)
    assert ex._fill_poll_applied_ts == 1090.0
    ex.drain_offloop(_STORE, NOW, 1090.0 + 1)
    assert not [w for w in ex.log.warns if "STALLED" in w]


# --------------------------------------------------------------------------- #
# the batch against the REAL row shapes (live-verified 2026-07-30 14:53Z)        #
# --------------------------------------------------------------------------- #
#: Exactly the keys the two venues returned for OUR resting orders, captured from the live account.
#: This is the audit's open question 6 answered: /data/orders DOES carry size_matched (as a string),
#: and Kalshi's resting rows carry fill_count_fp — so the batch can answer "how much is matched?" for
#: both venues and never has to fall back to a per-order read on the common path.
_REAL_POLY_ROW = {
    "asset_id": "3308727807874721762", "associate_trades": None, "created_at": 1785500000,
    "expiration": "0", "id": "0x723972abc2951c0db860132245067b430554d61bf50ce5875ccee6a2b33e9095",
    "maker_address": "0x0", "market": "0xmarket", "order_type": "GTC", "original_size": "24",
    "outcome": "Yes", "owner": "owner", "price": "0.52", "side": "BUY", "size_matched": "0",
    "status": "LIVE"}
_REAL_KALSHI_ROW = {
    "action": "buy", "book_side": "yes", "client_order_id": "mrt-3-1785500000000",
    "created_time": "2026-07-30T14:40:00Z", "exchange_index": 1, "fill_count_fp": "0.00",
    "initial_count_fp": "51.00", "last_update_time": "2026-07-30T14:40:00Z",
    "maker_fees_dollars": "0.00", "maker_fill_cost_dollars": "0.00", "no_price_dollars": "0.56",
    "order_id": "kx-real-1", "outcome_side": "yes", "remaining_count_fp": "51.00",
    "self_trade_prevention_type": "taker_at_cross", "side": "yes", "status": "resting",
    "subaccount_number": 0, "taker_fees_dollars": "0.00", "taker_fill_cost_dollars": "0.00",
    "ticker": "KXCLUBFTOTAL-26JUL30X-3", "type": "limit", "user_id": "u", "yes_price_dollars": "0.44"}


def test_the_batch_can_answer_from_the_real_venue_row_shapes(tmp_path):
    """If a venue row cannot answer 'how much is matched?', the job falls back to a per-order read for
    that order — correct, but it would silently undo the batching. These are the real shapes."""
    poly = _BatchPoly()
    ex = _poly_exec(tmp_path, poly)
    keys = _rest_poly(ex, poly, n=1)
    lo = ex.open_orders[keys[0]]
    assert ex._order_matched(lo, _REAL_POLY_ROW) == 0.0, "Poly /data/orders answers the matched question"
    assert ex._resting_order_id(_REAL_POLY_ROW) == _REAL_POLY_ROW["id"]
    klo = SimpleNamespace(rest_venue="kalshi", size=51.0, order_id="kx-real-1")
    assert ex._order_matched(klo, _REAL_KALSHI_ROW) == 0.0, "fill_count_fp is read, not skipped"
    assert ex._resting_order_id(_REAL_KALSHI_ROW) == "kx-real-1"


def test_a_row_that_cannot_answer_falls_back_to_a_per_order_read(tmp_path):
    """A batch row with no readable matched count is not a cheaper read, it is a BLIND one — and a blind
    read that looks successful is the 2026-07-23 invisible-fill class. Enforced per ROW, not per venue,
    so a venue quietly dropping the field degrades to correctness rather than to silence."""
    poly = _BatchPoly()
    ex = _poly_exec(tmp_path, poly)
    keys = _rest_poly(ex, poly, n=1)
    lo = ex.open_orders[keys[0]]
    poly.open[lo.order_id].pop("size_matched")          # the venue stops sending it
    poly.single_reads = poly.list_reads = 0
    ex.submit_fill_poll(200.0)
    _settle(ex)
    assert poly.list_reads == 1 and poly.single_reads == 1, "the blind row was backed by a real read"
