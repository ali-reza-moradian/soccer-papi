"""The rails that the 2026-07-29 F14 incident proved were not load-bearing.

The price-space bug was one defect; what made it a $330 fiction rather than a logged oddity is that
every rail downstream either trusted the number or quietly reset itself:

  N6  ``on_fill`` is the ONLY feeder of the daily-loss rail and accepted ANY pnl. +$320.05 on a $14.12
      rest leg went straight in, so the -$50 rail then needed a REAL -$380 to trip.
  N2  the UTC day roll cleared ``halted`` unconditionally, so a latched safety halt expired at midnight
      with the position still unverified (twice in production: TBTOR 07-23, ZAYRZE 07-25).
  N12 the startup stray-order sweep ran only when ARMED, so a process that came up in SHADOW abandoned
      whatever its predecessor had resting — six Kalshi quotes filled unhedged on 2026-07-29 after the
      13:30:07Z shadow restart and settled to -$38.08.
  N8  a settlement REFUSED by the sanity rails was dropped entirely, losing the event as well as the
      number (Fortaleza's real $353 settlement, refused twice, unbooked).
"""
from __future__ import annotations

import json

import pytest

from src.genz.maker_rt.caps import LiveCaps
from src.genz.maker_rt.settle import SettledPnlReconciler


class _Block:
    quote_usd_max = 70.0
    max_pair_stake_usd = 350.0
    max_daily_stake_usd = 800.0
    max_open_quotes = 12
    max_fills_per_day = 20
    max_daily_loss_usd = 50.0


def _caps():
    return LiveCaps(_Block())


# --------------------------------------------------------------------------- #
# N6 — a rail fed by an unbounded number is not a rail                          #
# --------------------------------------------------------------------------- #
def test_the_fortaleza_booking_can_no_longer_disable_the_loss_rail():
    """THE incident number: +$320.05. The bound is what a hedged pair can PRODUCE — the 5% sanity
    ceiling on a $350 pair, times headroom = $52.50 — not what it can stake ($350). Bounding by stake
    would have let this straight through, which is the whole lesson."""
    c = _caps()
    assert c.fill_pnl_bound() == pytest.approx(52.5)
    c.on_fill(320.05)
    assert c.pnl_today == 0.0, "an implausible pnl must not re-base the daily loss budget"
    assert c.halted and c.halt_reason == "implausible_fill_pnl"
    # and with the rail intact, a real loss still trips at the real threshold
    c2 = _caps()
    for _ in range(5):
        c2.on_fill(-10.5)
    assert c2.halted and c2.halt_reason == "max_daily_loss_usd"


def test_implausible_pnl_is_refused_in_both_directions():
    """A phantom LOSS false-halts the day just as surely as a phantom gain disables the rail."""
    c = _caps()
    c.on_fill(-900.0)
    assert c.pnl_today == 0.0 and c.halt_reason == "implausible_fill_pnl"


def test_plausible_pnl_still_books_normally():
    c = _caps()
    for pnl in (2.36, -0.22, 2.15, 0.83):        # the real venue-truth numbers from that session
        c.on_fill(pnl)
    assert c.pnl_today == pytest.approx(5.12)
    assert not c.halted and c.fills_today == 4


def test_an_honest_unwind_loss_is_not_refused_by_the_locked_pair_bound():
    """A realized unwind/orphan outcome is bounded by the money COMMITTED, not by the edge ceiling —
    a naked leg can honestly lose most of its stake. Applying the tight bound here would refuse real
    losses, which is the same failure mode (a rail that lies) with the sign flipped."""
    c = _caps()
    assert c.fill_pnl_bound(locked=False) == pytest.approx(700.0)
    c.on_fill(-120.0, locked=False)
    assert c.pnl_today == pytest.approx(-120.0)
    assert c.halted and c.halt_reason == "max_daily_loss_usd"      # tripped the REAL rail, correctly
    # ...but a number no position could produce is still refused on that path too
    c3 = _caps()
    c3.on_fill(-5000.0, locked=False)
    assert c3.pnl_today == 0.0 and c3.halt_reason == "implausible_fill_pnl"


def test_every_fill_is_counted_even_when_its_pnl_is_refused():
    """The FILL happened — only its money is in doubt. Losing the count would also lose the fills cap."""
    c = _caps()
    c.on_fill(320.05)
    assert c.fills_today == 1


# --------------------------------------------------------------------------- #
# N2 — midnight is a budget reset, not an absolution                            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reason", ["orphan_position", "booking_quarantine"])
def test_the_day_roll_does_not_clear_a_safety_halt(reason):
    c = _caps()
    c.roll("20260729")
    c.stake_today, c.fills_today, c.pnl_today = 739.96, 9, -12.0
    c.halted, c.halt_reason = True, reason
    c.roll("20260730")
    assert c.stake_today == 0.0 and c.fills_today == 0 and c.pnl_today == 0.0, "budgets DO reset"
    assert c.halted and c.halt_reason == reason, "an unresolved position does not resolve at midnight"
    assert c.can_place(10.0)[0] is False


@pytest.mark.parametrize("reason", ["max_daily_loss_usd", "max_daily_stake_usd", "implausible_fill_pnl"])
def test_the_day_roll_still_clears_a_daily_budget_halt(reason):
    """The reset must keep working for the halts that genuinely are per-day, or the bot never resumes."""
    c = _caps()
    c.roll("20260729")
    c.halted, c.halt_reason = True, reason
    c.roll("20260730")
    assert not c.halted and c.halt_reason is None
    assert c.can_place(10.0)[0] is True


def test_sticky_halts_are_exactly_the_manual_ones():
    # 'unreadable_state' joins the two latches for the same reason they are latched: a state file we
    # cannot parse is still unparseable tomorrow, and it is the file that names the positions to watch.
    assert set(LiveCaps.STICKY_HALTS) == {"orphan_position", "booking_quarantine", "unreadable_state"}


# --------------------------------------------------------------------------- #
# N8 — a refused settlement is queued, never dropped                            #
# --------------------------------------------------------------------------- #
def _forbot_row():
    """Fortaleza as the corrupted books saw it: a real +$2.36 pair whose cost basis had been wrecked by
    the price-space bug, so it presented as +1011% ROI and was refused."""
    return {"game": "26JUL28FORBOT", "market_key": "total_goals|3.5",
            "realized_pnl_usd": 321.23, "settled_cost_usd": 31.77,
            "reason": "KXBRASILEIROBTOTAL-26JUL28FORBOT-4 SETTLED"}


def test_a_refused_settlement_is_persisted_for_manual_reconciliation(tmp_path):
    path = tmp_path / "refused.json"
    rec = SettledPnlReconciler(max_pair_stake_usd=350.0)
    rec.refused_path = str(path)
    rec._queue_refused(("26JUL28FORBOT", "total_goals|3.5"), "|ROI| 1011.1% > 50% ceiling",
                       _forbot_row(), "2026-07-29T02:39:14Z")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert len(saved) == 1
    assert saved[0]["game"] == "26JUL28FORBOT"
    assert saved[0]["refused_reason"].startswith("|ROI|")
    assert saved[0]["realized_pnl_usd"] == pytest.approx(321.23)


def test_a_refused_settlement_does_not_mark_the_key_settled(tmp_path):
    """It must stay reconcilable: once the cost basis is repaired, a later pass should be able to book
    it properly. Marking it settled would make the repair unbookable."""
    rec = SettledPnlReconciler(max_pair_stake_usd=350.0)
    rec.refused_path = str(tmp_path / "refused.json")
    rec._queue_refused(("26JUL28FORBOT", "total_goals|3.5"), "why", _forbot_row(), "now")
    assert not rec.already_settled("26JUL28FORBOT", "total_goals|3.5")


def test_the_refused_queue_accumulates_and_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "refused.json"
    path.write_text("{ not json", encoding="utf-8")     # a corrupt queue must not swallow the new entry
    rec = SettledPnlReconciler(max_pair_stake_usd=350.0)
    rec.refused_path = str(path)
    rec._queue_refused(("A", "k"), "why", _forbot_row(), "t1")
    rec._queue_refused(("B", "k"), "why", _forbot_row(), "t2")
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 2


def test_queueing_never_raises_even_with_an_unwritable_path():
    rec = SettledPnlReconciler(max_pair_stake_usd=350.0)
    rec.refused_path = "\x00:/nope/refused.json"
    rec._queue_refused(("A", "k"), "why", _forbot_row(), "t")     # must not raise
    assert len(rec._refused) == 1
