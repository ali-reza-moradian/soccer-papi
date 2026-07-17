"""The event-driven quote engine — PURE quote math + a small stateful driver.

Per outcome we may rest on Polymarket (maker fee $0) and hedge by lifting the COMPLEMENT outcome's
ask on Kalshi, or rest on Kalshi and hedge on Polymarket. For each direction:

    floor       = 1 − hedge_best_ask − hedge_taker_fee(hedge_best_ask) − target_net
    quote_price = round_DOWN_to_tick( min(floor, rest_best_bid + tick) )
                  MUST be <= rest_best_ask − tick        (never crossable => always a maker; else skip)
                  placed only when >= rest_best_bid       (at-best/improving; behind-best is counted, not filled)
    precondition: the hedge ask ladder shows >= quote size within hedge_best_ask + 1 tick.

All money is per share; the two outcomes are complementary (their $1 payouts don't overlap), so the
locked net of resting at ``quote_price`` and hedge-lifting at ``hedge_best_ask`` is
1 − quote_price − hedge_best_ask − hedge_fee.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .books import SideView

POLY_MIN_SHARES = 5             # Polymarket resting minimum
DEFAULT_TICK = 0.01


def hedge_taker_fee(venue: str, price: float, poly_rate: float = 0.05) -> float:
    """Per-share TAKER fee to LIFT a hedge: Kalshi 0.07·p·(1−p); Polymarket poly_rate·min(p,1−p)."""
    p = min(max(float(price), 0.0), 1.0)
    v = str(venue).lower()
    if v == "kalshi":
        return 0.07 * p * (1.0 - p)
    if v in ("polymarket", "poly"):
        return poly_rate * min(p, 1.0 - p)
    return 0.0


def round_down_tick(x: float, tick: float) -> float:
    tick = tick or DEFAULT_TICK
    return round(math.floor(x / tick + 1e-9) * tick, 6)


def compute_floor(hedge_best_ask: float, hedge_venue: str, target_net: float,
                  poly_rate: float = 0.05) -> float:
    """The raw economic floor price: the highest rest price that still nets >= target_net after the
    hedge's taker fee."""
    return 1.0 - hedge_best_ask - hedge_taker_fee(hedge_venue, hedge_best_ask, poly_rate) - target_net


@dataclass
class QuoteDecision:
    viable: bool                 # place a real (or shadow) resting quote AND model fills for it
    reason: str
    quote_price: Optional[float] = None
    size_shares: Optional[float] = None
    floor: Optional[float] = None
    at_best: bool = False
    would_be_behind: bool = False   # priced BEHIND the best bid: counted in the summary, no fill model
    hedge_best_ask: Optional[float] = None
    net_at_quote: Optional[float] = None


def compute_quote(rest: SideView, hedge: SideView, *, hedge_venue: str, tick: float,
                  target_net: float, quote_usd: float, poly_rate: float = 0.05,
                  hedge_tick: float = DEFAULT_TICK, min_shares: int = POLY_MIN_SHARES) -> QuoteDecision:
    """One direction's quote decision from the current rest/hedge side-views. Never crosses (always a
    maker); refuses when the hedge book is too thin to cover the quote at the ask + 1 tick."""
    tick = tick or DEFAULT_TICK
    hedge_ask = hedge.best_ask
    if hedge_ask is None or not (0.0 < hedge_ask < 1.0):
        return QuoteDecision(False, "no_hedge_ask")
    floor = compute_floor(hedge_ask, hedge_venue, target_net, poly_rate)
    if floor < tick:
        return QuoteDecision(False, "floor_below_tick", floor=floor, hedge_best_ask=hedge_ask)
    rest_bid, rest_ask = rest.best_bid, rest.best_ask
    cap = floor if rest_bid is None else min(floor, rest_bid + tick)
    qp = round_down_tick(cap, tick)
    if qp < tick:
        return QuoteDecision(False, "quote_below_tick", floor=floor, hedge_best_ask=hedge_ask)
    # NEVER CROSSABLE: a resting bid must sit at least one tick below the rest venue's own best ask.
    if rest_ask is not None and qp > rest_ask - tick + 1e-9:
        return QuoteDecision(False, "would_cross", quote_price=qp, floor=floor, hedge_best_ask=hedge_ask)
    size = quote_usd / qp
    if size < min_shares:
        size = float(min_shares)
    # HEDGE PRECONDITION: enough resting hedge depth within one tick of the hedge ask to cover us.
    depth = sum(s for p, s in hedge.ask_ladder if p <= hedge_ask + hedge_tick + 1e-9)
    if depth < size - 1e-9:
        return QuoteDecision(False, "hedge_too_thin", quote_price=qp, size_shares=size,
                             floor=floor, hedge_best_ask=hedge_ask)
    at_best = rest_bid is None or qp >= rest_bid - 1e-9
    net = 1.0 - qp - hedge_ask - hedge_taker_fee(hedge_venue, hedge_ask, poly_rate)
    if not at_best:
        return QuoteDecision(False, "behind_best", quote_price=qp, size_shares=size, floor=floor,
                             at_best=False, would_be_behind=True, hedge_best_ask=hedge_ask, net_at_quote=net)
    return QuoteDecision(True, "ok", quote_price=qp, size_shares=size, floor=floor, at_best=True,
                         would_be_behind=False, hedge_best_ask=hedge_ask, net_at_quote=net)


def poly_leg_exceeds_cap(rest_venue: str, quote_price: Optional[float],
                         hedge_best_ask: Optional[float], cap: Optional[float]) -> bool:
    """TENNIS walkover guard: True when the POLYMARKET leg of a direction prices above ``cap`` -> skip.
    The Poly leg is the REST price when resting on Poly (rest_venue=='polymarket'), else the HEDGE best
    ask when hedging on Poly. Bounds the pre-ball-walkover tail (Poly settles 50c, Kalshi refunds ~last
    price). ``cap`` None (non-tennis) -> never skips."""
    if cap is None:
        return False
    poly_px = quote_price if rest_venue == "polymarket" else hedge_best_ask
    return poly_px is not None and poly_px > cap + 1e-9


def needs_reprice(prev: QuoteDecision, cur: QuoteDecision, tick: float) -> bool:
    """Reprice when the floor OR the resulting quote price moved by >= one tick (both directions)."""
    tick = tick or DEFAULT_TICK
    if prev is None or cur is None:
        return True
    if prev.quote_price is None or cur.quote_price is None:
        return prev.quote_price != cur.quote_price
    if abs((cur.quote_price or 0) - (prev.quote_price or 0)) >= tick - 1e-9:
        return True
    if prev.floor is not None and cur.floor is not None and abs(cur.floor - prev.floor) >= tick - 1e-9:
        return True
    return False
