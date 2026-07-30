"""Regression suite for the 2026-07-25 STACKED GHOST ORDERS incident (KXUFCFIGHT-26JUL25ZAYRZE).

Root cause: ``KalshiExec.cancel_order`` sent the DELETE to ``/portfolio/events/orders/{id}`` (the
``events/`` segment belongs ONLY to the batch CREATE path), so EVERY Kalshi cancel returned
``404 not_found``. The caller trusted that as "already gone", the slot was freed, and a replacement
stacked on top of the still-live order — five identical $19.60 bids all filled ($98, 5x the per-quote
cap) and the bot halted on the orphan.

The fix is layered and these tests pin each layer:
  1. the cancel path is ``/portfolio/orders/{id}`` (test_executor.test_kalshi_cancel_v2_endpoint);
  2. CANCEL VERIFY-OR-SCREAM — after any cancel, VENUE TRUTH decides; a still-resting or unreadable
     order is NEVER released (the slot stays held, a replacement is never placed);
  3. the stale-slot release re-resolves against the resting list before freeing a slot (a blind
     single-order read must not drop a still-live order);
  4. PRE-PLACEMENT STACK GUARD — a new order never rests on a market that already carries an untracked
     order of ours; and
  5. all of this holds at 8 concurrent opens.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.executor.kalshi_exec import KalshiExecError
from src.genz.maker_rt.caps import LiveCaps, direction_slot_ok
from src.genz.maker_rt import config as mrt_config
from src.genz.maker_rt.orders import KalshiOrderClient

from .test_maker_rt_pregame import _Log, _Store, _cand_kalshi, _dec, _exec_kalshi

NOW = datetime(2026, 7, 25, 16, 40, 0, tzinfo=timezone.utc)


class _KVenue:
    """A Kalshi account that models the EXACT failure the ghost incident hit: the resting book is
    authoritative (``list_resting``), but ``cancel_order`` can 404 (leaving the order resting) and the
    single-order / generic-list reads can come back BLIND while the order is still resting. This is what
    lets us prove the re-resolve-against-the-resting-list fix."""

    def __init__(self, *, cancel_404: bool = False, single_read_blind: bool = False,
                 orders_scan_blind: bool = False, list_raises: bool = False) -> None:
        self.resting: dict = {}                 # order_id -> order dict (the venue's TRUTH)
        self._n = 0
        self.cancel_404 = cancel_404
        self.single_read_blind = single_read_blind
        self.orders_scan_blind = orders_scan_blind
        self.list_raises = list_raises
        self.cancels: list = []
        self.places: list = []
        self.positions: dict = {}

    # -- placement / cancel --------------------------------------------------
    def place_order(self, ticker, side, count, price, *, action="buy", time_in_force=None,
                    post_only=False, client_order_id=None):
        self._n += 1
        oid = f"kx-{self._n}"
        self.places.append({"ticker": ticker, "oid": oid, "coid": client_order_id})
        self.resting[oid] = {"order_id": oid, "client_order_id": client_order_id, "ticker": ticker,
                             "side": side, "status": "resting", "count": count, "remaining_count": count,
                             "fill_count": 0}
        return {"status": "resting", "fill_count": 0, "avg_price": price, "order_id": oid}

    def cancel_order(self, oid):
        self.cancels.append(oid)
        if self.cancel_404:                     # the incident: DELETE 404s, order STAYS resting
            raise KalshiExecError(
                '404 on DELETE /portfolio/orders/%s: {"error":{"code":"not_found"}}' % oid)
        self.resting.pop(oid, None)
        return {"order": {"order_id": oid, "status": "canceled"}, "reduced_by": 1}

    # -- reads (deliberately UNRELIABLE per the flags) -----------------------
    def get_order(self, oid):
        if self.single_read_blind:
            return {}                           # single-order GET blind (models the 404 -> {} fallback)
        return dict(self.resting.get(oid, {}))

    def get_orders(self, *, status=None, ticker=None):
        if self.orders_scan_blind:
            return {"orders": []}               # the generic scan misses the resting order (the hole)
        rows = [o for o in self.resting.values() if ticker is None or o["ticker"] == ticker]
        return {"orders": rows}

    def list_resting(self, *, ticker=None, limit=200, max_pages=5):
        if self.list_raises:                    # the venue is unreadable -> caller must FAIL CLOSED
            raise KalshiExecError("resting-list read failed")
        return [dict(o) for o in self.resting.values() if ticker is None or o["ticker"] == ticker]

    # -- positions / unwind (for _kalshi_position + verify paths) ------------
    def get_positions(self):
        return {"market_positions": [{"ticker": t, "position": p} for t, p in self.positions.items()]}

    def place_market_sell(self, ticker, side, count, client_order_id=None):
        self.positions[ticker] = 0.0
        return {"status": "filled", "fill_count": count, "avg_price": 0.5}


def _kalshi_exec(tmp_path, venue, *, caps=None):
    koc = KalshiOrderClient(venue)
    ex, cfg = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=venue)
    if caps is not None:
        ex.caps = caps
    ex.roll_day(NOW)
    return ex, koc


def _ck(ticker, key_suffix, side="yes"):
    c = _cand_kalshi(ticker=ticker, side=side)
    c.key = ("mlb", f"G{key_suffix}", "ml2", "Home", "rest-kalshi")
    # ``.game`` has to AGREE with the key's game component, or these read as eight quotes on one match
    # to the per-game concentration cap (N15) — which is exactly what that cap exists to refuse.
    c.game = f"G{key_suffix}"
    return c


_STORE = _Store(kalshi_ask=0.60, poly_best_ask=0.55)
_DEC = lambda: _dec(0.46, hedge_ask=0.55)   # noqa: E731


# --------------------------------------------------------------------------- #
# 2. CANCEL VERIFY-OR-SCREAM: a 404'd cancel never frees the slot               #
# --------------------------------------------------------------------------- #
def test_cancel_404_keeps_tracked_never_releases(tmp_path):
    """A DELETE that 404s while the order is STILL RESTING must return unconfirmed — the order stays
    tracked (slot held), so a replacement can never stack on top of it."""
    venue = _KVenue(cancel_404=True, single_read_blind=True, orders_scan_blind=True)
    ex, _ = _kalshi_exec(tmp_path, venue)
    c = _ck("KX-1", 1)
    ex.place_or_reprice(c, _DEC(), None, _STORE, NOW, 100.0, "pre")
    assert c.key in ex.open_orders and len(venue.resting) == 1
    # The cancel 404s but the order is still resting -> NOT confirmed -> keep tracked (do NOT free slot).
    assert ex._cancel(c.key, NOW, "reprice") is False
    assert c.key in ex.open_orders and ex.caps.open_quotes == 1
    assert len(venue.resting) == 1, "the order is still live on the venue"


def test_genuine_cancel_confirms_and_releases(tmp_path):
    """Once the cancel path works, a real cancel is venue-confirmed (gone from the resting list) and the
    slot is released — even when the single-order read is blind."""
    venue = _KVenue(single_read_blind=True, orders_scan_blind=True)     # cancels succeed
    ex, _ = _kalshi_exec(tmp_path, venue)
    c = _ck("KX-1", 1)
    ex.place_or_reprice(c, _DEC(), None, _STORE, NOW, 100.0, "pre")
    assert ex._cancel(c.key, NOW, "reprice") is True
    assert c.key not in ex.open_orders and ex.caps.open_quotes == 0 and not venue.resting


# --------------------------------------------------------------------------- #
# 3. stale release re-resolves against the resting list                         #
# --------------------------------------------------------------------------- #
def test_poll_does_not_release_blind_but_still_resting(tmp_path):
    """poll_open_orders sees an EMPTY single-order read past the grace — but the order is still in the
    resting list, so its slot must NOT be freed (the exact drop that let the replacement stack)."""
    venue = _KVenue(single_read_blind=True, orders_scan_blind=True)     # blind reads, order IS resting
    ex, _ = _kalshi_exec(tmp_path, venue)
    c = _ck("KX-1", 1)
    ex.place_or_reprice(c, _DEC(), None, _STORE, NOW, 100.0, "pre")
    ex.poll_open_orders(_Store(), NOW, 100.0 + ex._stale_grace_s + 1)
    assert c.key in ex.open_orders, "a blind single-read must NOT drop a still-resting order"
    assert ex.caps.open_quotes == 1 and ex._slot_released == 0


def test_poll_releases_only_when_resting_list_confirms_gone(tmp_path):
    """When the order is truly gone (absent from the resting list) the blind-read path DOES release."""
    venue = _KVenue(single_read_blind=True, orders_scan_blind=True)
    ex, _ = _kalshi_exec(tmp_path, venue)
    c = _ck("KX-1", 1)
    ex.place_or_reprice(c, _DEC(), None, _STORE, NOW, 100.0, "pre")
    venue.resting.clear()                                   # the venue really has nothing resting now
    ex.poll_open_orders(_Store(), NOW, 100.0 + ex._stale_grace_s + 1)
    assert c.key not in ex.open_orders and ex._slot_released == 1


def test_poll_unreadable_venue_keeps_tracked_fail_closed(tmp_path):
    """If neither the single read NOR the resting list can be read, state is UNKNOWN -> fail closed
    (keep the order tracked, never free a slot on an unreadable venue)."""
    venue = _KVenue(single_read_blind=True, orders_scan_blind=True)
    ex, _ = _kalshi_exec(tmp_path, venue)
    c = _ck("KX-1", 1)
    ex.place_or_reprice(c, _DEC(), None, _STORE, NOW, 100.0, "pre")
    venue.list_raises = True                                # NOW the venue's resting list is unreadable
    ex.poll_open_orders(_Store(), NOW, 100.0 + ex._stale_grace_s + 1)
    assert c.key in ex.open_orders and ex._slot_released == 0


# --------------------------------------------------------------------------- #
# THE incident replay: place -> DELETE 404 -> still resting -> NO new placement #
# --------------------------------------------------------------------------- #
def _settle_offloop(ex, tries: int = 200) -> None:
    """Wait for the off-loop cancel worker to go idle (phase 2 / N18). The retry DELETE is issued on a
    worker thread and DECIDED on the loop, so a test that asserts on the venue has to let the thread run
    and then drain — which is also the honest shape of what the event loop does every tick."""
    import time as _t
    for _ in range(tries):
        if ex._worker.pending() == 0:
            break
        _t.sleep(0.01)
    ex._drain_cancel_results(_STORE, NOW, 0.0)


def test_incident_replay_no_stacking_then_recovers(tmp_path):
    """Replay the exact pattern: a mandatory reprice tries to cancel, the DELETE 404s, the order is still
    resting -> NO replacement is placed. Once the cancel path works the reprice confirms death and places
    cleanly — exactly ONE order rests throughout (never a stack).

    The RETRY CADENCE changed in phase 2 (N18): the storm this replay used to require — one synchronous
    DELETE+GET per tick, up to 4,909/hour — is exactly the thing that got fixed, so what is pinned here
    is the INVARIANT (never stack, never free the slot, never abandon the cancel) rather than the number
    of DELETEs. A tick inside the backoff window must issue NONE."""
    venue = _KVenue(cancel_404=True, single_read_blind=True, orders_scan_blind=True)
    ex, _ = _kalshi_exec(tmp_path, venue)
    c = _ck("KX-1", 1)
    ex.place_or_reprice(c, _DEC(), None, _STORE, NOW, 100.0, "pre")
    assert len(venue.resting) == 1
    d2 = _dec(0.44, hedge_ask=0.55); d2.floor = 0.44        # resting 0.46 is now below floor -> MANDATORY reprice
    for i in range(20):                                     # 20 driver passes = the whole 5s floor
        ex.place_or_reprice(c, d2, None, _STORE, NOW, 140.0 + i * 0.25, "pre")
    assert len(venue.resting) == 1, "NEVER stacked a replacement while the original was still live"
    assert len(ex.open_orders) == 1 and ex.caps.open_quotes == 1
    assert len(venue.cancels) == 1, "20 ticks inside the 5s backoff issue ONE DELETE, not 20"
    ex.place_or_reprice(c, d2, None, _STORE, NOW, 200.0, "pre")   # past the floor -> retry, off-loop
    _settle_offloop(ex)
    assert len(venue.cancels) >= 2, "the cancel was retried, not abandoned"
    assert len(ex.open_orders) == 1 and ex.caps.open_quotes == 1, "an unhonoured cancel keeps the slot"
    assert len(venue.resting) == 1, "still exactly one order live at the venue"
    # cancel path recovers -> the off-loop retry confirms death; the next tick replaces cleanly.
    venue.cancel_404 = False
    ex.place_or_reprice(c, d2, None, _STORE, NOW, 400.0, "pre")
    _settle_offloop(ex)
    assert c.key not in ex.open_orders, "the venue-confirmed cancel freed the slot on the loop"
    ex.place_or_reprice(c, d2, None, _STORE, NOW, 400.5, "pre")
    assert len(venue.resting) == 1 and ex.open_orders[c.key].price == 0.44


# --------------------------------------------------------------------------- #
# 4. PRE-PLACEMENT STACK GUARD                                                   #
# --------------------------------------------------------------------------- #
def _ghost(ticker, oid="ghost-1", coid="mrt-99-1", count=28):
    return {"order_id": oid, "client_order_id": coid, "ticker": ticker, "side": "yes",
            "status": "resting", "count": count, "remaining_count": count, "fill_count": 0}


def test_stack_guard_clears_ghost_then_places(tmp_path):
    """A NEW placement onto a market that already carries an UNTRACKED order of ours cancel-verifies the
    ghost away first, then places — exactly one (ours) rests, no stack."""
    venue = _KVenue()                                       # cancels work
    ex, _ = _kalshi_exec(tmp_path, venue)
    venue.resting["ghost-1"] = _ghost("KX-1")               # a prior failed cancel left this live, untracked
    c = _ck("KX-1", 1)
    ex.place_or_reprice(c, _DEC(), None, _STORE, NOW, 100.0, "pre")
    assert "ghost-1" not in venue.resting, "the ghost was cancel-verified away"
    assert c.key in ex.open_orders and len(venue.resting) == 1


def test_stack_guard_refuses_when_ghost_survives(tmp_path):
    """If the ghost cannot be cancelled (its DELETE 404s and it stays resting), the new order is REFUSED
    — never stack a second order on a market with a live untracked order."""
    venue = _KVenue(cancel_404=True, single_read_blind=True, orders_scan_blind=True)
    ex, _ = _kalshi_exec(tmp_path, venue)
    venue.resting["ghost-1"] = _ghost("KX-1")
    c = _ck("KX-1", 1)
    ex.place_or_reprice(c, _DEC(), None, _STORE, NOW, 100.0, "pre")
    assert c.key not in ex.open_orders, "must NOT place while a ghost is still resting"
    assert list(venue.resting) == ["ghost-1"], "no replacement stacked; only the uncleared ghost remains"
    assert any(r.get("reason") == "stack_guard_untracked_resting" for r in ex.state.rows)


def test_stack_guard_ignores_our_own_tracked_order_same_ticker(tmp_path):
    """A tracked order of ours already resting on a ticker is NOT a ghost — a second placement on the
    same ticker (different node) must pass the stack guard, not try to cancel our own live order."""
    venue = _KVenue()
    caps = LiveCaps(mrt_config.LiveConfig(max_open_quotes=8, max_daily_stake_usd=1000, max_fills_per_day=20))
    ex, _ = _kalshi_exec(tmp_path, venue, caps=caps)
    c1 = _ck("KX-1", 1)
    ex.place_or_reprice(c1, _DEC(), None, _STORE, NOW, 100.0, "pre")
    c2 = _ck("KX-1", 2)                                     # same ticker, different node/key
    ex.place_or_reprice(c2, _DEC(), None, _STORE, NOW, 101.0, "pre")
    assert c1.key in ex.open_orders and c2.key in ex.open_orders
    assert venue.cancels == [], "our own tracked order was never mistaken for a ghost"
    assert len(venue.resting) == 2


# --------------------------------------------------------------------------- #
# 5. 8 CONCURRENT opens: bookkeeping, verified cancels, no stacking             #
# --------------------------------------------------------------------------- #
def test_eight_concurrent_opens_no_stacking(tmp_path):
    """8 resting orders across 6 markets: tracked count == venue resting count throughout; the 9th is
    refused by max_open; verified cancels free slots; re-placing never stacks."""
    venue = _KVenue()
    caps = LiveCaps(mrt_config.LiveConfig(max_open_quotes=8, max_daily_stake_usd=1000, max_fills_per_day=20))
    ex, _ = _kalshi_exec(tmp_path, venue, caps=caps)
    ex.directions = {"rest-kalshi"}                        # single direction -> reserve is a no-op here
    # 8 orders across 6 tickers: KX-1 and KX-2 each carry two nodes (proves same-ticker isn't false-flagged).
    plan = [("KX-1", 1), ("KX-1", 2), ("KX-2", 3), ("KX-2", 4),
            ("KX-3", 5), ("KX-4", 6), ("KX-5", 7), ("KX-6", 8)]
    cands = [_ck(tk, sfx) for tk, sfx in plan]
    for i, c in enumerate(cands):
        ex.place_or_reprice(c, _DEC(), None, _STORE, NOW, 100.0 + i, "pre")
    assert ex.open_count() == 8 and ex.caps.open_quotes == 8 and len(venue.resting) == 8
    assert venue.cancels == [], "no cancels — nothing stacked, every order is our own tracked one"
    # 9th refused by max_open_quotes.
    extra = _ck("KX-7", 9)
    ex.place_or_reprice(extra, _DEC(), None, _STORE, NOW, 200.0, "pre")
    assert extra.key not in ex.open_orders and ex.open_count() == 8
    # Cancel two -> venue-confirmed gone -> slots freed.
    assert ex._cancel(cands[0].key, NOW, "reprice") is True
    assert ex._cancel(cands[7].key, NOW, "reprice") is True
    assert ex.open_count() == 6 and len(venue.resting) == 6
    # Re-place two fresh nodes -> back to 8, and the venue count still matches tracked (NO stacking).
    for j, c in enumerate((_ck("KX-8", 10), _ck("KX-9", 11))):
        ex.place_or_reprice(c, _DEC(), None, _STORE, NOW, 300.0 + j, "pre")
    assert ex.open_count() == 8 and len(venue.resting) == 8 == ex.caps.open_quotes


def test_reserve_two_per_direction_at_eight():
    """reserve_per_direction=2 of 8: each enabled direction is guaranteed 2 slots; the other 4 float."""
    # rest-kalshi holding 6, rest-poly viable & holding 0 -> poly's 2 reserved are protected -> kalshi
    # refused a 7th (6 == 8 - 2).
    assert direction_slot_ok("rest-kalshi", {"rest-kalshi": 6}, {"rest-poly", "rest-kalshi"}, 8, 2) is False
    # holding 5 -> allowed (5 < 8 - 2).
    assert direction_slot_ok("rest-kalshi", {"rest-kalshi": 5}, {"rest-poly", "rest-kalshi"}, 8, 2) is True
    # rest-poly already holds 1 of its 2 reserved -> only 1 protected for it -> kalshi may take up to 7.
    assert direction_slot_ok("rest-kalshi", {"rest-kalshi": 6, "rest-poly": 1},
                             {"rest-poly", "rest-kalshi"}, 8, 2) is True


# --------------------------------------------------------------------------- #
# venue-order-state classifier (the shared truth used everywhere above)         #
# --------------------------------------------------------------------------- #
def test_venue_order_state_classifies_all_paths(tmp_path):
    venue = _KVenue()
    ex, _ = _kalshi_exec(tmp_path, venue)
    c = _ck("KX-1", 1)
    ex.place_or_reprice(c, _DEC(), None, _STORE, NOW, 100.0, "pre")
    lo = ex.open_orders[c.key]
    # resting (direct single read finds it)
    assert ex._venue_order_state(lo)[0] == "resting"
    # blind single read + resting list still shows it -> resting
    venue.single_read_blind = True
    assert ex._venue_order_state(lo)[0] == "resting"
    # gone from the resting list -> canceled
    venue.resting.clear()
    assert ex._venue_order_state(lo)[0] == "canceled"
    # unreadable resting list -> unknown (fail closed)
    venue.list_raises = True
    assert ex._venue_order_state(lo)[0] == "unknown"
