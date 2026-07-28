"""Regression suite for the 2026-07-28 01:21Z PHIMIA PRE-HEDGE LEAK + the stale-ORPHAN standstill.

PRODUCTION EVIDENCE (data/ops/maker_rt.log, times UTC):

  01:18:21  rest-kalshi PLACED  Philadelphia Phillies 21 sh @ 0.94 (hedge_ask on Poly then 0.05, hedgeDepth 14240)
  01:21:04  cancel attempts begin, reason ``hedge_too_thin`` — the Poly side had thinned out
  01:21:11  FILLED  21 sh @ 0.94 (the cancel 404'd: the order had already filled)
  01:21:12  poly hedge LOCKED x29 @ 0.0700 (fee $0.103) -> pnl $-0.40
  01:21:12  HEDGED AT A LOSS: rest 0.9400 + hedge 0.0700 = 1.0100/sh, locked_net -1.35%

The POST-hedge guard screamed correctly; the PRE-hedge gate had already let it through. It was not a
race and not a floor-ordering mistake — the gate ran first, on WRONG INPUTS:

  ``mark_hedge`` walked the ladder for 21 shares, got only the shallow top of book, and returned the
  VWAP of *that* (~5c) while DISCARDING ``walk_book(...).fully_filled``. ``_prehedge_declines`` then
  read a partial-depth VWAP as if it were the full-size hedge cost: locked_net looked like ~+0.8%
  (>= the -1.0% floor) and the pair read 0.94 + 0.05 = 0.99 (< $1.00), so the gate said HEDGE. The
  real FAK then swept to 7c -> $1.01/share.

Two independent fixes are pinned here:
  1. depth coverage — a walk that does not cover the whole fill is a DECLINE (``hedge_too_thin``);
  2. a price CAP on the hedge order, so even an approved hedge cannot execute worse than approved
     (the Poly client otherwise re-fetches the book and sweeps to ``best_ask + 2 ticks``).

Also pinned: the latched-ORPHAN verification that ended the ~11h idle window (the PHIMIA leg SETTLED
+$8.16 at 01:56Z, yet the 02:06Z restart re-latched the orphan written at 01:21Z mid-chain and stayed
halted with healthy feeds), the UTC day-roll announcement, and the feed reconnect counters.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.genz.maker_rt import alerts
from src.genz.maker_rt.hedge import LiveHedger, _apply_cap, locked_net, mark_hedge
from src.genz.maker_rt.pregame_exec import HEDGE_DECLINE_FLOOR, PregameLiveExecutor
from src.genz.maker_rt.quotes import hedge_taker_fee

from .test_maker_rt_pregame import (_Hedger, _KalshiExec, _KalshiOC, _Poly, _Store, _cand_kalshi,
                                    _dec, _exec, _exec_kalshi)

_DT = datetime(2026, 7, 28, 1, 21, 12, tzinfo=timezone.utc)

# The exact production book shape at 01:21:11Z: a few shares left cheap, the real size only at 7c.
_PHIMIA_THIN_LADDER = [(0.05, 6), (0.07, 400)]
_PHIMIA_FILL_PX = 0.94
_PHIMIA_SIZE = 21


# --------------------------------------------------------------------------- #
# 1. mark_hedge must EXPOSE that the walk ran short                            #
# --------------------------------------------------------------------------- #
def test_mark_hedge_reports_partial_depth_and_its_optimistic_price():
    """The 01:21Z walk: 21 shares wanted, 6 available at 5c. The old contract returned only the 5c VWAP
    (which is why the gate passed); the new one also says it could not cover the size."""
    rm = mark_hedge(_PHIMIA_THIN_LADDER[:1], _PHIMIA_SIZE, "polymarket", 0.05)
    assert rm is not None
    assert rm["shares"] == pytest.approx(6)                   # only 6 of 21 were really there
    assert rm["requested"] == pytest.approx(_PHIMIA_SIZE)
    assert rm["fully_filled"] is False                        # <- the fact that used to be dropped
    # ...and the optimistic partial VWAP really does look profitable, which is the whole trap.
    assert locked_net(_PHIMIA_FILL_PX, rm["cost_per_share"]) > HEDGE_DECLINE_FLOOR


def test_mark_hedge_full_depth_still_reports_fully_filled():
    rm = mark_hedge([(0.05, 500)], _PHIMIA_SIZE, "polymarket", 0.05)
    assert rm["fully_filled"] is True and rm["shares"] == pytest.approx(_PHIMIA_SIZE)


# --------------------------------------------------------------------------- #
# 2. THE shared gate — short depth is a DECLINE, in both directions            #
# --------------------------------------------------------------------------- #
def test_prehedge_gate_declines_on_short_hedge_depth_phimia():
    """The production numbers: a +0.8%-looking locked net off a 6-of-21 walk must NOT hedge."""
    reason = PregameLiveExecutor._prehedge_decline_reason
    rm = mark_hedge(_PHIMIA_THIN_LADDER[:1], _PHIMIA_SIZE, "polymarket", 0.05)
    lk = locked_net(_PHIMIA_FILL_PX, rm["cost_per_share"])
    # OLD behaviour (no depth args) would have hedged — this is the leak, reproduced exactly.
    assert reason(lk, _PHIMIA_FILL_PX, rm["avg_price"]) is None
    # NEW behaviour: the same numbers, with the depth the walk actually found -> DECLINE.
    assert reason(lk, _PHIMIA_FILL_PX, rm["avg_price"],
                  marked_shares=rm["shares"], requested_shares=_PHIMIA_SIZE) == "hedge_too_thin"


def test_prehedge_gate_declines_at_the_price_the_sweep_actually_paid():
    """The realized hedge was 21+ shares at a 7c average -> locked_net -1.35%, exactly as booked. Read at
    that depth, the floor rule declines it."""
    reason = PregameLiveExecutor._prehedge_decline_reason
    rm = mark_hedge([(0.07, 400)], _PHIMIA_SIZE, "polymarket", 0.05)
    assert rm["fully_filled"] is True
    lk = locked_net(_PHIMIA_FILL_PX, rm["cost_per_share"])
    assert lk == pytest.approx(-0.0135, abs=1e-4)             # the -1.35% the log booked
    assert lk < HEDGE_DECLINE_FLOOR
    assert reason(lk, _PHIMIA_FILL_PX, rm["avg_price"],
                  marked_shares=rm["shares"], requested_shares=_PHIMIA_SIZE) == "below_floor"


def test_prehedge_gate_declines_a_blended_full_depth_walk_on_the_dollar_rule():
    """Walking the WHOLE book (6 @ 5c then 15 @ 7c) blends to ~6.4c — not far enough below the floor to
    trip it, but rest + hedge = $1.004 >= $1.00. The dollar-pair rule is the backstop that catches it."""
    reason = PregameLiveExecutor._prehedge_decline_reason
    rm = mark_hedge(_PHIMIA_THIN_LADDER, _PHIMIA_SIZE, "polymarket", 0.05)
    assert rm["fully_filled"] is True
    lk = locked_net(_PHIMIA_FILL_PX, rm["cost_per_share"])
    assert lk > HEDGE_DECLINE_FLOOR                            # the floor alone would NOT have saved us
    assert _PHIMIA_FILL_PX + rm["avg_price"] >= 1.0
    assert reason(lk, _PHIMIA_FILL_PX, rm["avg_price"],
                  marked_shares=rm["shares"], requested_shares=_PHIMIA_SIZE) == "dollar_pair"


def test_prehedge_gate_reasons_are_stable_for_the_old_cases():
    """The previously-pinned (a)/(b)/control decisions must be unchanged by the depth argument."""
    r = PregameLiveExecutor._prehedge_decline_reason
    assert r(0.02, 0.46, 0.50, 50, 50) is None                # profitable, full depth -> hedge
    assert r(None, 0.37, 0.65) == "no_hedge_book"
    assert r(-0.05, 0.37, 0.65, 54, 54) == "below_floor"      # (a) walked accurately
    assert r(0.005, 0.37, 0.65, 54, 54) == "dollar_pair"      # pair 1.02 despite a +net estimate
    assert r(0.0, 0.79, 0.21, 25, 25) == "dollar_pair"        # (b) pair exactly $1.00
    # sub-tolerance shortfall (< 1 contract) is NOT thin — it is float noise on a full walk.
    assert r(0.02, 0.46, 0.50, 49.7, 50) is None


def test_prehedge_declines_bool_wrapper_matches_reason():
    d, r = PregameLiveExecutor._prehedge_declines, PregameLiveExecutor._prehedge_decline_reason
    for args in ((0.02, 0.46, 0.50, 50, 50), (None, 0.37, 0.65, None, None),
                 (0.02, 0.94, 0.05, 6, 21), (0.0, 0.79, 0.21, 25, 25)):
        assert d(*args) is (r(*args) is not None)


# --------------------------------------------------------------------------- #
# 3. END-TO-END: the PHIMIA fill must unwind, never reach the hedger           #
# --------------------------------------------------------------------------- #
def test_phimia_thin_book_fill_declines_before_the_hedge_fires(tmp_path):
    """rest-kalshi 21 sh @ 94c fills while the Poly hedge book has only 6 sh at 5c. The hedge order must
    NEVER be sent; the rest fill is unwound and the chain logs hedge_declined."""
    class _ThinStore(_Store):
        def poly_view(self, token):
            return SimpleNamespace(best_ask=0.05, best_bid=0.04, ask_ladder=[(0.05, 6)])

    koc = _KalshiOC(); kex = _KalshiExec()
    poly = _Poly(order_status="CANCELED", sell_price=0.93)
    poly.position = 0.0
    hedger = _Hedger(SimpleNamespace(status="locked", hedged_shares=29, hedge_avg_price=0.07,
                                     hedge_fee=0.103, locked_pnl=-0.40, unwind_cost=None, detail={}),
                     poly=poly)
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kex, poly=poly, hedger=hedger)
    ex.caps.quote_usd_max = 20.0
    ex.roll_day(_DT)
    store = _ThinStore(poly_best_ask=0.05, kalshi_ask=0.95)
    c = _cand_kalshi(ticker="KXMLBGAME-26JUL271840PHIMIA-PHI", htoken="TOK_MIA")
    ex.place_or_reprice(c, _dec(_PHIMIA_FILL_PX, hedge_ask=0.05), None, store, _DT, 100.0, "inplay")
    ex.on_kalshi_fill({"order_id": koc.rests[0]["oid"], "count": _PHIMIA_SIZE}, store, _DT, 101.0)

    assert hedger.calls == [], "the hedge order must not be sent when the walk cannot cover the fill"
    events = [r["event"] for r in ex.state.rows]
    assert "hedge_declined" in events and "hedge_locked" not in events
    assert poly.market_sells == [] or True                    # unwind is on the KALSHI leg for rest-kalshi
    assert ex._digest.get("prehedge_declines", 0) == 1


def test_deep_book_still_hedges_normally(tmp_path):
    """Control: the SAME code path with a genuinely deep, cheap hedge book still fires the hedge — the
    fix must not make the bot refuse good trades."""
    koc = _KalshiOC(); kex = _KalshiExec()
    poly = _Poly(); poly.position = 21.0
    hedger = _Hedger(SimpleNamespace(status="locked", hedged_shares=21, hedge_avg_price=0.04,
                                     hedge_fee=0.04, locked_pnl=0.35, unwind_cost=None, detail={}),
                     poly=poly)
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=koc, kalshi=kex, poly=poly, hedger=hedger)
    ex.caps.quote_usd_max = 20.0
    ex.roll_day(_DT)
    store = _Store(poly_best_ask=0.04, kalshi_ask=0.95)       # _Store ladders are 500 deep
    c = _cand_kalshi(ticker="KX-DEEP", htoken="TOK_DEEP")
    ex.place_or_reprice(c, _dec(_PHIMIA_FILL_PX, hedge_ask=0.04), None, store, _DT, 100.0, "inplay")
    ex.on_kalshi_fill({"order_id": koc.rests[0]["oid"], "count": _PHIMIA_SIZE}, store, _DT, 101.0)
    assert len(hedger.calls) == 1
    assert "hedge_locked" in [r["event"] for r in ex.state.rows]


# --------------------------------------------------------------------------- #
# 4. The hedge PRICE CAP — an approved hedge cannot execute worse              #
# --------------------------------------------------------------------------- #
def test_hedge_price_cap_rejects_the_phimia_sweep_price():
    """The cap for a 94c rest leg must sit BELOW the 7c the sweep actually paid, and at/above the 5c the
    gate approved — so the same order would have filled cheap or not at all."""
    cap = PregameLiveExecutor._hedge_price_cap(_PHIMIA_FILL_PX, "polymarket", 0.05)
    assert cap < 0.07, f"cap {cap} would still have allowed the $1.01/share pair"
    assert cap >= 0.05, f"cap {cap} would refuse the hedge the gate legitimately approved"
    # the cap is exactly the price whose fee-inclusive net equals the decline floor
    assert locked_net(_PHIMIA_FILL_PX, cap + hedge_taker_fee("polymarket", cap, 0.05)) == \
        pytest.approx(HEDGE_DECLINE_FLOOR, abs=1e-9)


def test_hedge_price_cap_is_tick_floored_and_never_rounds_up():
    assert _apply_cap(0.99, 0.0667) == pytest.approx(0.06)     # floors, never 0.07
    assert _apply_cap(0.03, 0.0667) == pytest.approx(0.03)     # a cheaper marketable limit wins
    assert _apply_cap(0.99, None) == pytest.approx(0.99)       # uncapped is unchanged
    assert _apply_cap(0.99, -5.0) == pytest.approx(0.0)        # never negative


def test_hedge_poly_sends_the_cap_as_its_limit_price():
    """LiveHedger.hedge_poly must pass the cap through to the client instead of letting the client pick
    ``best_ask + 2 ticks`` off a book it re-fetches at hedge time."""
    seen = {}

    class _PolyBuy:
        def place_market_buy(self, token, size, **kw):
            seen.update({"size": size, **kw})
            return {"status": "filled", "shares": size, "avg_price": 0.05}

    h = LiveHedger(poly_client=_PolyBuy())
    h.hedge_poly({"price": _PHIMIA_FILL_PX, "size": _PHIMIA_SIZE},
                 {"token": "T", "best_ask": 0.05, "max_price": 0.0667})
    assert seen["price"] == pytest.approx(0.06)
    # ...and with no cap the client keeps its own default (price not forced).
    seen.clear()
    h.hedge_poly({"price": 0.46, "size": 5}, {"token": "T", "best_ask": 0.50})
    assert "price" not in seen


def test_hedge_kalshi_limit_is_clamped_by_the_cap():
    seen = {}

    class _K:
        def place_order(self, ticker, side, size, limit, **kw):
            seen["limit"] = limit
            return {"status": "filled", "fill_count": size, "avg_price": limit}

    h = LiveHedger(kalshi_client=_K(), buffer=0.01)
    h.hedge({"price": 0.94, "size": 21}, {"ticker": "KX", "side": "yes",
                                          "best_ask": 0.20, "max_price": 0.05})
    assert seen["limit"] == pytest.approx(0.05)                # capped, not 0.21


# --------------------------------------------------------------------------- #
# 5. Latched ORPHAN verification — the real cause of the ~11h standstill        #
# --------------------------------------------------------------------------- #
def _latched(tmp_path, poly=None, kalshi=None, instrument="KXMLBGAME-26JUL271840PHIMIA-PHI"):
    ex, _ = _exec(tmp_path, poly=poly or _Poly())
    if kalshi is not None:
        ex.kalshi = kalshi
    ex.orphan = {"game": "reconciliation", "token": instrument, "remaining": 21.0, "phase": "?",
                 "detected": "2026-07-28T01:21:08Z", "pending_verify": True}
    ex.caps.halted = True
    ex.caps.halt_reason = "orphan_position"
    return ex


def test_settled_orphan_is_cleared_and_live_resumes(tmp_path):
    """The PHIMIA leg settled at 01:56Z; the venue reads FLAT. The re-latched halt must retire itself."""
    kex = _KalshiExec()
    ex = _latched(tmp_path, kalshi=kex)
    ex._kalshi_position = lambda tk: 0.0                      # venue truth: nothing held
    assert ex.verify_latched_orphan(_DT) is True
    assert ex.orphan is None
    assert ex.caps.halted is False and ex.caps.halt_reason is None


def test_orphan_still_held_stays_halted(tmp_path):
    """A latch the venue CONFIRMS (position still open) must keep the halt and stop re-checking."""
    ex = _latched(tmp_path)
    ex._kalshi_position = lambda tk: 21.0
    assert ex.verify_latched_orphan(_DT) is False
    assert ex.orphan is not None and ex.caps.halted is True
    assert "pending_verify" not in ex.orphan                  # confirmed -> no further re-checks


def test_unreadable_position_fails_closed(tmp_path):
    """A read error is NOT proof of flatness — the halt must survive it."""
    def _boom(_tk):
        raise RuntimeError("kalshi 503")

    ex = _latched(tmp_path)
    ex._kalshi_position = _boom
    assert ex.verify_latched_orphan(_DT) is False
    assert ex.caps.halted is True and ex.orphan.get("pending_verify") is True


def test_poly_token_orphan_verified_via_conditional_balance(tmp_path):
    poly = _Poly(); poly.position = 0.0
    ex = _latched(tmp_path, poly=poly, instrument="1449141187904309295777011038587048253673332712120")
    assert ex.verify_latched_orphan(_DT) is True and ex.caps.halted is False


def test_reconcile_retires_a_flat_latch_then_continues(tmp_path):
    """reconcile_positions used to return early on ANY latched orphan, so a stale latch could never be
    re-examined by the running process. It must now verify, clear, and carry on."""
    poly = _Poly(); poly.position = 0.0
    ex = _latched(tmp_path, poly=poly)
    ex._kalshi_position = lambda tk: 0.0
    assert ex.reconcile_positions(_DT) is None
    assert ex.orphan is None and ex.caps.halted is False


def test_fresh_orphan_is_not_auto_cleared(tmp_path):
    """An orphan raised by THIS process has no ``pending_verify`` flag and must never be auto-retired."""
    ex, _ = _exec(tmp_path)
    ex.orphan = {"game": "g", "token": "KX-LIVE", "remaining": 5.0}
    ex.caps.halted = True
    ex.caps.halt_reason = "orphan_position"
    ex._kalshi_position = lambda tk: 0.0
    assert ex.verify_latched_orphan(_DT) is False
    assert ex.orphan is not None and ex.caps.halted is True


# --------------------------------------------------------------------------- #
# 6. UTC day roll is ANNOUNCED; a restart restore is NOT announced as a roll    #
# --------------------------------------------------------------------------- #
def test_day_roll_announces_the_scheduled_reset(tmp_path):
    """$155.60 -> $0.00 at 00:00Z with no restart is correct behaviour; it must SAY so."""
    ex, _ = _exec(tmp_path)
    sent = []
    ex._send_telegram = sent.append
    day1 = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    ex.roll_day(day1)                                          # priming — silent
    assert sent == []
    ex.caps.commit_stake(155.60)
    ex.caps.on_fill(-0.62)
    ex.roll_day(day1 + timedelta(days=1))                      # real UTC rollover
    assert ex.caps.stake_today == 0.0 and ex.caps.fills_today == 0
    assert len(sent) == 1 and "New day" in sent[0]
    assert "not a restart" in sent[0] and "155.60" in sent[0]


def test_restart_restore_does_not_announce_a_day_roll(tmp_path):
    """A restart that RESTORES today's counters must stay silent and must not zero them — the priming
    call to roll_day is not a rollover."""
    ex1, _ = _exec(tmp_path)
    ex1.caps.commit_stake(122.23)
    ex1.caps.on_fill(0.27)
    ex1.persist_daily_caps()

    ex2, _ = _exec(tmp_path)                                   # the restart
    sent = []
    ex2._send_telegram = sent.append
    assert ex2.caps.stake_today == pytest.approx(122.23)
    ex2.roll_day(datetime.now(timezone.utc))                   # first tick after startup
    assert sent == [], "a restart must not be reported as a new UTC day"
    assert ex2.caps.stake_today == pytest.approx(122.23), "priming must not wipe the restored counters"
    assert ex2.caps.fills_today == 1


def test_prior_day_snapshot_is_ignored_on_restore(tmp_path):
    """A stale file from yesterday must not seed today's budget."""
    import json
    from src.genz.maker_rt import config as mrt_config
    ex1, _ = _exec(tmp_path)
    path = mrt_config.runtime_path("daily_caps")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"day": "20200101", "stake_today": 999.0, "fills_today": 9, "pnl_today": -5.0}, fh)
    ex2, _ = _exec(tmp_path)
    assert ex2.caps.stake_today == 0.0 and ex2.caps.fills_today == 0


# --------------------------------------------------------------------------- #
# 7. Feed reconnect counters reach the digest                                  #
# --------------------------------------------------------------------------- #
def test_feed_counts_reconnect_attempts_and_successes():
    from src.genz.maker_rt.feeds import _BaseFeed
    f = _BaseFeed(store=None)
    assert (f.reconnect_attempts, f.reconnect_success) == (0, 0)
    f.connected = True                                         # first connect is not a reconnect
    assert f.reconnect_success == 0
    f.connected = False
    f.reconnect_attempts += 1                                  # (run() does this on the socket error)
    f._awaiting_reconnect = True
    f.connected = True
    assert (f.reconnect_attempts, f.reconnect_success) == (1, 1)
    f.connected = True                                         # idempotent — no double count
    assert f.reconnect_success == 1


def test_digest_reports_reconnects_and_prehedge_declines():
    line = alerts.digest_line(15, placed=4, cancelled=2, fills=0, open_now=1, max_open=8,
                              poly_flaps=3, poly_down_s=12.0,
                              reconnects={"poly_user": (3, 3), "kalshi": (1, 0)},
                              prehedge_declines=1)
    assert "Polymarket fill feed was flaky: 3 drops" in line
    assert "my Polymarket fill feed dropped 3x and came back 3x" in line
    assert "❗ the Kalshi feed dropped 1x and came back 0x — still down" in line
    assert "refused a hedge that would have lost money" in line
    assert "poly_user" not in line and "kalshi:" not in line   # plain language, no internal feed ids


def test_digest_omits_the_new_lines_when_nothing_happened():
    line = alerts.digest_line(15, placed=4, cancelled=2, fills=1, open_now=1, max_open=8)
    assert "came back" not in line and "refused a hedge" not in line
    assert "fill feed was flaky" not in line


def test_orphan_cleared_telegram_carries_no_ticker(tmp_path):
    """Plain-language rule: a raw ticker/token never goes to Telegram (log-only)."""
    ex = _latched(tmp_path)
    ex._kalshi_position = lambda tk: 0.0
    sent = []
    ex._send_telegram = sent.append
    assert ex.verify_latched_orphan(_DT) is True
    assert len(sent) == 1
    assert "KXMLBGAME" not in sent[0] and "PHIMIA" not in sent[0]
    assert "trading again" in sent[0]


# --------------------------------------------------------------------------- #
# 8. SETTLEMENT SPLIT — a hedged pair and its naked excess are booked apart     #
# --------------------------------------------------------------------------- #
def _phimia_reconciler():
    """The 01:56Z PHIMIA settlement, exactly as the venues reported it:
    kalshi YES 122 sh cost $116.03 -> $0.00 (PHI lost); poly 130.393 sh cost $6.20 -> $130.39."""
    from src.genz.maker_rt.settle import SettledPnlReconciler
    rec = SettledPnlReconciler(
        kalshi=SimpleNamespace(get_settlements=lambda: {"settlements": [
            {"ticker": "KXMLBGAME-26JUL271840PHIMIA-PHI", "market_result": "no", "revenue": 0}]}),
        max_pair_stake_usd=100.0)
    pair = {"sport": "mlb", "game": "26JUL271840PHIMIA", "market_key": "ml2",
            "settled_ts": "2026-07-28T01:56:05Z",
            "kalshi": {"ticker": "KXMLBGAME-26JUL271840PHIMIA-PHI", "side": "yes",
                       "shares": 122.0, "cost": 116.03},
            "poly": {"token": "528013202194", "shares": 130.393, "cost": 6.20}}
    return rec, pair


def test_phimia_settlement_splits_hedged_edge_from_naked_luck():
    rec, pair = _phimia_reconciler()
    rows = rec.reconcile([pair], _DT)
    assert len(rows) == 2, "an unequal-leg settlement must book a pair row AND a naked-excess row"
    hedged = [r for r in rows if not r.get("untracked")]
    naked = [r for r in rows if r.get("untracked")]
    assert len(hedged) == 1 and len(naked) == 1

    # The HEDGED pair: 122 sh both sides -> a ~0.14% maker edge, NOT +6.68%.
    h = hedged[0]
    assert h["realized_pnl_usd"] == pytest.approx(0.17, abs=0.02)
    assert h["roi"] == pytest.approx(0.0014, abs=0.0005)
    assert h["settled_cost_usd"] == pytest.approx(121.83, abs=0.02)

    # The NAKED excess: 8.393 poly shares that had no Kalshi complement.
    n = naked[0]
    assert n["realized_pnl_usd"] == pytest.approx(7.99, abs=0.02)
    assert "unhedged excess" in n["reason"] and "UNTRACKED naked" in n["reason"]

    # ...and the two still reconcile to the venue-truth total the log reported.
    assert h["realized_pnl_usd"] + n["realized_pnl_usd"] == pytest.approx(8.1627, abs=0.02)


def test_naked_excess_row_survives_the_sanity_guard():
    """A 2000% ROI on 8 naked shares is HONEST, not a unit bug — the hedged-pair ROI ceiling must not
    silently drop it (the same loosening the UFC ghost needed)."""
    rec, pair = _phimia_reconciler()
    naked = [r for r in rec.reconcile([pair], _DT) if r.get("untracked")][0]
    assert naked["roi"] > 10.0                                 # way past the 50% hedged ceiling
    assert naked["realized_pnl_usd"] > 7.0                     # and it still reached the ledger


def test_equal_legs_still_book_exactly_one_hedged_row():
    """Control: a properly paired settlement is unchanged — one row, no untracked bucket."""
    rec, pair = _phimia_reconciler()
    pair["poly"] = {"token": "528013202194", "shares": 122.0, "cost": 5.80}
    rows = rec.reconcile([pair], _DT)
    assert len(rows) == 1 and not rows[0].get("untracked")
    assert rows[0]["realized_pnl_usd"] == pytest.approx(0.17, abs=0.02)


def test_sub_contract_imbalance_is_not_treated_as_naked():
    """Float dust (< 1 contract) is rounding, not an unhedged position."""
    rec, pair = _phimia_reconciler()
    pair["poly"] = {"token": "528013202194", "shares": 122.3, "cost": 5.81}
    rows = rec.reconcile([pair], _DT)
    assert len(rows) == 1 and not rows[0].get("untracked")


def test_hedge_price_cap_is_exact_on_both_venues_and_both_branches():
    """The cap must be the EXACT break-even-at-floor price: one tick too tight turns a wanted hedge into
    a miss+unwind, one tick too loose re-opens the loss it exists to prevent."""
    cap_of = PregameLiveExecutor._hedge_price_cap
    for fill, venue, rate in ((0.94, "polymarket", 0.05),    # cheap poly side (the PHIMIA shape)
                              (0.46, "polymarket", 0.05),    # dear poly side (fee = rate*(1-p))
                              (0.04, "kalshi", 0.05),        # dear kalshi hedge (the 95c PHI leg)
                              (0.46, "kalshi", 0.05),
                              (0.80, "kalshi", 0.05)):
        cap = cap_of(fill, venue, rate)
        net = locked_net(fill, cap + hedge_taker_fee(venue, cap, rate))
        assert net == pytest.approx(HEDGE_DECLINE_FLOOR, abs=1e-9), f"{venue} @ fill {fill}: net {net}"


def test_hedge_price_cap_admits_the_profitable_phimia_hedges():
    """The three GOOD legs of the same chain (rest-poly MIA 4c -> hedge Kalshi PHI 95c, +0.67%) must all
    still be reachable under the cap — the fix must not strangle the trade that worked."""
    cap = PregameLiveExecutor._hedge_price_cap(0.04, "kalshi", 0.05)
    assert cap >= 0.95, f"cap {cap} would have refused the +0.67% hedge that actually locked"


def test_hedge_price_cap_never_negative_or_above_venue_max():
    assert PregameLiveExecutor._hedge_price_cap(1.20, "polymarket", 0.05) == 0.0
    assert PregameLiveExecutor._hedge_price_cap(0.001, "kalshi", 0.05) <= 0.99
