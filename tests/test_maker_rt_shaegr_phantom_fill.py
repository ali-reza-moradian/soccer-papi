"""THE SHAEGR PHANTOM-FILL CLASS — a rest fill the venue REPORTED and never DELIVERED.

2026-08-04, KXUELTOTAL-26AUG06SHAEGR-4 ("Shamrock Rovers FC vs. KF Egnatia Rrogozhinë: O/U 3.5"):

  18:36:33Z  a REAL rest-poly fill of 109 Under @74c, hedged 109 Kalshi Over @24c. A correct +0.72% pair.
  18:40:57Z  the REST FILL POLL read ``GET /data/order/0x7b5374…`` and saw ``size_matched`` 116 on a
             SECOND order. The bot hedged it with 116 REAL Kalshi contracts ($29.32) and announced
             "profit is GUARANTEED either way".

VENUE TRUTH, re-read read-only on 2026-08-05 (a day later):
  * ``conditional_balance`` on the Under token = 109.0 — NOT 225. It never moved.
  * ``data-api /activity`` has exactly ONE row on that token ever: BUY 109, usdcSize 80.66,
    tx 0xa0ddef2581cf45c2746a8a8c741cea1ba9af214244d41ce2b486ac9c562ac900.
So the 116 was not slow. It never existed. 116 Kalshi contracts rode NAKED to settlement and won
$86.68 on pure luck.

MECHANISM: ``size_matched`` is set when the book MATCHES. Polymarket's trade lifecycle is
MATCHED -> MINED -> CONFIRMED, and a MATCHED trade can still FAIL — at which point nothing walks the
order's ``size_matched`` back down. Every one of our three fill detectors reads that field, so this is a
property of the FIELD, not of any one detector.

THE FIX these tests pin: the hedge still fires on the signal (speed is the edge), but the PAIR is not
booked until the venue confirms the rest leg was delivered.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from .test_maker_rt_pregame import (_Guard, _KalshiOC, _Log, _Poly, _State, _Store, _dec,
                                    _exec_kalshi)

_DT = datetime(2026, 8, 4, 18, 36, 33, tzinfo=timezone.utc)
#: the real Polymarket token id + Kalshi ticker from the incident
_UNDER = "33024417091507491293411269778630975545719422039388295867790359972503755572161"
_TICKER = "KXUELTOTAL-26AUG06SHAEGR-4"
_REST_PX = 0.74
_HEDGE_PX = 0.24
#: rest 0.74 + hedge 0.24 = $0.98/share -> ~+0.72% after the Kalshi taker fee. The production numbers.
#: The POLY ask must sit above our 74c rest bid or the never-cross guard refuses to place at all.
_BOOKS = _Store(poly_best_ask=0.76, kalshi_ask=_HEDGE_PX)


class _Kalshi:
    """Kalshi portfolio + IOC sells that MOVE THE POSITION, so a keep-floor can be observed.

    The shared ``_KalshiExec`` fake zeroes the ticker on any sell, which cannot express the thing this
    file is about: selling 116 contracts off a 225 position and correctly stopping at the 109 held by an
    EARLIER, genuine pair."""

    def __init__(self, sell_price=0.24):
        self.market_sells: list = []
        self.sell_price = sell_price
        self.positions: dict = {}

    def place_market_sell(self, ticker, side, count, client_order_id=None):
        self.market_sells.append({"ticker": ticker, "side": side, "count": count})
        self.positions[ticker] = max(0.0, float(self.positions.get(ticker, 0.0)) - float(count))
        return {"status": "filled", "fill_count": count, "avg_price": self.sell_price,
                "order_id": f"unwind-{len(self.market_sells)}"}

    def get_positions(self):
        return {"market_positions": [{"ticker": t, "position": p} for t, p in self.positions.items()]}

    def cancel_all(self):
        return 0


class _EchoHedger:
    """A hedger that BUYS WHAT IT IS ASKED FOR and moves the Kalshi position, like the real one."""

    def __init__(self, kalshi, poly, *, ticker=_TICKER, price=_HEDGE_PX):
        self.kalshi, self.poly, self.ticker, self.price = kalshi, poly, ticker, price
        self.calls: list = []
        self.call_ts: list = []

    def hedge(self, fill, spec):
        self.calls.append({"fill": fill, "spec": spec})
        self.call_ts.append(time.perf_counter())
        n = float(fill["size"])
        self.kalshi.positions[self.ticker] = self.kalshi.positions.get(self.ticker, 0.0) + n
        fee = round(0.07 * self.price * (1.0 - self.price) * n, 4)
        self.result_pnl = round((1 - _REST_PX - self.price) * n - fee, 4)
        return SimpleNamespace(status="locked", hedged_shares=n, hedge_avg_price=self.price,
                               hedge_fee=fee, locked_pnl=self.result_pnl, unwind_cost=None,
                               detail={"kalshi": {"order_id": f"kh{len(self.calls)}"}})


def _shaegr(tmp_path, **kw):
    """An armed executor resting on POLY with its hedge on KALSHI, sized for the real 109/116 fills."""
    poly = _Poly()
    kalshi = _Kalshi()
    hedger = _EchoHedger(kalshi, poly)
    ex, _cfg = _exec_kalshi(tmp_path, kalshi_oc=_KalshiOC(), kalshi=kalshi, poly=poly, hedger=hedger,
                            state=_State())
    ex.in_flight = _Guard()
    ex.log = _Log()
    ex.telegram = kw.setdefault("sent", []).append
    ex.digest_min = 0.0
    ex.caps.quote_usd_max = 200.0            # 200/0.74 -> 270 shares; the fills below are 109 and 116
    ex.caps.max_pair_stake_usd = 1000.0
    ex.caps.max_daily_stake_usd = 10000.0
    ex.caps.max_fills_per_day = 20
    ex.roll_day(_DT)
    return ex, poly, kalshi, hedger, kw["sent"]


def _shaegr_cand():
    """The real market, so the alerts under test render the real human name."""
    return SimpleNamespace(
        key=("soccer", "SHAEGR", "total_goals|3.5", "Under", "rest-poly"),
        sport="soccer", game="SHAEGR", market_key="total_goals|3.5", rest_side="Under", side="Under",
        direction="rest-poly", rest_ref=("polymarket", _UNDER, "BUY"), rest_venue="polymarket",
        hedge_venue="kalshi", poly_rate=0.05, rest_id=_UNDER, hedge_id=_TICKER,
        teams="Shamrock Rovers vs KF Egnatia Rrogozhine",
        hedge_lookup={"venue": "kalshi", "ticker": _TICKER, "side": "yes"})


def _place(ex, size, now_ts):
    """Rest an order sized to EXACTLY ``size`` shares, so the fill below completes it — the incident was
    two SEPARATE orders (the 109 filled and closed; a new one was placed at 18:40:07Z), and a single
    part-filled order would make the second signal a 7-share delta instead of a fresh 116."""
    ex.caps.quote_usd_max = round(size * _REST_PX, 6)     # quote_usd_max / price -> exactly `size`
    ex.place_or_reprice(_shaegr_cand(), _dec(_REST_PX, hedge_ask=_HEDGE_PX), None, _BOOKS, _DT,
                        now_ts, "pre")
    rest = ex.order_client.rests[-1]
    assert rest["size"] == size, f"fixture placed {rest['size']} shares, wanted {size}"
    return rest["oid"]


def _rest_fill(ex, poly, size, *, now_ts, deliver=None):
    """Place a rest-poly order on the SHAEGR Under token and report ``size`` matched on it.

    ``deliver`` is what the VENUE actually puts in the wallet (None = leave the balance untouched, which
    is the phantom). The two are deliberately separate arguments — that gap IS the bug."""
    oid = _place(ex, size, now_ts)
    if deliver is not None:
        poly.position = float(deliver)
    ex.on_order_update({"order_id": oid, "size_matched": size, "price": _REST_PX},
                       _BOOKS, _DT, now_ts + 1.0)
    return oid


def _settle_pending(ex, now_ts):
    """Run the confirmation cadence to completion (submit -> worker -> drain -> decide)."""
    for _ in range(40):
        if not ex._pending_pairs:
            break
        ex.submit_rest_confirmations(_DT, now_ts)
        for _ in range(20):                       # let the single off-loop thread answer
            if ex._worker.drain.__self__._results:
                break
            time.sleep(0.02)
        ex.drain_offloop(_BOOKS, _DT, now_ts)
        now_ts += 5.0
    return now_ts


def _events(ex):
    return [r["event"] for r in ex.state.rows]


# --------------------------------------------------------------------------- #
# 1. THE REPLAY: the real 109, then the phantom 116                            #
# --------------------------------------------------------------------------- #
def test_shaegr_replay_phantom_fill_is_not_booked_and_its_hedge_is_unwound(tmp_path):
    ex, poly, kalshi, hedger, sent = _shaegr(tmp_path)

    # 18:36:33Z — the REAL fill. The venue delivers 109 Under, and this books exactly as it always did.
    _rest_fill(ex, poly, 109, now_ts=100.0, deliver=109)
    assert _events(ex).count("hedge_locked") == 1, "the genuine pair books"
    assert ex._expected_shares("polymarket", _UNDER) == 109.0
    assert ex._expected_shares("kalshi", _TICKER) == 109.0
    assert any("GUARANTEED" in m for m in sent), "a real pair still announces the lock"

    # 18:40:57Z — the PHANTOM. size_matched says 116; the wallet stays at 109 forever.
    sent.clear()
    _rest_fill(ex, poly, 116, now_ts=300.0, deliver=None)
    assert len(hedger.calls) == 2, "the hedge STILL fires instantly on the signal — speed is the edge"
    assert kalshi.positions[_TICKER] == 225.0, "116 real contracts were bought"
    assert _events(ex).count("hedge_locked") == 1, "NO second pair is booked on an unconfirmed rest leg"
    assert not any("GUARANTEED" in m for m in sent), "nothing may be called guaranteed yet"
    assert ex._pending_pairs, "the pair is PROVISIONAL while the venue is asked"

    _settle_pending(ex, 400.0)

    # The verdict: phantom. The unpaired hedge is sold back, DOWN TO the 109 held by the genuine pair.
    assert not ex._pending_pairs, "the pending pair is resolved, not left hanging"
    assert kalshi.market_sells and sum(s["count"] for s in kalshi.market_sells) == 116
    assert kalshi.positions[_TICKER] == 109.0, "the earlier genuine pair's hedge is NOT liquidated"
    assert "phantom_unwound" in _events(ex)
    assert _events(ex).count("hedge_locked") == 1, "still exactly one pair, ever"
    assert ex._expected_shares("polymarket", _UNDER) == 109.0, "no phantom rest leg was registered"
    assert ex._expected_shares("kalshi", _TICKER) == 109.0
    assert ex.orphan is None and ex.caps.halted is False


def test_the_phantom_alert_is_red_names_the_market_and_never_says_guaranteed(tmp_path):
    ex, poly, _kalshi, _hedger, sent = _shaegr(tmp_path)
    _rest_fill(ex, poly, 109, now_ts=100.0, deliver=109)
    sent.clear()
    _rest_fill(ex, poly, 116, now_ts=300.0, deliver=None)
    _settle_pending(ex, 400.0)

    red = [m for m in sent if "FILL NEVER ARRIVED" in m]
    assert red, "a phantom fill must produce an instant RED alert"
    msg = red[0]
    assert "🔴" in msg
    assert "Shamrock" in msg or "Egnatia" in msg, "the alert names the market in plain words"
    assert "GUARANTEED" not in msg and "locked in" in msg
    assert "0x" not in msg and _TICKER not in msg, "no tickers or ids reach Telegram"


def test_the_exit_toll_of_a_phantom_reaches_lifetime_and_the_exits_bucket(tmp_path):
    """The unwind cost is the ENTIRE financial record of a phantom fill — there is no pair and there will
    be no settlement, so if it does not enter lifetime here it enters nowhere."""
    from src.genz.maker_rt.state import MakerState

    st = MakerState()
    before_life, before_exits = st.settled_pnl_lifetime, st.settled_pnl_exits_lifetime
    st.record({"event": "phantom_unwound", "mode": "live", "phase": "pre", "game": "g",
               "market_key": "m", "unwind_cost": 1.75}, _DT)
    assert st.settled_pnl_lifetime == round(before_life - 1.75, 4)
    assert st.settled_pnl_exits_lifetime == round(before_exits - 1.75, 4)
    assert st.settled_exits == 1


# --------------------------------------------------------------------------- #
# 2. PARTIAL DELIVERY: 116 claimed, 60 delivered                                #
# --------------------------------------------------------------------------- #
def test_partial_delivery_books_the_delivered_pair_and_unwinds_only_the_excess(tmp_path):
    ex, poly, kalshi, hedger, sent = _shaegr(tmp_path)
    _rest_fill(ex, poly, 116, now_ts=100.0, deliver=60)      # claimed 116, the wallet receives 60

    assert len(hedger.calls) == 1 and kalshi.positions[_TICKER] == 116.0
    assert ex._pending_pairs, "60 < 116 -> not confirmed, so nothing is booked yet"
    _settle_pending(ex, 200.0)

    assert _events(ex).count("hedge_locked") == 1, "the part that DID arrive is a genuine, smaller pair"
    row = [r for r in ex.state.rows if r["event"] == "hedge_locked"][0]
    assert row["size"] == 60, "the pair is booked at the DELIVERED size, not the claimed one"
    assert sum(s["count"] for s in kalshi.market_sells) == 56, "only the unpaired hedge is sold"
    assert kalshi.positions[_TICKER] == 60.0
    assert ex._expected_shares("polymarket", _UNDER) == 60.0
    assert ex._expected_shares("kalshi", _TICKER) == 60.0
    assert ex.caps.halted is False and ex.orphan is None


def test_a_partial_delivery_pnl_is_scaled_to_the_shares_that_actually_arrived(tmp_path):
    """Booking the full-size pnl on a part-delivered pair would pay us edge on shares we never received —
    which is the SHAEGR error in miniature (there the books took +$1.61 on a pair worth +$0.79)."""
    ex, poly, _kalshi, hedger, _sent = _shaegr(tmp_path)
    _rest_fill(ex, poly, 116, now_ts=100.0, deliver=60)
    claimed_pnl = float(hedger.result_pnl)                 # what 116 shares of this edge would have paid
    _settle_pending(ex, 200.0)

    row = [r for r in ex.state.rows if r["event"] == "hedge_locked"][0]
    booked = float(row["locked_pnl"])
    assert booked == pytest.approx(claimed_pnl * 60.0 / 116.0, rel=0.02), "pnl scales to what arrived"
    assert booked < claimed_pnl
    assert ex.caps.pnl_today == pytest.approx(booked - _unwind_cost(ex), abs=1e-4)


def _unwind_cost(ex):
    return sum(float(r.get("unwind_cost") or 0.0) for r in ex.state.rows
               if r["event"] == "phantom_unwound")


# --------------------------------------------------------------------------- #
# 3. SLOW CONFIRM: a REAL fill that lands late must NEVER be unwound            #
# --------------------------------------------------------------------------- #
def test_a_slow_but_real_delivery_is_booked_not_punished(tmp_path):
    """The dangerous error is one-sided. Waiting costs a late Telegram; a false phantom verdict would
    unwind a good hedge AND strand a real rest leg."""
    ex, poly, kalshi, hedger, sent = _shaegr(tmp_path)
    _rest_fill(ex, poly, 109, now_ts=100.0, deliver=None)    # nothing in the wallet yet
    assert ex._pending_pairs and not any("GUARANTEED" in m for m in sent)

    # Several confirmation rounds pass with the wallet still empty — and nothing is unwound.
    now_ts = 101.0
    for _ in range(4):
        ex.submit_rest_confirmations(_DT, now_ts)
        time.sleep(0.05)
        ex.drain_offloop(_BOOKS, _DT, now_ts)
        now_ts += 2.0
    assert ex._pending_pairs, "still inside the deadline -> still waiting"
    assert kalshi.market_sells == [], "a hedge is NEVER unwound while the deadline has not passed"

    poly.position = 109.0                                    # the trade finally MINES, late
    _settle_pending(ex, now_ts)

    assert _events(ex).count("hedge_locked") == 1, "a slow-but-real fill books normally"
    assert kalshi.market_sells == [], "and nothing was sold"
    assert any("GUARANTEED" in m for m in sent), "the lock is announced, just later"
    assert ex._expected_shares("polymarket", _UNDER) == 109.0
    assert ex._expected_shares("kalshi", _TICKER) == 109.0


def test_delivery_landing_on_the_last_read_before_the_deadline_still_books(tmp_path):
    ex, poly, kalshi, _hedger, _sent = _shaegr(tmp_path)
    _rest_fill(ex, poly, 109, now_ts=100.0, deliver=None)
    rec = list(ex._pending_pairs.values())[0]
    edge = float(rec["deadline_ts"]) - 0.01                  # the very edge of the deadline
    poly.position = 109.0
    ex.submit_rest_confirmations(_DT, edge)
    time.sleep(0.05)
    ex.drain_offloop(_BOOKS, _DT, edge)
    assert not ex._pending_pairs and "hedge_locked" in _events(ex)
    assert kalshi.market_sells == []


def test_an_unreadable_venue_is_unknown_and_never_a_phantom_verdict(tmp_path):
    """A read that RAISES means we do not know. Treating that as 'nothing arrived' would unwind good
    hedges every time the venue had a bad minute."""
    ex, poly, kalshi, _hedger, _sent = _shaegr(tmp_path)

    def _boom(_token):
        raise RuntimeError("balance endpoint down")

    _rest_fill(ex, poly, 109, now_ts=100.0, deliver=None)
    poly.conditional_balance = _boom
    now_ts = 101.0
    for _ in range(3):
        ex.submit_rest_confirmations(_DT, now_ts)
        time.sleep(0.05)
        ex.drain_offloop(_BOOKS, _DT, now_ts)
        now_ts += 2.0
    assert ex._pending_pairs, "unreadable is UNKNOWN — keep waiting, do not unwind"
    assert kalshi.market_sells == []


# --------------------------------------------------------------------------- #
# 4. MATCHED-then-FAILED, on the exact payload shape the fill poll read         #
# --------------------------------------------------------------------------- #
#: The REST FILL POLL is what fired on 18:40:57Z: ``GET /data/orders`` did not carry a readable matched
#: count for the order, so ``_fill_poll_job`` fell through to ``GET /data/order/{id}`` — and THAT payload
#: is what said 116. This is its shape (Polymarket sends every number as a string).
_MATCHED_THEN_FAILED = {
    "id": "0x7b53749773fa49a61b349c110aebac9e5001f0f6a3675040fcb2f020928714cf",
    "status": "MATCHED", "owner": "maker", "side": "BUY", "outcome": "Under",
    "asset_id": _UNDER, "market": "0x2fb5ae0d",
    "original_size": "116", "size_matched": "116", "price": "0.74",
}


def test_the_fill_poll_payload_still_routes_a_hedge_but_books_nothing(tmp_path):
    """Drive the REAL detector with the REAL payload: the poll must still hedge (a fill we ignore is a
    naked position) and must not book a pair off ``size_matched`` alone."""
    ex, poly, kalshi, hedger, sent = _shaegr(tmp_path)
    oid = _place(ex, 116, 100.0)

    # the venue answers the per-order read with the MATCHED-but-never-mined payload
    poly.get_order = lambda _oid: dict(_MATCHED_THEN_FAILED, id=_oid)
    ex.poll_open_orders(_BOOKS, _DT, 101.0)

    assert len(hedger.calls) == 1, "the poll still hedges the reported fill, instantly"
    assert float(hedger.calls[0]["fill"]["size"]) == 116.0
    assert "hedge_locked" not in _events(ex), "size_matched alone must not book a pair"
    assert "pair_pending" in _events(ex)
    assert not any("GUARANTEED" in m for m in sent)

    _settle_pending(ex, 200.0)
    assert "phantom_unwound" in _events(ex)
    assert sum(s["count"] for s in kalshi.market_sells) == 116


def test_size_matched_is_still_read_as_a_number_from_the_string_payload(tmp_path):
    """Guard the parse itself: the payload's numbers are STRINGS, and a regression to reading them raw is
    the 2026-07-30 TypeError that killed the live process."""
    from src.genz.maker_rt.pregame_exec import PregameLiveExecutor

    lo = SimpleNamespace(rest_venue="polymarket", size=116.0)
    assert PregameLiveExecutor._order_matched(lo, _MATCHED_THEN_FAILED) == 116.0


# --------------------------------------------------------------------------- #
# 5. IDEMPOTENCE across the detectors + the dedupe                              #
# --------------------------------------------------------------------------- #
def test_all_detectors_seeing_the_same_phantom_produce_ONE_hedge_and_ONE_unwind(tmp_path):
    """The socket, the REST poll and a re-poll all report the SAME cumulative ``size_matched``. Exactly
    one hedge may be bought and exactly one pending pair created."""
    ex, poly, kalshi, hedger, _sent = _shaegr(tmp_path)
    oid = _place(ex, 116, 100.0)
    poly.get_order = lambda _oid: dict(_MATCHED_THEN_FAILED, id=_oid)

    ex.on_order_update({"order_id": oid, "size_matched": 116, "price": _REST_PX}, _BOOKS, _DT, 101.0)
    ex.poll_open_orders(_BOOKS, _DT, 102.0)                  # the REST poll sees the same cumulative
    ex.on_order_update({"order_id": oid, "size_matched": 116, "price": _REST_PX}, _BOOKS, _DT, 103.0)

    assert len(hedger.calls) == 1, "one execution, one hedge — never one per detector"
    assert len(ex._pending_pairs) == 1
    assert kalshi.positions[_TICKER] == 116.0

    _settle_pending(ex, 200.0)
    assert sum(s["count"] for s in kalshi.market_sells) == 116, "and exactly one unwind"
    assert _events(ex).count("phantom_unwound") == 1


def test_a_phantom_fill_consumes_exactly_one_fill_slot_and_its_stake_up_front(tmp_path):
    """Caps see the money the moment it is spent — the hedge is real however the rest leg resolves — and
    the resolution must NOT count a second fill (the adjust_pnl doctrine)."""
    ex, poly, _kalshi, _hedger, _sent = _shaegr(tmp_path)
    _rest_fill(ex, poly, 116, now_ts=100.0, deliver=None)

    assert ex.caps.fills_today == 1, "the fill slot is spent up front, fail-closed"
    hedge_notional = 116 * _HEDGE_PX
    assert ex.caps.stake_today >= hedge_notional, "the hedge notional is committed immediately"
    staked = ex.caps.stake_today

    _settle_pending(ex, 200.0)
    assert ex.caps.fills_today == 1, "resolving a pending pair must never count a second fill"
    assert ex.caps.stake_today >= staked, "the unwind sell is committed stake too"
    assert ex.caps.pnl_today < 0, "the exit toll moves the day's pnl"


def test_a_pending_pair_counts_as_held_so_its_hedge_is_not_a_false_orphan(tmp_path):
    """While we wait, the hedge is REAL SHARES. The account sweep and the reconcile must see them as
    ours — the HANHAL false-halt shape, which the pending window would otherwise re-open."""
    ex, poly, kalshi, _hedger, _sent = _shaegr(tmp_path)
    _rest_fill(ex, poly, 116, now_ts=100.0, deliver=None)

    assert ex._pending_pairs
    assert ex._expected_shares("kalshi", _TICKER) == 116.0, "the bought hedge counts as held"
    ex._traded_tickers.add(_TICKER)
    assert ex.reconcile_positions(_DT) is None, "an unconfirmed pair's hedge is not an orphan"
    assert ex.caps.halted is False


def test_a_pending_pair_is_decided_even_if_no_confirmation_read_ever_lands(tmp_path):
    """Backstop: a starved worker must not leave real money hedged against nothing with nobody looking."""
    ex, poly, kalshi, _hedger, _sent = _shaegr(tmp_path)
    _rest_fill(ex, poly, 116, now_ts=100.0, deliver=None)
    rec = list(ex._pending_pairs.values())[0]
    past = float(rec["deadline_ts"]) + ex.PENDING_DECIDE_GRACE_S + 1.0

    ex.submit_rest_confirmations(_DT, past)                  # decides without waiting for a read

    assert not ex._pending_pairs
    assert "phantom_unwound" in _events(ex)
    assert sum(s["count"] for s in kalshi.market_sells) == 116


# --------------------------------------------------------------------------- #
# 6. LATENCY GUARD: nothing may be added in front of the hedge                  #
# --------------------------------------------------------------------------- #
def test_no_venue_read_happens_before_the_hedge_is_submitted(tmp_path):
    """THE performance invariant. Confirmation reads the rest leg — but never before the hedge order has
    gone. Any read that lands in front of the hedge is latency added to the one path that is a race."""
    ex, poly, kalshi, hedger, _sent = _shaegr(tmp_path)
    order: list = []
    real_balance = _Poly.conditional_balance

    def _traced(self_, token):
        order.append("read")
        return getattr(self_, "position", 0.0)

    poly.conditional_balance = lambda token: _traced(poly, token)
    hedged_at = []
    inner = hedger.hedge

    def _hedge(fill, spec):
        order.append("hedge")
        hedged_at.append(len(order))
        return inner(fill, spec)

    hedger.hedge = _hedge
    _rest_fill(ex, poly, 109, now_ts=100.0, deliver=None)

    assert "hedge" in order, "the hedge fired"
    assert order.index("hedge") == 0, f"a venue read ran BEFORE the hedge: {order}"
    assert real_balance is _Poly.conditional_balance      # (we only patched the instance)


def test_fill_to_hedge_submit_does_not_regress(tmp_path):
    """Measured, not asserted by eye: the fill -> hedge-submit path must stay in the same regime it was
    in before rest-leg confirmation existed. Confirmation happens AFTER the hedge, so the only work
    added in front of it is none at all."""
    ex, poly, _kalshi, hedger, _sent = _shaegr(tmp_path)
    oid = _place(ex, 116, 100.0)

    t0 = time.perf_counter()
    ex.on_order_update({"order_id": oid, "size_matched": 109, "price": _REST_PX}, _BOOKS, _DT, 101.0)
    submitted_at = hedger.call_ts[0]

    assert (submitted_at - t0) < 0.05, "fill -> hedge submit must stay in the sub-50ms regime"
