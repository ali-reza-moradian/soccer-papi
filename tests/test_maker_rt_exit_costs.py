"""THE EXIT TOLL IS REAL MONEY — it belongs in lifetime pnl (src/genz/maker_rt/state.py).

A fill we cannot hedge is unwound at a real cost in real venue cash. The market it happened in then
settles with us FLAT, so no ``trade_settled`` row is ever written for it — and until this shipped, the
only record of the toll was ``unwind_cost_today``, which rolls to zero at UTC midnight. Flat-to-flat
since the 2026-07-31 baseline the books therefore claimed +$8.53 while the exchanges had moved +$3.30;
the whole $5.23 gap was three unwinds ($6.99) less $1.76 of baseline mark-vs-cost noise.

The invariant these tests hold:  hedged = lifetime - untracked - exits.  Booking the toll pulls
``settled_pnl_lifetime`` down to what the venues actually did WITHOUT moving the hedged edge, because
the toll was never edge in the first place.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from src.genz.maker_rt import config as mrt_config
from src.genz.maker_rt.state import MakerState, hedged_lifetime, load_tuning

NOW = datetime(2026, 8, 2, 0, 31, 19, tzinfo=timezone.utc)
NEXT_DAY = datetime(2026, 8, 3, 0, 5, 0, tzinfo=timezone.utc)


def _exit(event="hedge_unwound", cost=2.20, **kw):
    row = {"event": event, "mode": "live", "sport": "soccer", "game": "26AUG01DIMCAL",
           "market_key": "total_goals|1.5", "side": "over", "phase": "pre", "unwind_cost": cost}
    row.update(kw)
    return row


def _settled(net, *, untracked=False, cost=100.0):
    return {"event": "trade_settled", "sport": "soccer", "game": "G", "market_key": "m",
            "realized_pnl_usd": net, "settled_cost_usd": cost, "untracked": untracked}


# --------------------------------------------------------------------------- #
# the formula, in one place                                                     #
# --------------------------------------------------------------------------- #
def test_the_hedged_formula_subtracts_both_luck_and_the_exit_toll():
    """Exits are NEGATIVE, so subtracting them makes hedged LARGER than lifetime — that is the point:
    the toll is a cost the hedged strategy never incurred on a pair it completed."""
    assert hedged_lifetime(25.3947, 8.3788, -6.99) == 24.0059
    assert hedged_lifetime(None, None, None) == 0.0
    assert hedged_lifetime("nonsense", 1.0, 0.0) == -1.0


# --------------------------------------------------------------------------- #
# which events book, and which must NOT                                         #
# --------------------------------------------------------------------------- #
def test_a_verified_unwind_lands_in_both_lifetime_and_the_exits_bucket():
    st = MakerState()
    st.record(_exit(cost=2.20), NOW)
    assert st.settled_pnl_lifetime == -2.20
    assert st.settled_pnl_exits_lifetime == -2.20
    assert st.settled_exits == 1
    assert st.unwind_cost_today == 2.20, "the daily counter still does its own job"


def test_a_declined_hedge_books_only_when_it_actually_paid_something():
    """``hedge_declined`` covers two different things: a fill unwound because the hedge was too dear
    (real cash), and a fill that needed no exit at all or left un-closable dust (no cash). Booking the
    zero-cost ones would put a $0.00 exit in the count and in every ratio computed from it."""
    st = MakerState()
    st.record(_exit("hedge_declined", cost=0.0, reason="dust_below_venue_minimum"), NOW)
    assert st.settled_exits == 0 and st.settled_pnl_exits_lifetime == 0.0
    st.record(_exit("hedge_declined", cost=1.25), NOW)
    assert st.settled_exits == 1 and st.settled_pnl_exits_lifetime == -1.25


def test_unwind_failed_is_never_booked_because_that_position_still_exists():
    """``unwind_FAILED`` means we did NOT get flat. The shares are still ours, marked provisionally, and
    their real outcome arrives later as mark_corrected / trade_settled — which already reach lifetime.
    Booking the worst-case guess here as well would count the same shares twice."""
    st = MakerState()
    st.record(_exit("unwind_FAILED", cost=48.30), NOW)
    assert st.settled_pnl_lifetime == 0.0 and st.settled_pnl_exits_lifetime == 0.0
    assert st.settled_exits == 0
    assert st.unwind_cost_today == 48.30 and st.lifetime_unwinds == 1, "still a paid-toll OUTCOME"
    # ...and when the venue later says what it really came to, THAT is what reaches lifetime.
    st.record({"event": "mark_corrected", "sport": "soccer", "game": "26AUG01DIMCAL",
               "market_key": "total_goals|1.5", "realized_pnl_usd": -12.5,
               "settled_cost_usd": 48.30}, NOW)
    assert st.settled_pnl_lifetime == -12.5, "booked ONCE, at venue truth"


def test_an_auto_flatten_is_a_verified_exit_and_books_like_one():
    """``auto_flattened`` is the bounded sweep that closes a position the first unwind could not, and it
    books only AFTER the position reads flat — terminal on exactly the same terms as hedge_unwound. It
    has never fired (0 rows in every CSV ever written), which is why it was worth closing: its cost
    would have vanished from lifetime exactly as the three unwinds did, with nobody looking."""
    st = MakerState()
    st.record(_exit("auto_flattened", cost=3.40), NOW)
    assert st.settled_pnl_lifetime == -3.40
    assert st.settled_pnl_exits_lifetime == -3.40 and st.settled_exits == 1
    assert st.unwind_cost_today == 3.40, "and it counts as a paid exit in the daily counters too"
    assert st.lifetime_unwinds == 1 and list(st.recent_outcomes) == [1]


def test_an_auto_flatten_that_RECOVERED_money_books_the_gain():
    """The row carries ``unwind_cost = -realized``, so a sweep that came back BETTER than the position
    cost is a negative toll. Lifetime has to be able to move either way or it is not venue truth."""
    st = MakerState()
    st.record(_exit("auto_flattened", cost=-1.25), NOW)
    assert st.settled_pnl_lifetime == 1.25 and st.settled_pnl_exits_lifetime == 1.25


def test_booking_the_toll_moves_lifetime_but_not_the_hedged_edge():
    """The reason the toll goes into lifetime at all: lifetime is the number the balance audit compares
    against venue cash, and the cash moved. The reason it ALSO goes into its own bucket: the hedged
    number must stay a measurement of the hedged strategy."""
    st = MakerState()
    st.record(_settled(1.80), NOW)                      # a clean pair
    st.record(_settled(-2.2737, untracked=True, cost=2.27), NOW)   # its naked remainder
    before = st.summary("live", {}, NOW)
    st.record(_exit(cost=6.99), NOW)
    after = st.summary("live", {}, NOW)
    assert after["settled_pnl_lifetime"] == round(before["settled_pnl_lifetime"] - 6.99, 4)
    assert after["settled_pnl_exits_lifetime"] == -6.99
    assert after["settled_pnl_hedged_lifetime"] == before["settled_pnl_hedged_lifetime"]
    assert after["settled_pnl_hedged_lifetime"] == 1.80


def test_the_summary_and_the_heartbeat_agree_about_all_four_numbers():
    """The panel reads the summary and the operator reads the heartbeat. Two surfaces disagreeing about
    lifetime pnl is how a real gap gets argued about instead of investigated."""
    st = MakerState()
    st.record(_settled(4.0), NOW)
    st.record(_settled(8.3788, untracked=True, cost=8.0), NOW)
    st.record(_exit(cost=6.99), NOW)
    s, hb = st.summary("live", {}, NOW), st.heartbeat("live", {}, 0, NOW)
    for k in ("settled_pnl_lifetime", "settled_pnl_hedged_lifetime",
              "settled_pnl_untracked_lifetime", "settled_pnl_exits_lifetime"):
        assert s[k] == hb[k], k
    assert s["settled_pnl_hedged_lifetime"] == hedged_lifetime(
        s["settled_pnl_lifetime"], s["settled_pnl_untracked_lifetime"], s["settled_pnl_exits_lifetime"])


def test_the_exit_bucket_survives_the_daily_roll():
    """``unwind_cost_today`` resetting at UTC midnight is exactly the bug. The lifetime bucket must not."""
    st = MakerState()
    st.record(_exit(cost=6.99), NOW)
    st.record({"event": "quote", "sport": "soccer", "phase": "pre"}, NEXT_DAY)   # rolls the day
    assert st.unwind_cost_today == 0.0, "the DAILY counter rolls, as designed"
    assert st.settled_pnl_exits_lifetime == -6.99 and st.settled_pnl_lifetime == -6.99
    assert st.settled_exits == 1


# --------------------------------------------------------------------------- #
# idempotency across the ~10-21 restarts a working day sees                     #
# --------------------------------------------------------------------------- #
def test_a_deploy_restart_does_not_double_book_the_toll():
    """The maker restarts on every deploy. An exit persists its counters IMMEDIATELY (like hedge_locked)
    and the fresh process reads them back — it does not replay the CSV, so there is exactly one booking
    per exit no matter how many times the process comes up."""
    st = MakerState()
    st.record(_exit(cost=2.20), NOW)
    st.record(_exit(cost=4.75), NOW)
    assert st.settled_pnl_exits_lifetime == -6.95

    for _ in range(3):                                   # three deploys, back to back
        fresh = MakerState()
        fresh.load_tuning()
        assert fresh.settled_pnl_exits_lifetime == -6.95
        assert fresh.settled_pnl_lifetime == -6.95
        assert fresh.settled_exits == 2
        fresh.persist_tuning()
    assert load_tuning()["settled_pnl_exits_lifetime"] == -6.95


# --------------------------------------------------------------------------- #
# the one-time keyed restatement                                                #
# --------------------------------------------------------------------------- #
def _write_restatement(entries):
    with open(mrt_config.runtime_path("restatements"), "w", encoding="utf-8") as fh:
        json.dump(entries, fh)


def test_the_restatement_books_the_three_unwinds_once_and_refuses_to_repeat():
    """-$6.99 of exits that happened before the counter existed, confirmed against venue cash in
    data/ops/CASH_CLASSIFIED_20260804.txt. It must land exactly once across every future restart."""
    _write_restatement([{"key": "exits-20260804", "exits_usd": -6.99, "exits_n": 3,
                         "note": "three verified unwinds, 2026-08-01..02"}])
    st = MakerState()
    st.settled_pnl_lifetime, st.settled_pnl_untracked_lifetime = 32.3847, 8.3788

    assert st.apply_restatements() == ["exits-20260804"]
    assert round(st.settled_pnl_lifetime, 4) == 25.3947
    assert st.settled_pnl_exits_lifetime == -6.99 and st.settled_exits == 3
    assert st.summary("live", {}, NOW)["settled_pnl_hedged_lifetime"] == 24.0059, "hedged UNMOVED"

    # DATED, so the 8-hourly audit can tell this apart from $6.99 leaving the account.
    entry = st.restatement_log[0]
    assert entry["key"] == "exits-20260804" and entry["usd"] == -6.99
    assert entry["applied_ts"].endswith("Z") and entry["effective_ts"].endswith("Z")

    assert st.apply_restatements() == [], "same object, second call"
    for _ in range(5):                                   # five restarts
        fresh = MakerState()
        fresh.load_tuning()
        assert fresh.apply_restatements() == []
        assert round(fresh.settled_pnl_lifetime, 4) == 25.3947
        assert fresh.settled_pnl_exits_lifetime == -6.99


def test_the_key_and_the_counters_are_persisted_by_the_same_write():
    """If the key could land without the money (or the money without the key) a crash between the two
    would either double-book or silently drop the correction. One atomic write, both facts."""
    _write_restatement([{"key": "k1", "exits_usd": -1.5, "exits_n": 1}])
    st = MakerState()
    st.apply_restatements()
    on_disk = load_tuning()
    assert on_disk["settled_pnl_exits_lifetime"] == -1.5
    assert on_disk["restatements_applied"] == ["k1"]


def test_an_entry_applied_before_the_log_existed_is_reconstructed_without_moving_money():
    """The -$6.99 landed on a build that had no ``restatement_log``. Without a dated record the balance
    audit cannot tell our own correction from cash leaving, and would alarm on it at every check — but
    rebuilding it must NOT re-apply the money."""
    _write_restatement([{"key": "exits-20260804", "exits_usd": -6.99, "exits_n": 3,
                         "effective_ts": "2026-08-02T22:00:43Z",
                         "applied_ts": "2026-08-04T14:54:50Z", "note": "three verified unwinds"}])
    st = MakerState()
    st.settled_pnl_lifetime, st.settled_pnl_untracked_lifetime = 25.3947, 8.3788
    st.settled_pnl_exits_lifetime, st.settled_exits = -6.99, 3
    st.restatements_applied = ["exits-20260804"]          # applied; no log entry

    assert st.apply_restatements() == [], "the money is already booked"
    assert round(st.settled_pnl_lifetime, 4) == 25.3947, "NOT re-applied"
    assert st.settled_pnl_exits_lifetime == -6.99 and st.settled_exits == 3
    e = st.restatement_log[0]
    assert (e["key"], e["usd"], e["reconstructed"]) == ("exits-20260804", -6.99, True)
    assert e["applied_ts"] == "2026-08-04T14:54:50Z"
    assert e["effective_ts"] == "2026-08-02T22:00:43Z"
    assert load_tuning()["restatement_log"][0]["usd"] == -6.99, "persisted"

    st2 = MakerState()
    st2.load_tuning()
    assert st2.apply_restatements() == []
    assert len(st2.restatement_log) == 1, "reconstructed ONCE, not once per restart"


def test_a_restatement_can_correct_the_other_buckets_too():
    """The mechanism is general — an untracked or hedged correction rides the same key."""
    _write_restatement([{"key": "mixed", "exits_usd": -1.0, "untracked_usd": 2.0, "hedged_usd": 0.5}])
    st = MakerState()
    st.apply_restatements()
    assert st.settled_pnl_lifetime == 1.5
    assert st.settled_pnl_exits_lifetime == -1.0 and st.settled_pnl_untracked_lifetime == 2.0
    assert st.summary("live", {}, NOW)["settled_pnl_hedged_lifetime"] == 0.5


def test_no_restatements_file_is_a_no_op_not_an_error():
    st = MakerState()
    assert st.apply_restatements() == []
    assert st.settled_pnl_lifetime == 0.0


def test_an_unreadable_restatements_file_is_preserved_and_ignored():
    """Silently defaulting to {} over money state is how a BOM cost this system $329.96 of committed
    stake. Move the bytes aside; never apply a half-parsed correction."""
    p = mrt_config.runtime_path("restatements")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("{not json at all")
    st = MakerState()
    assert st.apply_restatements() == []
    assert st.settled_pnl_lifetime == 0.0
    import os
    assert os.path.exists(p + ".unreadable.bak"), "the bytes survive for a human"


def test_the_books_the_balance_audit_reads_carry_the_exit_bucket():
    """balance.books_from is what the 8-hourly audit compares against venue cash — it has to see all
    four numbers, and derive hedged with the same formula everything else uses."""
    from src.genz.maker_rt.balance import books_from
    st = MakerState()
    st.settled_pnl_lifetime, st.settled_pnl_untracked_lifetime = 25.3947, 8.3788
    st.settled_pnl_exits_lifetime = -6.99
    b = books_from(st)
    assert b["settled_pnl_lifetime"] == 25.3947
    assert b["settled_pnl_exits_lifetime"] == -6.99
    assert b["settled_pnl_hedged_lifetime"] == 24.0059
