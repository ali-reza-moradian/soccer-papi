"""THE KALSHI MAKER FEE, MEASURED — not assumed from a published schedule.

``kalshi_maker_fee_series`` sat empty for the life of this bot with a comment calling it a "verified
list", and nothing had ever verified it. So we read our own fill history (GET /portfolio/fills,
2026-07-22 .. 2026-08-04, 91 fills, 46 of them MAKER) and looked at what the venue actually charged.

Two facts came out, and they are different facts:

  1. WHAT the fee is, where it is charged:  ceil(0.0175 · C · p · (1-p), 4dp) — exactly a QUARTER of the
     0.07 taker rate over the same base. All 8 fee-charging maker fills match to the cent.
  2. WHICH series charge it: KXMLBGAME, KXATPMATCH and KXWTAMATCH did; every soccer and UFC series did
     NOT — 38 maker fills charged exactly $0.00, including a 353-share fill and a 179-share fill, which
     is far too much size for a zero to be a rounding artefact.

The rows below are VERBATIM from those payloads (``count_fp``, ``yes_price_dollars``, ``fee_cost``,
``is_taker``). They are the regression: if Kalshi changes its schedule, this test is what notices.
"""
from __future__ import annotations

import math

from src.genz.maker_rt import config as mrt_config
from src.genz.maker_rt.books import SideView
from src.genz.maker_rt.quotes import (KALSHI_MAKER_FEE_RATE, compute_floor, compute_quote,
                                      rest_maker_fee)

# (ticker series, count, yes_price, fee_cost) — real maker fills that WERE charged
CHARGED = [
    ("KXMLBGAME", 17.00, 0.06, 0.016800),
    ("KXWTAMATCH", 54.00, 0.37, 0.220300),
    ("KXWTAMATCH", 25.00, 0.79, 0.072600),
    ("KXATPMATCH", 20.00, 0.98, 0.006900),
    ("KXMLBGAME", 2.05, 0.98, 0.000800),
    ("KXMLBGAME", 17.95, 0.98, 0.006200),
    ("KXMLBGAME", 20.00, 0.99, 0.003500),
    ("KXMLBGAME", 21.00, 0.94, 0.020800),
]

# ...and real maker fills that were charged NOTHING, on the series this bot actually rests on.
FREE = [
    ("KXCLUBFTOTAL", 353.00, 0.96), ("KXMLSTEAMTOTAL", 179.00, 0.61),
    ("KXDIMAYORTOTAL", 116.72, 0.16), ("KXUECLTOTAL", 114.49, 0.22),
    ("KXCLUBFTOTAL", 112.00, 0.38), ("KXUFCFIGHT", 28.00, 0.70),
    ("KXARGPREMDIVTOTAL", 72.00, 0.03), ("KXEKSTRAKLASATOTAL", 16.05, 0.93),
    ("KXLIGAMXTOTAL", 47.20, 0.08), ("KXUCLTOTAL", 61.00, 0.21),
]


def _venue_fee(count, price, rate):
    """What the venue charges for a whole fill: the per-share rate x count, rounded UP to 4dp."""
    return math.ceil(rest_maker_fee(price, rate) * count * 1e4) / 1e4


# --------------------------------------------------------------------------- #
# 1. the coefficient                                                            #
# --------------------------------------------------------------------------- #
def test_the_measured_coefficient_reproduces_every_charged_maker_fill():
    for series, count, price, fee in CHARGED:
        assert abs(_venue_fee(count, price, KALSHI_MAKER_FEE_RATE) - fee) < 5e-5, (series, count, price)


def test_the_maker_rate_is_a_quarter_of_the_taker_rate():
    """Stated because it is the memorable form of the same number, and because a future reader will
    otherwise wonder where 0.0175 came from."""
    assert KALSHI_MAKER_FEE_RATE == 0.07 * 0.25


def test_a_flat_per_contract_guess_would_misprice_both_tails():
    """The fee has the p·(1−p) shape, so it is ~28x larger at 50c than at 2c per share. Guessing a flat
    rate — the obvious wrong model — misses the 54-share WTA fill by an order of magnitude."""
    flat = 0.0025                                            # a plausible flat per-contract guess
    _s, count, price, fee = CHARGED[1]                       # 54 sh @ 0.37 -> $0.2203
    assert abs(math.ceil(flat * count * 1e4) / 1e4 - fee) > 0.05
    assert abs(_venue_fee(count, price, KALSHI_MAKER_FEE_RATE) - fee) < 5e-5


# --------------------------------------------------------------------------- #
# 2. which series pay it                                                        #
# --------------------------------------------------------------------------- #
def test_the_configured_map_matches_what_the_venue_charged():
    cfg = mrt_config.load_maker_rt_config(overrides=None)
    assert set(cfg.kalshi_maker_fee_rates) == {"KXMLBGAME", "KXATPMATCH", "KXWTAMATCH"}
    assert set(cfg.kalshi_maker_fee_rates.values()) == {KALSHI_MAKER_FEE_RATE}
    for series, _c, _p, _f in CHARGED:
        assert cfg.kalshi_maker_fee_rates.get(series), series
    for series, _c, _p in FREE:
        assert not cfg.kalshi_maker_fee_rates.get(series, 0.0), series


def test_a_series_that_charges_nothing_is_priced_at_nothing():
    """38/38 soccer and UFC maker fills were charged $0.00. Nothing about resting on them may change."""
    cfg = mrt_config.load_maker_rt_config(overrides=None)
    for series, count, price in FREE:
        rate = cfg.kalshi_maker_fee_rates.get(series, 0.0)
        assert _venue_fee(count, price, rate) == 0.0, series
    assert rest_maker_fee(0.5, 0.0) == 0.0


def test_the_hard_refusal_list_stays_empty_and_separate():
    """A fee that is PRICED does not need a series BANNED. The refuse list remains the emergency lever;
    conflating the two is how a measurement turns into an unintended trading-rail change."""
    cfg = mrt_config.load_maker_rt_config(overrides=None)
    assert cfg.kalshi_maker_fee_series == ()


# --------------------------------------------------------------------------- #
# 3. the effect on quoting                                                      #
# --------------------------------------------------------------------------- #
def _views(rest_bid=0.30, rest_ask=0.60, hedge_ask=0.60):
    rest = SideView(best_bid=rest_bid, best_ask=rest_ask, bid_sizes={}, ask_ladder=[])
    hedge = SideView(best_bid=None, best_ask=hedge_ask, bid_sizes={},
                     ask_ladder=[(hedge_ask, 10_000.0)])
    return rest, hedge


def _quote(rate):
    rest, hedge = _views()
    return compute_quote(rest, hedge, hedge_venue="polymarket", tick=0.01, target_net=0.013,
                         quote_usd=70.0, poly_rate=0.05, rest_maker_rate=rate)


def test_the_fee_lowers_the_floor_and_the_reported_edge():
    """It is a real per-share cost of the very trade being priced. Leaving it out overstates the edge."""
    free, charged = _quote(0.0), _quote(KALSHI_MAKER_FEE_RATE)
    assert charged.floor < free.floor
    assert charged.net_at_quote < free.net_at_quote
    exp = rest_maker_fee(charged.quote_price, KALSHI_MAKER_FEE_RATE)
    assert abs((free.net_at_quote - charged.net_at_quote) - exp) < 1.1e-2, "one tick of quantisation"


def test_the_edge_it_reports_is_the_edge_after_the_fee():
    """The number the rails read must be net of everything paid, not net of everything except one thing."""
    d = _quote(KALSHI_MAKER_FEE_RATE)
    from src.genz.maker_rt.quotes import hedge_taker_fee
    expect = (1.0 - d.quote_price - d.hedge_best_ask
              - hedge_taker_fee("polymarket", d.hedge_best_ask, 0.05)
              - rest_maker_fee(d.quote_price, KALSHI_MAKER_FEE_RATE))
    assert abs(d.net_at_quote - expect) < 1e-12


def test_zero_rate_is_byte_identical_to_the_old_behaviour():
    """Soccer and UFC are every series this bot rests Kalshi on today. They must be untouched."""
    rest, hedge = _views()
    old = compute_quote(rest, hedge, hedge_venue="polymarket", tick=0.01, target_net=0.013,
                        quote_usd=70.0, poly_rate=0.05)
    new = compute_quote(rest, hedge, hedge_venue="polymarket", tick=0.01, target_net=0.013,
                        quote_usd=70.0, poly_rate=0.05, rest_maker_rate=0.0)
    assert (old.quote_price, old.floor, old.net_at_quote) == (new.quote_price, new.floor,
                                                              new.net_at_quote)
    assert compute_floor(0.60, "polymarket", 0.013) == compute_floor(0.60, "polymarket", 0.013,
                                                                     rest_maker_rate=0.0)


def test_the_floors_newton_step_is_far_inside_a_tick():
    """The maker fee depends on the price being solved for, so the floor takes ONE correction step. The
    residual has to be negligible against the 1c tick or the shortcut is not a shortcut."""
    for hedge_ask in (0.05, 0.25, 0.50, 0.75, 0.95):
        f = compute_floor(hedge_ask, "polymarket", 0.013, 0.05, KALSHI_MAKER_FEE_RATE)
        exact = f                                          # iterate to the fixed point
        base = 1.0 - hedge_ask - 0.05 * min(hedge_ask, 1 - hedge_ask) - 0.013
        for _ in range(40):
            exact = base - rest_maker_fee(exact, KALSHI_MAKER_FEE_RATE)
        assert abs(f - exact) < 1e-4, hedge_ask


def test_the_candidate_carries_the_rate_only_for_rest_kalshi():
    """Polymarket charges no maker fee at all, so a rest-poly candidate must never carry one — and the
    lookup is by SERIES, the ticker's first segment."""
    from src.genz.maker_rt.driver import Candidate
    assert Candidate.__dataclass_fields__["rest_maker_rate"].default == 0.0
    cfg = mrt_config.load_maker_rt_config(overrides=None)
    assert cfg.kalshi_maker_fee_rates.get("KXMLBGAME-26JUL231507TBTOR-TB".split("-", 1)[0])
