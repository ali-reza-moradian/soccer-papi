"""CAPS RAISES — the shipped values, and the arithmetic that makes each raise coherent.

2026-07-28 (slot starvation: the panel showed slots pinned 8/8 with a 13-minute wait for a free slot,
so it was slots that were binding, not edge availability):

    quote_usd_max      20 -> 70      max_open_quotes       8 -> 12  (reserve/direction 2 -> 3)
    max_daily_stake    300 -> 800    max_pair_stake_usd  100 -> 350

2026-08-04 — a measured ~+50% step, AUTHORIZED BY THE GATE: --gates read soccer EDGE-POSITIVE on 25
clean hedged fills since the 2026-07-30 restatement (mean +0.859%, p50 +0.700%, worst -0.000%). mlb,
tennis and ufc were all still MEASURING and got nothing.

    quote_usd_max      70 -> 105     max_daily_stake     800 -> 1200
    max_pair_stake_usd 350 -> 525    auto_flatten_max     120 -> 180

2026-08-06 — an owner-directed DOUBLING, gate-authorized on 28 clean soccer fills (mean +0.828%,
p50 +0.707%). It also SPLIT the daily budget: pre-game keeps max_daily_stake_usd and in-play gets a
ring-fenced pool of its own that neither phase can borrow from.

    quote_usd_max     105 -> 210     max_daily_stake    1200 -> 2500  (+ in-play pool 500)
    max_pair_stake_usd 525 -> 1050   auto_flatten_max    180 -> 360
    max_open_quotes    12 -> 24      max_fills_per_day     20 -> 40
    max_open_per_game   3 -> 4       (NOT 6: 6 x $1050 = $6,300 on one match, and one goal moves
                                      every correlated line together — audit N15)

and in ALL THREE raises the one limit that bounds a bad day, max_daily_loss_usd $50, did NOT move.

The per-pair cap is the one that MUST scale with quote_usd_max: it bounds rest + worst-case hedge for
ONE bet, and a cheap rest leg's hedge can be many multiples of the leg (TBTOR: $1.02 rest + $15.81
hedge). Left at 100 it would have refused nearly every quote the bigger rest leg exists to enable.
These tests read the SHIPPED config, not literals, so editing config.yaml alone can never silently
drop a cap back.
"""
from __future__ import annotations

import os

import pytest
import yaml

from src.genz.maker_rt.caps import LiveCaps, direction_slot_ok, plan_size
from src.genz.maker_rt.config import load_maker_rt_config

_BIG = 10 ** 9        # a depth that never binds
CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")


@pytest.fixture(scope="module")
def live():
    return load_maker_rt_config().live


@pytest.fixture(scope="module")
def raw_yaml():
    with open(CONFIG, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["maker_rt"]


# --------------------------------------------------------------------------- #
# 1. The shipped values                                                         #
# --------------------------------------------------------------------------- #
def test_shipped_caps_are_the_raised_values(live):
    assert live.quote_usd_max == 210.0
    assert live.max_daily_stake_usd == 2500.0
    assert live.max_pair_stake_usd == 1050.0
    assert live.max_open_quotes == 24
    assert live.max_open_per_game == 4, "4, not 6 — see N15 concentration"
    assert live.inplay_pool_usd == 500.0, "in-play's ring-fenced pool"
    assert live.inplay_max_loss_usd == 40.0, "in-play's own daily loss sub-cap"


def test_the_pair_cap_scaled_WITH_the_quote_cap(live):
    """The raise is INERT unless this holds. A cheap rest leg's hedge is many multiples of the leg, so a
    quote cap that grows while the pair cap stands still just moves the binding constraint to the pair
    cap and nothing actually gets bigger. Both raises kept the same 1:5 shape (70:350, 105:525)."""
    assert live.max_pair_stake_usd == pytest.approx(live.quote_usd_max * 5.0)


def test_the_auto_flatten_ceiling_was_recomputed_from_its_own_rule(live):
    """Its own comment defines it: "covers a full-size pair (quote_usd_max + slippage) without covering
    a runaway" — 120 against a $70 leg, i.e. 1.714x. Left at 120 against a $105 leg it would cover only
    1.14x, and the bounded-flatten path would start halting on ordinary full-size positions: a cap raise
    quietly making the bot MORE fragile."""
    assert live.auto_flatten_max_usd == 360.0
    assert live.auto_flatten_max_usd / live.quote_usd_max == pytest.approx(120.0 / 70.0, rel=1e-3)
    assert live.auto_flatten_max_usd < live.max_pair_stake_usd, "still not a runaway"


def test_safety_limits_did_not_move(live):
    """THE one limit that bounds a bad day did not move, through all three raises: a bad day still halts
    at exactly $50. That is the whole reason a size raise can be safe — the downside is pinned while the
    upside scales.

    max_fills_per_day and max_open_quotes DID move (20 -> 40, 12 -> 24): they are throughput, not
    exposure, and each fill is still bounded by the pair cap and the day by the $50 brake."""
    assert live.max_daily_loss_usd == 50.0, "the brake never moves"
    assert live.max_fills_per_day == 40
    assert live.max_open_quotes == 24
    # Concentration moved 3 -> 4, deliberately NOT to 6. At $1,050/pair, 6 slots on one match is $6,300
    # against a ~$7,350 bankroll, and ONE goal moves every correlated line on that match together (N15).
    assert live.max_open_per_game == 4
    assert live.max_open_per_game * live.max_pair_stake_usd == 4200.0
    assert live.max_open_per_game < 6, "6 was explicitly refused — see N15 and the config comment"


def test_reserve_per_direction_scaled_with_the_slots(raw_yaml):
    """24 slots, 3 reserved per direction, 2 live directions -> 6 reserved, 18 floating."""
    assert raw_yaml["reserve_per_direction"] == 3
    assert len(raw_yaml["directions"]) == 2
    assert raw_yaml["reserve_per_direction"] * len(raw_yaml["directions"]) < raw_yaml["live"]["max_open_quotes"]


# --------------------------------------------------------------------------- #
# 2. THE DIRECTIVE'S ACCEPTANCE TEST — a ~$340 pair admits, ~$360 refuses        #
# --------------------------------------------------------------------------- #
def _caps(live):
    c = LiveCaps(live)
    c.roll("2026-07-28")
    return c


def test_a_pair_just_under_the_cap_admits(live):
    ok, reason = _caps(live).can_place(live.max_pair_stake_usd - 10.0)
    assert ok is True and reason == "ok"


def test_a_pair_just_over_the_cap_refuses(live):
    ok, reason = _caps(live).can_place(live.max_pair_stake_usd + 10.0)
    assert ok is False and reason == "max_pair_stake_usd"


def test_the_pair_cap_boundary_is_exact(live):
    c = _caps(live)
    assert c.can_place(1050.0)[0] is True           # exactly at the cap is allowed
    assert c.can_place(1050.01)[0] is False


def test_oversized_pair_refuses_ONE_quote_and_does_not_halt_the_day(live):
    """A pair-cap breach is a SIZING limit, not a daily breach: the next in-size quote must still pass."""
    c = _caps(live)
    assert c.can_place(live.max_pair_stake_usd + 10.0)[0] is False
    assert c.halted is False
    assert c.can_place(live.max_pair_stake_usd - 10.0)[0] is True


def test_daily_stake_breach_still_halts(live):
    """The day cap keeps its teeth: SPENT stake past the cap halts the day and trips the reason."""
    c = _caps(live)
    c.commit_stake(live.max_daily_stake_usd - 100.0)       # 2,400 spent (pre-game pool)
    ok, reason = c.can_place(200.0)                        # 2,400 + 200 > 2,500
    assert ok is False and reason == "max_daily_stake_usd" and c.halted is True


# --------------------------------------------------------------------------- #
# 3. The raise actually reaches the bigger sizes (a $70 leg is reachable)        #
# --------------------------------------------------------------------------- #
def test_a_full_size_rest_leg_is_now_reachable(live):
    """0.35 price, deep books: the rest-leg notional cap binds at 210/0.35 = 600 shares. The same quote
    was capped at 300 under the $105 cap, 200 under $70 and 57 under the original $20 — the raise is
    visible in the SIZE, which is the only place a raise is allowed to be visible."""
    p = plan_size(0.35, 0.63, quote_usd_max=live.quote_usd_max,
                  max_pair_stake_usd=live.max_pair_stake_usd,
                  daily_stake_headroom=live.max_daily_stake_usd,
                  hedge_depth=_BIG, book_depth=_BIG, venue_minimum=5)
    assert p["size"] == 600 and p["binding"] == "quote_usd_max"
    for q, pair, daily, expect in ((105.0, 525.0, 1200.0, 300), (70.0, 350.0, 800.0, 200),
                                   (20.0, 100.0, 300.0, 57)):
        prev = plan_size(0.35, 0.63, quote_usd_max=q, max_pair_stake_usd=pair,
                         daily_stake_headroom=daily, hedge_depth=_BIG, book_depth=_BIG,
                         venue_minimum=5)
        assert prev["size"] == expect


def test_pair_cap_still_binds_a_cheap_leg_with_an_outsized_hedge(live):
    """The TBTOR shape (cheap rest leg, expensive hedge) is exactly what the pair cap is for: at
    $0.06 rest + $0.95 hedge the pair notional — not the rest leg — is what limits the size."""
    p = plan_size(0.06, 0.95, quote_usd_max=live.quote_usd_max,
                  max_pair_stake_usd=live.max_pair_stake_usd,
                  daily_stake_headroom=live.max_daily_stake_usd,
                  hedge_depth=_BIG, book_depth=_BIG, venue_minimum=5)
    assert p["binding"] == "pair_cap"
    assert p["size"] == int(live.max_pair_stake_usd / (0.06 + 0.95))       # 1039 at the $1050 cap
    assert p["size"] * (0.06 + 0.95) <= live.max_pair_stake_usd + 1e-9


def test_an_unscaled_pair_cap_would_have_made_the_raise_INERT(live):
    """Proves the "MUST scale" claim rather than asserting it, for BOTH raises: hold the new quote cap
    but leave the pair cap behind, and the pair cap — not the leg — binds, so nothing gets bigger."""
    strangled = plan_size(0.35, 0.63, quote_usd_max=70.0, max_pair_stake_usd=100.0,
                          daily_stake_headroom=800.0, hedge_depth=_BIG, book_depth=_BIG,
                          venue_minimum=5)
    assert strangled["binding"] == "pair_cap" and strangled["size"] == 102
    scaled = plan_size(0.35, 0.63, quote_usd_max=70.0, max_pair_stake_usd=350.0,
                       daily_stake_headroom=800.0, hedge_depth=_BIG, book_depth=_BIG, venue_minimum=5)
    assert scaled["binding"] == "quote_usd_max" and scaled["size"] == 200
    # 2026-08-04: a $105 leg against the OLD $350 pair cap gives back strictly less size at the
    # cheap-rest shape, which is exactly where the bigger leg was supposed to help.
    inert = plan_size(0.06, 0.95, quote_usd_max=105.0, max_pair_stake_usd=350.0,
                      daily_stake_headroom=1200.0, hedge_depth=_BIG, book_depth=_BIG, venue_minimum=5)
    full = plan_size(0.06, 0.95, quote_usd_max=105.0, max_pair_stake_usd=525.0,
                     daily_stake_headroom=1200.0, hedge_depth=_BIG, book_depth=_BIG, venue_minimum=5)
    assert inert["binding"] == full["binding"] == "pair_cap"
    assert inert["size"] == 346 and full["size"] == 519


# --------------------------------------------------------------------------- #
# 4. Slots: 24 open, 3 reserved per direction                                   #
# --------------------------------------------------------------------------- #
def test_twelve_slots_are_actually_available(live):
    c = _caps(live)
    for _ in range(24):
        assert c.can_place(10.0)[0] is True
        c.on_open()
    assert c.can_place(10.0) == (False, "max_open_quotes")


def test_neither_direction_can_eat_the_others_reserve(raw_yaml, live):
    """rest_kalshi may claim up to max_open - reserve (poly's reserve stays protected), never one more."""
    dirs = ["rest_poly", "rest_kalshi"]
    reserve, max_open = raw_yaml["reserve_per_direction"], live.max_open_quotes
    free = max_open - reserve                      # 24 - 3 = 21 claimable while poly holds none
    assert direction_slot_ok("rest_kalshi", {"rest_kalshi": free - 1, "rest_poly": 0},
                             dirs, max_open, reserve)
    assert not direction_slot_ok("rest_kalshi", {"rest_kalshi": free, "rest_poly": 0},
                                 dirs, max_open, reserve)
    # once poly holds its reserve, kalshi's remaining headroom opens back up
    assert direction_slot_ok("rest_kalshi", {"rest_kalshi": free - 1, "rest_poly": reserve},
                             dirs, max_open, reserve)
