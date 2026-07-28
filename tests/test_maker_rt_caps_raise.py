"""CAPS RAISE 2026-07-28 — the live panel showed slots pinned 8/8 with a 13-minute wait for a free
slot, i.e. SLOT STARVATION was binding, not edge availability. So quote/stake/slots went up:

    quote_usd_max      20 -> 70      max_open_quotes       8 -> 12  (reserve/direction 2 -> 3)
    max_daily_stake    300 -> 800    max_pair_stake_usd  100 -> 350

and the two SAFETY limits (max_daily_loss_usd 50, max_fills_per_day 20) did NOT move.

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
    assert live.quote_usd_max == 70.0
    assert live.max_daily_stake_usd == 800.0
    assert live.max_pair_stake_usd == 350.0
    assert live.max_open_quotes == 12


def test_safety_limits_did_not_move(live):
    """Bigger bets do NOT buy a bigger loss budget — this is the whole reason the raise is safe."""
    assert live.max_daily_loss_usd == 50.0
    assert live.max_fills_per_day == 20


def test_reserve_per_direction_scaled_with_the_slots(raw_yaml):
    """12 slots, 3 reserved per direction, 2 live directions -> 6 reserved, 6 floating."""
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


def test_pair_of_340_admits(live):
    ok, reason = _caps(live).can_place(340.0)
    assert ok is True and reason == "ok"


def test_pair_of_360_refuses(live):
    ok, reason = _caps(live).can_place(360.0)
    assert ok is False and reason == "max_pair_stake_usd"


def test_pair_cap_boundary_is_exactly_350(live):
    c = _caps(live)
    assert c.can_place(350.0)[0] is True            # exactly at the cap is allowed
    assert c.can_place(350.01)[0] is False


def test_oversized_pair_refuses_ONE_quote_and_does_not_halt_the_day(live):
    """A pair-cap breach is a SIZING limit, not a daily breach: the next in-size quote must still pass."""
    c = _caps(live)
    assert c.can_place(360.0)[0] is False
    assert c.halted is False
    assert c.can_place(340.0)[0] is True


def test_daily_stake_breach_still_halts(live):
    """The day cap keeps its teeth: 800 committed, then any further pair halts + trips the reason."""
    c = _caps(live)
    c.commit_stake(700.0)
    ok, reason = c.can_place(200.0)                  # 700 + 200 > 800
    assert ok is False and reason == "max_daily_stake_usd" and c.halted is True


# --------------------------------------------------------------------------- #
# 3. The raise actually reaches the bigger sizes (a $70 leg is reachable)        #
# --------------------------------------------------------------------------- #
def test_a_70_dollar_rest_leg_is_now_reachable(live):
    """0.35 price, deep books: the rest-leg notional cap binds at 70/0.35 = 200 shares. Under the OLD
    $20 cap this same quote was capped at 57."""
    p = plan_size(0.35, 0.63, quote_usd_max=live.quote_usd_max,
                  max_pair_stake_usd=live.max_pair_stake_usd,
                  daily_stake_headroom=live.max_daily_stake_usd,
                  hedge_depth=_BIG, book_depth=_BIG, venue_minimum=5)
    assert p["size"] == 200 and p["binding"] == "quote_usd_max"
    old = plan_size(0.35, 0.63, quote_usd_max=20.0, max_pair_stake_usd=100.0,
                    daily_stake_headroom=300.0, hedge_depth=_BIG, book_depth=_BIG, venue_minimum=5)
    assert old["size"] == 57


def test_pair_cap_still_binds_a_cheap_leg_with_an_outsized_hedge(live):
    """The TBTOR shape (cheap rest leg, expensive hedge) is exactly what the pair cap is for: at
    $0.06 rest + $0.95 hedge the pair notional — not the rest leg — is what limits the size."""
    p = plan_size(0.06, 0.95, quote_usd_max=live.quote_usd_max,
                  max_pair_stake_usd=live.max_pair_stake_usd,
                  daily_stake_headroom=live.max_daily_stake_usd,
                  hedge_depth=_BIG, book_depth=_BIG, venue_minimum=5)
    assert p["binding"] == "pair_cap"
    assert p["size"] == int(350.0 / (0.06 + 0.95))          # 346
    assert p["size"] * (0.06 + 0.95) <= live.max_pair_stake_usd + 1e-9


def test_pair_cap_of_100_would_have_strangled_the_new_quote_size(live):
    """Proves the directive's 'MUST scale' claim rather than asserting it: hold quote_usd_max at the
    new $70 but leave the pair cap at the old $100 and the pair cap, not the leg, binds."""
    strangled = plan_size(0.35, 0.63, quote_usd_max=70.0, max_pair_stake_usd=100.0,
                          daily_stake_headroom=800.0, hedge_depth=_BIG, book_depth=_BIG,
                          venue_minimum=5)
    assert strangled["binding"] == "pair_cap" and strangled["size"] == 102
    scaled = plan_size(0.35, 0.63, quote_usd_max=70.0, max_pair_stake_usd=350.0,
                       daily_stake_headroom=800.0, hedge_depth=_BIG, book_depth=_BIG, venue_minimum=5)
    assert scaled["binding"] == "quote_usd_max" and scaled["size"] == 200


# --------------------------------------------------------------------------- #
# 4. Slots: 12 open, 3 reserved per direction                                   #
# --------------------------------------------------------------------------- #
def test_twelve_slots_are_actually_available(live):
    c = _caps(live)
    for _ in range(12):
        assert c.can_place(10.0)[0] is True
        c.on_open()
    assert c.can_place(10.0) == (False, "max_open_quotes")


def test_neither_direction_can_eat_the_others_reserve(raw_yaml, live):
    """rest_kalshi may claim up to 9 of 12 (poly's 3 reserved stay protected), never the 10th."""
    dirs = ["rest_poly", "rest_kalshi"]
    reserve, max_open = raw_yaml["reserve_per_direction"], live.max_open_quotes
    assert direction_slot_ok("rest_kalshi", {"rest_kalshi": 8, "rest_poly": 0}, dirs, max_open, reserve)
    assert not direction_slot_ok("rest_kalshi", {"rest_kalshi": 9, "rest_poly": 0}, dirs, max_open, reserve)
    # once poly holds its reserve, kalshi's remaining headroom opens back up
    assert direction_slot_ok("rest_kalshi", {"rest_kalshi": 8, "rest_poly": 3}, dirs, max_open, reserve)
