"""The 2026-07-30 17:04Z halt: a store=None hedge, a latch that could not retire, and a stale alert.

One fill produced all three. The sequence, from the log:

  17:04:50  HEDGE CHAIN RAISED (attempt 1/3): 'NoneType' object has no attribute 'poly_view'
  17:04:54  FREEZE 26JUL30ILVSTJ ... disarmed 6 line(s) of this game
  17:04:55  attempt 2/3, same error
  17:05:05  attempt 3/3, same error
  17:05:08  ORPHAN POSITION ... remaining=113.56 (hedge chain raised 3x in a row) — HALTED
  17:05:11  ... the COMPLEMENT position confirms FULLY HEDGED (113.56 sh) — booked LOCKED
  17:05:11  HEDGED AT A LOSS ... locked_net -0.00% ... "the pre-hedge check should have declined this"

The pair was hedged three seconds AFTER the halt latched, at a net inside the -1% execution floor. So
the bot spent 26 minutes halted over a correctly hedged position, and shouted a red error about a pair
the policy had just chosen to accept.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.genz.maker_rt import alerts
from src.genz.maker_rt.pregame_exec import PregameLiveExecutor as PX

from .test_maker_rt_pregame import _Log, _Store, _cand, _dec, _exec, _exec_kalshi

NOW = datetime(2026, 7, 30, 17, 5, 8, tzinfo=timezone.utc)
_STORE = _Store(kalshi_ask=0.60, poly_best_ask=0.55)
_PDEC = lambda: _dec(0.38, hedge_ask=0.60)          # noqa: E731
TICKER = "KXUECLTOTAL-26JUL30ILVSTJ-4"


def _ex(tmp_path):
    ex, _ = _exec(tmp_path)
    ex.roll_day(NOW)
    ex.log = _Log()
    ex.caps.max_open_quotes = 12
    ex.caps.max_daily_stake_usd = 10_000.0
    ex.caps.max_fills_per_day = 20
    return ex


# --------------------------------------------------------------------------- #
# 1a. THE ROOT CAUSE — an off-loop batch applied without a book                 #
# --------------------------------------------------------------------------- #
def test_a_batch_result_is_never_applied_without_a_book(tmp_path):
    """``_cancel`` drains the off-loop mailbox, and the sweeping callers (shock freeze, disarm, churn)
    cancel BY KEY with no store in hand. A batched fill poll applied on that path reaches ``_hedge_fill``,
    which needs the book to price the hedge — and raised AttributeError three times in a row."""
    ex = _ex(tmp_path)
    ex._worker._results.append((("fill_poll",), {"index": {}, "per_order": {}, "venue_ok": {},
                                                 "covered": set()}, None))
    applied = []
    ex._apply_fill_poll_batch = lambda *a, **k: applied.append(a)
    ex._drain_cancel_results(None, NOW, 0.0)                 # the no-store path
    assert applied == [], "nothing was decided without a book"
    assert len(ex._deferred_offloop) == 1, "and nothing was DROPPED either"
    ex._drain_cancel_results(_STORE, NOW, 100.0)             # the loop, which has one
    assert len(applied) == 1 and ex._deferred_offloop == []


def test_the_deferred_mailbox_is_bounded(tmp_path):
    """A caller that never supplies a book must not grow the mailbox without limit."""
    ex = _ex(tmp_path)
    for _ in range(100):
        ex._worker._results.append((("fill_poll",), {}, None))
        ex._drain_cancel_results(None, NOW, 0.0)
    assert len(ex._deferred_offloop) <= 32


def test_a_shock_freeze_cancel_cannot_enter_the_hedge_path(tmp_path):
    """The exact trigger: phase 3 made the freeze GAME-WIDE, so one shock cancels six lines by key with
    no store — six chances per shock to apply a batch that needs a book. It fired within four seconds."""
    ex = _ex(tmp_path)
    c = _cand()
    ex.place_or_reprice(c, _PDEC(), None, _STORE, NOW, 100.0, "pre")
    ex._worker._results.append((("fill_poll",), {"index": {}, "per_order": {}, "venue_ok": {},
                                                 "covered": set()}, None))
    boom = []
    ex._hedge_fill = lambda *a, **k: boom.append(a)
    ex.cancel_key(c.key, NOW, "shock_freeze")                # no store, exactly as the driver calls it
    assert boom == [], "the hedge chain was never entered without a book"


# --------------------------------------------------------------------------- #
# 1b. THE LATCH THAT COULD NOT RETIRE ITSELF                                    #
# --------------------------------------------------------------------------- #
def _latched_in_process(tmp_path, *, held, expected):
    kx = SimpleNamespace(positions={TICKER: held})
    kx.get_positions = lambda: {"market_positions": [{"ticker": t, "position": p}
                                                     for t, p in kx.positions.items()]}
    ex, _ = _exec_kalshi(tmp_path, kalshi=kx)
    ex.roll_day(NOW)
    ex.log = _Log()
    ex.telegram = lambda t: None
    if expected:
        ex._register_expected("kalshi", TICKER, "YES", expected, "26JUL30ILVSTJ",
                              "total_goals|3.5", NOW)
    # Latched IN PROCESS by the hedge-exception escalation — not loaded from disk at startup.
    ex._orphan_detected("26JUL30ILVSTJ", TICKER, "inplay", held,
                        "hedge chain raised 3x in a row", NOW)
    return ex


def test_an_in_process_latch_is_marked_re_verifiable(tmp_path):
    """``pending_verify`` was set ONLY by the startup loader, so a latch this process created could be
    retired only by a RESTART. That is the 26-minute halt: the pair was hedged three seconds after the
    latch and nothing was allowed to look again.

    Note the ORDER, because it is the whole incident: at latch time there is NO expected leg yet (the
    hedge has not completed, which is precisely why the chain was retrying), so ``_orphan_detected``'s
    own last-line guard cannot save us — it correctly refuses to latch only when the leg is ALREADY
    registered. The retirement path is the one that has to cope, and it could not."""
    ex = _latched_in_process(tmp_path, held=113.56, expected=0.0)
    assert ex.orphan is not None and ex.caps.halted is True
    assert ex.orphan.get("pending_verify") is True, "an in-process latch must be re-checkable"


def test_the_guard_refuses_to_latch_when_the_leg_is_already_explained(tmp_path):
    """The other half, unchanged: if the hedge HAS registered by latch time, no halt is taken at all."""
    ex = _latched_in_process(tmp_path, held=113.56, expected=113.56)
    assert ex.orphan is None and ex.caps.halted is False


def test_a_confirmed_orphan_keeps_being_re_checked(tmp_path):
    """The old code popped ``pending_verify`` on a confirmed orphan, which disabled every FUTURE check —
    so a position that was naked at 17:05 and hedged at 17:06 stayed halted until a human noticed."""
    ex = _latched_in_process(tmp_path, held=113.56, expected=0.0)
    assert ex.verify_latched_orphan(NOW) is False
    assert ex.orphan.get("pending_verify") is True, "still re-checkable"
    ex._register_expected("kalshi", TICKER, "YES", 113.56, "26JUL30ILVSTJ", "total_goals|3.5", NOW)
    assert ex.verify_latched_orphan(NOW) is True, "the next pass retires it"
    assert ex.caps.halted is False


def test_a_genuinely_naked_latch_still_halts(tmp_path):
    """The rail is unchanged where it matters: shares with no expected leg behind them keep the halt."""
    ex = _latched_in_process(tmp_path, held=113.56, expected=0.0)
    for _ in range(5):
        assert ex.verify_latched_orphan(NOW) is False
    assert ex.caps.halted is True and ex.orphan is not None


def test_the_confirmed_scream_is_throttled(tmp_path):
    """Re-checking every reconcile pass must not mean screaming every reconcile pass."""
    ex = _latched_in_process(tmp_path, held=113.56, expected=0.0)
    for _ in range(20):
        ex.verify_latched_orphan(NOW)
    assert len([w for w in ex.log.warns if "ORPHAN CONFIRMED" in w]) == 1


# --------------------------------------------------------------------------- #
# 2. THE STALE ALERT THRESHOLD                                                  #
# --------------------------------------------------------------------------- #
def test_a_pair_inside_the_floor_is_not_an_error():
    """The Ilves fill: rest 0.07 + hedge 0.93 = $1.00/share, locked_net -0.00%. With the floor at -1.0%
    that is WITHIN policy — locking it is cheaper than unwinding it, which is why the floor is -1%."""
    assert PX.pair_outcome(0.07, 0.93, -0.00004, -0.010) == "within_floor"


def test_the_classifier_tracks_the_floor_at_three_settings():
    """Pinned at three floors so the guard can never drift out of sync with the rail it describes."""
    net = -0.005                                             # a pair netting -0.5%
    assert PX.pair_outcome(0.30, 0.705, net, -0.010) == "within_floor"   # floor -1.0% -> allowed
    assert PX.pair_outcome(0.30, 0.705, net, -0.003) == "breach"         # floor -0.3% -> refused
    assert PX.pair_outcome(0.30, 0.705, net, 0.0) == "breach"            # break-even  -> refused
    assert PX.pair_outcome(0.30, 0.60, 0.10, 0.0) == "profit"            # a real profit, any floor


def test_a_breach_is_still_a_breach():
    """The rail that matters is untouched: a -2% hedge like the golubic fill is still a red ERROR."""
    assert PX.pair_outcome(0.30, 0.72, -0.020, -0.010) == "breach"
    assert PX.pair_outcome(0.30, 0.72, None, -0.010) == "breach", "an unknown net is never 'fine'"


def test_the_within_floor_alert_says_no_action_needed():
    text = alerts.format_event("locked_thin", sport="soccer", teams="Ilves vs Stjarnan", side="over",
                               market_key="total_goals|3.5", venue="kalshi", hedge_venue="polymarket",
                               rest_price=0.07, rest_shares=113.56, hedge_price=0.93,
                               hedge_shares=114.23, pnl=-0.004, net_pct=-0.004, floor_pct=-1.0)
    assert "within policy" in text and "no action needed" in text
    assert "Investigate" not in text and "HEDGED AT A LOSS" not in text
    assert "floor -1.00%" in text


def test_the_breach_alert_names_the_floor_not_a_dollar():
    """It used to say "(>= $1.00)" — a break-even assumption baked into copy, which is how it ended up
    firing on a pair the configured floor had just accepted."""
    text = alerts.format_event("locked_loss", sport="soccer", teams="A vs B", side="over",
                               market_key="ml2", venue="kalshi", hedge_venue="polymarket",
                               rest_price=0.30, rest_shares=100, hedge_price=0.73, hedge_shares=100,
                               pnl=-3.0, net_pct=-3.0, floor_pct=-1.0)
    assert "worse than the execution floor" in text and "floor -1.00%" in text
    assert "Investigate" in text
