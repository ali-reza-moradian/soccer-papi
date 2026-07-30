"""AUDIT PHASE 2, step 0 — INSTRUMENT FIRST, plus the blocker that instrumenting immediately exposed.

The audit's speed findings were all one shape: something synchronous on a single event loop, run far
more often than anyone had counted. The old ``blockers`` map kept a MAX per bucket, and a max cannot
tell one 5-second stall apart from 4,909 fast calls an hour — which need opposite fixes. So every
bucket now carries sum + count + max, the lag line carries a p99, and the two blocks that were
previously unattributed (the websocket callbacks and the heartbeat/summary write) are buckets too.

The second half of this file pins the reason the phase could not be measured when it started: maker_rt
had been HALTED for ten hours over a position both venues agreed was fully hedged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.genz.maker_rt.loopstats import LoopStats

from .test_maker_rt_pregame import _Log, _exec_kalshi

NOW = datetime(2026, 7, 30, 19, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# the buckets                                                                   #
# --------------------------------------------------------------------------- #
def test_buckets_carry_sum_and_count_not_only_a_max():
    """A max answers 'how bad did it get once'. It cannot answer 'how much of this minute did this
    subsystem consume', which is the only question a de-stall can be proven against."""
    s = LoopStats()
    for ms in (10.0, 20.0, 30.0):
        s.add("fill_poll", ms)
    assert s.sum_ms("fill_poll") == 60.0
    assert s.count("fill_poll") == 3
    assert s.max_ms("fill_poll") == 30.0


def test_busiest_bucket_is_by_total_time_not_by_worst_call():
    """The exact confusion the audit had to resolve by hand: ONE 5s quote block looks worse than the
    cancel storm, while the storm's 4,909 fast calls consume twice the wall time. Only a sum sees it."""
    s = LoopStats()
    s.add("quotes", 5000.0)                       # one bad call
    for _ in range(4909):
        s.add("cancel", 2.0)                      # the measured storm: 9,818ms in total
    assert s.worst_bucket() == "cancel"
    assert s.render().startswith("cancel ")       # rendered busiest-first
    assert "4909x" in s.render()


def test_a_timer_records_even_when_the_body_raises():
    """Measurement must not lose the one sample that mattered — the call that blew up."""
    s = LoopStats()
    try:
        with s.timer("reconcile"):
            raise RuntimeError("venue exploded")
    except RuntimeError:
        pass
    assert s.count("reconcile") == 1


def test_an_unknown_bucket_is_created_rather_than_dropped():
    """Instrumentation is total by construction: a new call site must never be able to lose its sample
    (or raise) just because nobody added its name to a list first."""
    s = LoopStats()
    s.add("something_new", 7.0)
    assert s.count("something_new") == 1 and "something_new" in s.render()
    s.add("bad", "not a number")                  # a non-numeric sample is ignored, never raised
    assert s.count("bad") == 0


def test_p99_is_reported_alongside_p50_and_max():
    """p50 said 42ms while the loop was freezing for seconds; max said 6,149ms once. The shape of the
    tail is the p99, and it was the number the old line did not have."""
    s = LoopStats()
    for i in range(240):                          # ~one 60s reporting window at a 250ms debounce
        s.note_tick(float(i))
    p50, p99, mx = s.lag_percentiles()
    assert (p50, mx) == (120.0, 239.0)
    assert p99 == 237.0, "the third-worst tick of the window — not the median, not the one-off max"
    # The diagnostic claim: a handful of multi-second freezes move the p99 while p50 says nothing at all.
    s2 = LoopStats()
    for _ in range(237):
        s2.note_tick(40.0)
    for _ in range(3):
        s2.note_tick(4000.0)
    p50b, p99b, _ = s2.lag_percentiles()
    assert p50b == 40.0 and p99b == 4000.0


def test_a_negative_lag_is_clamped_not_recorded_as_a_stall():
    """A tick that beat its debounce target is not negative lag; it is zero."""
    s = LoopStats()
    s.note_tick(-50.0)
    assert s.lag_percentiles() == (0.0, 0.0, 0.0)


def test_reset_starts_a_clean_window():
    s = LoopStats()
    s.add("settle", 5.0)
    s.note_tick(1.0)
    s.reset()
    assert s.count("settle") == 0 and s.ticks == 0 and s.render() == "idle"


def test_the_main_loop_wires_every_blocking_block_to_a_bucket():
    """A bucket nobody calls measures nothing. These are the blocks the audit named as unattributed."""
    import inspect
    from src.genz.maker_rt import __main__ as m
    src = inspect.getsource(m)
    for bucket in ("quotes", "fill_poll", "reconcile", "settle", "ws_cb", "heartbeat", "telegram",
                   "trees"):
        assert f'STATS.timer("{bucket}")' in src, f"{bucket} is declared but never timed"
    assert "blockers" not in src, "the old max-only map is gone"


# --------------------------------------------------------------------------- #
# the blocker: a latch over a HEDGED leg could never retire                      #
# --------------------------------------------------------------------------- #
sent: list = []


def _latched(tmp_path, *, held, expected):
    """An executor with a re-latched orphan on KXCLUBFTOTAL-26JUL30BOUFCA-4, the venue holding ``held``
    contracts, of which ``expected`` are a registered leg of a booked pair."""
    kx = SimpleNamespace(positions={"KXCLUBFTOTAL-26JUL30BOUFCA-4": held})
    kx.get_positions = lambda: {"market_positions": [{"ticker": t, "position": p}
                                                     for t, p in kx.positions.items()]}
    ex, _ = _exec_kalshi(tmp_path, kalshi=kx)
    ex.roll_day(NOW)
    ex.log = _Log()
    ex.telegram = lambda t: sent.append(t)
    inst = "KXCLUBFTOTAL-26JUL30BOUFCA-4"
    if expected:
        ex._register_expected("kalshi", inst, "YES", expected, "26JUL30BOUFCA", "total_goals|3.5", NOW)
    ex.orphan = {"game": "reconciliation", "token": inst, "remaining": held, "phase": "?",
                 "detected": "2026-07-30T04:33:13Z", "pending_verify": True}
    ex.caps.halted, ex.caps.halt_reason = True, "orphan_position"
    return ex


def test_a_latch_over_a_fully_hedged_leg_retires_itself(tmp_path):
    """2026-07-30: a reconciliation sweep and a fill landed in the SAME SECOND. The sweep saw 51.97
    unexplained Kalshi contracts and halted at 04:33:18; the chain registered them as an EXPECTED leg and
    booked the pair LOCKED (+$1.56, Poly holding 51.96 of the complement) at 04:33:20. The bot then sat
    halted for ten hours over a position both venues agreed was hedged — because the retirement path
    asked 'do we hold any?' while the detection path asks 'do we hold MORE than we expect?'."""
    sent.clear()
    ex = _latched(tmp_path, held=51.97, expected=51.97)
    assert ex.verify_latched_orphan(NOW) is True
    assert ex.orphan is None and ex.caps.halted is False and ex.caps.halt_reason is None
    assert sent and "fully hedged" in sent[0], "and the operator is told the truth: it is HELD, not gone"
    assert "holding nothing" not in sent[0]


def test_a_genuinely_unexplained_holding_still_stays_halted(tmp_path):
    """The rail is unchanged where it matters: shares with no expected leg behind them are an orphan."""
    sent.clear()
    ex = _latched(tmp_path, held=51.97, expected=0.0)
    assert ex.verify_latched_orphan(NOW) is False
    assert ex.caps.halted is True and ex.orphan is not None
    assert any("ORPHAN CONFIRMED" in w for w in ex.log.warns)


def test_holding_more_than_the_expected_leg_still_stays_halted(tmp_path):
    """Explained means 'no MORE than expected'. Twenty extra contracts on top of a booked leg are
    exposure nobody chose, and they must keep the halt exactly as before."""
    ex = _latched(tmp_path, held=71.97, expected=51.97)
    assert ex.verify_latched_orphan(NOW) is False
    assert ex.caps.halted is True


def test_an_unreadable_position_keeps_the_halt(tmp_path):
    """FAIL CLOSED: 'I could not ask' is never 'it is fine'."""
    ex = _latched(tmp_path, held=51.97, expected=51.97)

    def _boom():
        raise RuntimeError("kalshi 500")
    ex.kalshi.get_positions = _boom
    assert ex.verify_latched_orphan(NOW) is False
    assert ex.caps.halted is True


def test_a_flat_instrument_still_clears_with_the_settled_wording(tmp_path):
    """The original path is untouched: nothing held -> cleared, and the operator hears the right story."""
    sent.clear()
    ex = _latched(tmp_path, held=0.0, expected=51.97)
    assert ex.verify_latched_orphan(NOW) is True
    assert ex.caps.halted is False
    assert sent and "holding nothing" in sent[0]
