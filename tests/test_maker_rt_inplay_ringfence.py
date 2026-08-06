"""THE IN-PLAY RING-FENCE (P3) + the venue-cash precondition, 2026-08-06.

Measured on 2026-08-06: in-play reached the executor on 0.69% of its evaluations and was refused on
100% of those, always on `daily_stake` — because ``max_open_quotes`` x the typical projected pair
equalled the ENTIRE daily budget, so pre-game's resting quotes reserved all of it and in-play was
sized at 0 shares forever. Raising the shared budget alone would not have fixed that: pre-game would
reserve the bigger number too.

So in-play now draws on its OWN pool, and answers to its OWN realized-loss sub-cap, because in-play
lifetime is -$6.69 over 12 fills and its p50 locked net is negative. The two properties that matter
are both NEGATIVE ones — neither pool may borrow from the other, and the experiment must fail cheaply
without touching the phase that actually earns.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.genz.maker_rt.caps import LiveCaps

from .test_maker_rt_pregame import (_Guard, _Hedger, _KalshiOC, _Poly, _State, _Store, _cand, _dec,
                                    _exec_kalshi)

_DT = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


def _cfg(**kw):
    base = dict(quote_usd_max=210.0, max_open_quotes=24, max_fills_per_day=40,
                max_daily_loss_usd=50.0, max_daily_stake_usd=2500.0, max_pair_stake_usd=1050.0,
                inplay_pool_usd=500.0, inplay_max_loss_usd=40.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _caps(**kw):
    c = LiveCaps(_cfg(**kw))
    c.roll("20260806")
    return c


# --------------------------------------------------------------------------- #
# 1. NEITHER POOL MAY BORROW THE OTHER'S                                       #
# --------------------------------------------------------------------------- #
def test_inplay_can_place_when_pregame_has_reserved_its_entire_ceiling():
    """THE HEADLINE PROPERTY. This is the exact 2026-08-06 state — pre-game holding the whole budget in
    reservations — and in-play must still be able to quote."""
    c = _caps()
    c.on_open(2500.0, "pre")                      # pre-game reserves every dollar it owns
    assert c.daily_stake_headroom("pre") == 0.0
    assert c.can_place(100.0, "pre")[0] is False

    assert c.daily_stake_headroom("inplay") == 500.0, "in-play's pool is untouched"
    ok, reason = c.can_place(400.0, "inplay")
    assert ok is True and reason == "ok"


def test_pregame_cannot_consume_the_inplay_pool():
    """The fence has to hold in BOTH directions or it is just a bigger shared budget."""
    c = _caps()
    c.on_open(500.0, "inplay")                    # in-play holds its whole pool
    assert c.daily_stake_headroom("inplay") == 0.0
    assert c.can_place(50.0, "inplay")[0] is False
    # pre-game still has all 2500 of its own — in-play's spend did not shrink it
    assert c.daily_stake_headroom("pre") == 2500.0
    assert c.can_place(1000.0, "pre")[0] is True       # an in-size pair still admits


def test_inplay_spending_does_not_shrink_pregame_headroom():
    c = _caps()
    c.commit_stake(450.0, "inplay")
    assert c.daily_stake_headroom("pre") == 2500.0
    assert c.daily_stake_headroom("inplay") == 50.0


def test_an_inplay_pool_breach_refuses_inplay_and_NEVER_halts_the_day():
    """Halting the day because the $500 experiment ran out of room would stop the proven earner to
    protect the unproven one. In-play is refused; pre-game carries on."""
    c = _caps()
    c.commit_stake(500.0, "inplay")
    ok, reason = c.can_place(100.0, "inplay")
    assert ok is False and reason == "inplay_pool_spent"
    assert c.halted is False, "an in-play pool breach must never day-halt"
    assert c.can_place(100.0, "pre")[0] is True, "pre-game is unaffected"


def test_a_pregame_pool_breach_still_halts_the_day_exactly_as_before():
    c = _caps()
    c.commit_stake(2400.0, "pre")
    ok, reason = c.can_place(200.0, "pre")
    assert ok is False and reason == "max_daily_stake_usd" and c.halted is True


def test_with_the_ringfence_off_both_phases_share_one_pool_as_before():
    """0.0 restores the pre-2026-08-06 behaviour exactly — the ring-fence is opt-in."""
    c = _caps(inplay_pool_usd=0.0)
    assert c.pool_for("inplay") == c.pool_for("pre") == 2500.0
    c.on_open(2500.0, "pre")
    assert c.can_place(10.0, "inplay")[0] is False, "no fence -> in-play sees pre-game's reservations"


# --------------------------------------------------------------------------- #
# 2. THE -$40 IN-PLAY LOSS SUB-CAP                                             #
# --------------------------------------------------------------------------- #
def test_the_subcap_halts_inplay_and_leaves_pregame_running():
    c = _caps()
    c.on_fill(-25.0, locked=False, phase="inplay")
    assert c.inplay_budget_halted is False and c.can_place(50.0, "inplay")[0] is True

    c.on_fill(-16.0, locked=False, phase="inplay")            # -41 total
    assert c.inplay_budget_halted is True
    assert c.can_place(50.0, "inplay") == (False, "inplay_daily_loss")
    assert c.halted is False, "the SHARED day-halt must not trip on the in-play sub-cap"
    assert c.can_place(50.0, "pre")[0] is True, "pre-game keeps running"


def test_the_subcap_counts_UNWIND_TOLLS_not_just_pairs():
    """The tolls are what actually lost in-play its money (-$2.20, -$4.75, -$0.96), and they arrive via
    adjust_pnl, not on_fill. A sub-cap blind to them would never fire."""
    c = _caps()
    c.adjust_pnl(-41.0, "inplay")
    assert c.inplay_budget_halted is True
    assert c.can_place(10.0, "inplay")[0] is False


def test_pregame_losses_never_trip_the_inplay_subcap():
    c = _caps()
    c.on_fill(-45.0, locked=False, phase="pre")
    assert c.inplay_budget_halted is False
    assert c.can_place(10.0, "inplay")[0] is True


def test_the_shared_50_dollar_brake_still_binds_across_BOTH_phases():
    """The sub-cap is an ADDITIONAL rail on the unproven phase, never a replacement for the brake."""
    c = _caps()
    c.on_fill(-30.0, locked=False, phase="pre")
    c.on_fill(-21.0, locked=False, phase="inplay")            # -51 across the day
    ok, reason = c.can_place(10.0, "pre")
    # ``on_fill`` latches the halt as soon as the brake breaches, so can_place reports it as already
    # halted. Either spelling is the same event; what matters is that BOTH phases are stopped.
    assert ok is False and c.halted is True
    assert "max_daily_loss_usd" in reason
    assert c.can_place(10.0, "inplay")[0] is False


def test_the_shared_fill_cap_binds_across_both_phases():
    c = _caps(max_fills_per_day=3)
    c.on_fill(0.0, phase="pre")
    c.on_fill(0.0, phase="inplay")
    c.on_fill(0.0, phase="pre")
    assert c.can_place(10.0, "inplay") == (False, "max_fills_per_day")
    assert c.can_place(10.0, "pre") == (False, "max_fills_per_day")


def test_inplay_loss_left_reports_the_remaining_rope():
    c = _caps()
    assert c.inplay_loss_left() == 40.0
    c.adjust_pnl(-15.0, "inplay")
    assert c.inplay_loss_left() == pytest.approx(25.0)


def test_the_subcap_resets_with_the_utc_day():
    c = _caps()
    c.adjust_pnl(-41.0, "inplay")
    assert c.inplay_budget_halted is True
    c.roll("20260807")
    assert c.inplay_budget_halted is False and c.pnl_by_phase["inplay"] == 0.0
    assert c.can_place(10.0, "inplay")[0] is True


def test_a_disabled_subcap_never_halts():
    c = _caps(inplay_max_loss_usd=0.0)
    c.adjust_pnl(-500.0, "inplay")
    assert c.inplay_budget_halted is False
    assert c.inplay_loss_left() == float("inf")


# --------------------------------------------------------------------------- #
# 3. THE EXECUTOR HONOURS BOTH (enforcement, not just accounting)               #
# --------------------------------------------------------------------------- #
def _exec(tmp_path):
    poly = _Poly()
    hedger = _Hedger(SimpleNamespace(status="locked", hedged_shares=5, hedge_avg_price=0.50,
                                     hedge_fee=0.01, locked_pnl=0.11, unwind_cost=None, detail={}),
                     poly=poly)
    ex, _ = _exec_kalshi(tmp_path, kalshi_oc=_KalshiOC(), poly=poly, hedger=hedger, state=_State())
    ex.in_flight = _Guard()
    ex.caps.inplay_pool_usd = 500.0
    ex.caps.inplay_max_loss_usd = 40.0
    # PRODUCTION's shared brake. The fixture config defaults to $25, which -$41 of in-play loss would
    # breach FIRST — masking the very thing these tests exist to show (in-play stopping alone).
    ex.caps.max_daily_loss_usd = 50.0
    ex.roll_day(_DT)
    return ex, poly


def test_eligible_refuses_inplay_once_the_subcap_latches(tmp_path):
    """Enforcement reads the caps latch DIRECTLY, so it cannot depend on a notifier being called."""
    ex, _poly = _exec(tmp_path)
    c = _cand(direction="rest-poly")
    assert ex.eligible(c, "inplay", 10.0) is True
    ex.caps.adjust_pnl(-41.0, "inplay")
    assert ex.eligible(c, "inplay", 10.0) is False, "in-play is stopped"
    assert ex.eligible(c, "pre", 10.0) is True, "pre-game is untouched"


def test_the_subcap_alert_fires_once_and_stops_inplay(tmp_path):
    sent = []
    ex, _poly = _exec(tmp_path)
    ex.telegram = sent.append
    ex.caps.adjust_pnl(-41.0, "inplay")
    ex._check_inplay_budget(_DT)
    ex._check_inplay_budget(_DT)                              # idempotent
    assert ex.inplay_halted is True
    msgs = [m for m in sent if "in-play" in m and "STOPPED" in m]
    assert len(msgs) == 1, "one alert, not one per call"
    assert "$40" in msgs[0] and "Pre-game" in msgs[0], "names the number and says pre-game continues"


def test_an_inplay_quote_sizes_against_the_inplay_pool(tmp_path):
    """The sizing call must read the phase's OWN headroom — this is the line that, if it read the
    global pool, would silently re-merge the two budgets."""
    ex, _poly = _exec(tmp_path)
    ex.caps.on_open(2500.0, "pre")                            # pre-game reserves everything it has
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    ex.place_or_reprice(_cand(), _dec(price=0.46), None, store, _DT, 1.0, "inplay")
    assert len(ex.order_client.rests) == 1, "in-play still places out of its own pool"


def test_pregame_is_refused_when_ONLY_the_inplay_pool_has_room(tmp_path):
    ex, _poly = _exec(tmp_path)
    ex.caps.commit_stake(2500.0, "pre")
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    ex.place_or_reprice(_cand(), _dec(price=0.46), None, store, _DT, 1.0, "pre")
    assert ex.order_client.rests == [], "pre-game may not dip into in-play's pool"


def test_the_reservation_is_released_into_the_pool_it_came_from(tmp_path):
    """A hold taken from in-play's pool must be released back to IT — released to the wrong pool, the
    fence leaks one quote at a time."""
    ex, _poly = _exec(tmp_path)
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    ex.place_or_reprice(_cand(), _dec(price=0.46), None, store, _DT, 1.0, "inplay")
    assert ex.caps.reserved_by_phase["inplay"] > 0.0
    assert ex.caps.reserved_by_phase["pre"] == 0.0
    ex.cancel_key(_cand().key, _DT, "test")
    assert ex.caps.reserved_by_phase["inplay"] == pytest.approx(0.0)
    assert ex.caps.reserved_by_phase["pre"] == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# 4. VENUE CASH (T1) — budget headroom is not money in the bank                 #
# --------------------------------------------------------------------------- #
def test_venue_cash_refuses_when_a_bank_cannot_fund_its_leg(tmp_path):
    ex, _poly = _exec(tmp_path)
    ex._note_venue_cash({"polymarket": 5.0, "kalshi": 10000.0})
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    ex.place_or_reprice(_cand(), _dec(price=0.46), None, store, _DT, 1.0, "pre")
    assert ex.order_client.rests == [], "the rest venue cannot fund the leg"
    assert ex.state.funnel_calls, "the refusal is counted"
    assert any(s == "cap_refused:venue_cash" for _sp, _ph, s in ex.state.funnel_calls)
    assert ex._binding_counts.get("venue_cash") == 1


def test_venue_cash_checks_the_HEDGE_bank_too(tmp_path):
    """A rest-poly pair hedges on Kalshi — an empty Kalshi is what turns a fill into a naked leg."""
    ex, _poly = _exec(tmp_path)
    ex._note_venue_cash({"polymarket": 10000.0, "kalshi": 1.0})
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    ex.place_or_reprice(_cand(), _dec(price=0.46), None, store, _DT, 1.0, "pre")
    assert ex.order_client.rests == [], "the hedge bank cannot fund the hedge"


def test_venue_cash_admits_when_both_banks_are_funded(tmp_path):
    ex, _poly = _exec(tmp_path)
    ex._note_venue_cash({"polymarket": 10000.0, "kalshi": 10000.0})
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    ex.place_or_reprice(_cand(), _dec(price=0.46), None, store, _DT, 1.0, "pre")
    assert len(ex.order_client.rests) == 1


def test_an_UNREADABLE_balance_is_not_a_refusal(tmp_path):
    """Refusing every quote because a balance endpoint blipped would be a self-inflicted outage. The
    stake pools are still in force underneath."""
    ex, _poly = _exec(tmp_path)
    assert ex._venue_cash == {"kalshi": None, "polymarket": None}
    store = _Store(poly_best_ask=0.60, kalshi_ask=0.50)
    ex.place_or_reprice(_cand(), _dec(price=0.46), None, store, _DT, 1.0, "pre")
    assert len(ex.order_client.rests) == 1, "unknown cash must not block trading"


def test_low_venue_cash_alerts_once_per_hour(tmp_path):
    sent = []
    ex, _poly = _exec(tmp_path)
    ex.telegram = sent.append
    ex._venue_cash_ts = 10_000.0
    ex._note_venue_cash({"polymarket": 5.0, "kalshi": 10000.0}, None)
    ex._note_venue_cash({"polymarket": 5.0, "kalshi": 10000.0}, None)
    low = [m for m in sent if "Polymarket" in m and "less than one full-size bet" in m]
    assert len(low) == 1, "throttled to one scream per venue per hour"


# --------------------------------------------------------------------------- #
# 5. THE UNTOUCHED RAILS STILL REFUSE EXACTLY WHAT THEY REFUSED (P2 is OFF)     #
# --------------------------------------------------------------------------- #
def test_the_inplay_rails_were_not_relaxed(live_cfg=None):
    """P2 was explicitly NOT done: the rails are the selection mechanism and in-play's p50 is negative.
    Read the SHIPPED config so a future edit cannot quietly loosen one."""
    import os

    import yaml
    with open(os.path.join(os.path.dirname(__file__), "..", "config.yaml"), encoding="utf-8") as fh:
        blk = yaml.safe_load(fh)["maker_rt"]
    ip, live_ip = blk["inplay"], blk["live_inplay"]
    assert ip["shock_move"] == 0.05 and ip["shock_window_s"] == 10 and ip["freeze_s"] == 30
    assert ip["persist_ms"] == 1500
    assert ip["conn_fresh_s"] == 30 and ip["node_quiet_max_s"] == 180 and ip["stale_grace_s"] == 5
    assert live_ip["freeze_cooloff_s"] == 10
    assert live_ip["first_fill_pause_s"] == 120
    assert live_ip["halt_locked_net"] == -0.020
    assert live_ip["hedge_timeout_ms"] == 1500
    # and the shared edge/floor knobs the directive also froze
    assert blk["target_net"] == 0.006
    assert blk["max_plausible_edge_pct"] == 5.0
    assert blk["hedge_execution_floor"] == -0.010
