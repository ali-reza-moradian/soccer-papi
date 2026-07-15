"""Pure execution math (Phase 2): fees, book-walking, sizing helpers, edge-survival.

No network, no SDKs — all functions are deterministic and unit-tested directly. Shared by the
dry-run and live paths so modeled and realized economics use the SAME math.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from .poly_exec import min_poly_shares  # noqa: F401  (re-exported: it's a sizing helper)


# --------------------------------------------------------------------------- #
# Fees                                                                           #
# --------------------------------------------------------------------------- #
def kalshi_fee_cents(count: int, price: float) -> int:
    """Kalshi taker fee in CENTS for ``count`` contracts at ``price`` (dollars in (0,1)).

    Official Kalshi general-markets formula: fee = roundup_to_cent( 0.07 x C x P x (1-P) ), where
    0.07 x C x P x (1-P) is a DOLLAR amount and you round UP to the next whole cent. In cents that
    is ceil(0.07 x C x P x (1-P) x 100) = ceil(7 x C x P x (1-P)). (E.g. C=100, P=0.50 -> $1.75.)
    """
    if count <= 0:
        return 0
    p = min(max(float(price), 0.0), 1.0)
    cents = 0.07 * count * p * (1.0 - p) * 100.0
    # Round to a sane precision before the ceil so binary float error (e.g. 175.00000000000003)
    # doesn't bump an exact $1.75 fee up to the next cent.
    return math.ceil(round(cents, 6))


def kalshi_fee_usd(count: int, price: float) -> float:
    """Kalshi taker fee in dollars (the cent-rounded official formula above)."""
    return kalshi_fee_cents(count, price) / 100.0


DEFAULT_POLY_FEE_RATE = 0.05          # gamma feeSchedule.rate for sports markets (verified live)


def poly_fee_usd(count: float, price: float, rate: float = DEFAULT_POLY_FEE_RATE) -> float:
    """Polymarket sports TAKER fee in DOLLARS for ``count`` shares bought at ``price`` (dollars in (0,1)).

    CONFIRMED to the cent against the live trade widget: the fee is charged in SHARES on payout —
    fee_shares = rate x min(P, 1-P) x count — and each fee share forfeits $1 at settlement, so
    fee_usd = fee_shares x $1 = rate x min(P, 1-P) x count. rate = the market's feeSchedule rate (0.05,
    exponent 1); makers pay 0 (takerOnly). A non-sports market (fees disabled) or rate <= 0 -> 0."""
    if count <= 0 or rate <= 0:
        return 0.0
    p = min(max(float(price), 0.0), 1.0)
    return rate * min(p, 1.0 - p) * float(count)


# --------------------------------------------------------------------------- #
# Book walking                                                                   #
# --------------------------------------------------------------------------- #
@dataclass
class Walk:
    """Result of walking an ask ladder for a target size."""
    filled: float                 # size actually obtainable from the ladder
    avg_price: float              # volume-weighted average fill price (0.0 if nothing filled)
    cost: float                   # filled * avg_price
    fully_filled: bool            # filled >= requested
    levels_consumed: int

    @property
    def empty(self) -> bool:
        return self.filled <= 0


def walk_book(levels: list[tuple[float, float]], size: float) -> Walk:
    """Walk an ascending (price, size) ASK ladder consuming up to ``size`` units to BUY.

    Returns the obtainable fill, its VWAP, total cost, and whether the full ``size`` was available.
    Models the real fill price after eating depth — the core of slippage measurement."""
    remaining = float(size)
    filled = 0.0
    cost = 0.0
    consumed = 0
    for price, avail in levels:
        if remaining <= 1e-12:
            break
        take = min(remaining, float(avail))
        if take <= 0:
            continue
        filled += take
        cost += take * float(price)
        remaining -= take
        consumed += 1
    avg = (cost / filled) if filled > 0 else 0.0
    return Walk(filled=filled, avg_price=avg, cost=cost,
                fully_filled=filled >= float(size) - 1e-9, levels_consumed=consumed)


def depth_at_or_below(levels: list[tuple[float, float]], price: float) -> float:
    """Total size available at ask prices <= ``price`` (the liquidity usable at your target)."""
    return sum(s for p, s in levels if p <= price + 1e-12)


# --------------------------------------------------------------------------- #
# Sizing helpers (proven tricks)                                                #
# --------------------------------------------------------------------------- #
def volume_haircut(available: float, frac: float = 0.80) -> float:
    """Request only ~``frac`` of observed depth so a FOK/IOC is likely to fully fill."""
    return max(0.0, float(available) * float(frac))


def marketable_limit(base: float, buffer: float = 0.01, *, side: str = "buy") -> float:
    """Cross the book by one tick so a limit order is marketable. BUY -> base+buffer; SELL ->
    base-buffer. Clamped to [0.01, 0.99]."""
    px = base + buffer if str(side).lower() == "buy" else base - buffer
    return min(max(px, 0.01), 0.99)


# --------------------------------------------------------------------------- #
# Edge survival (the dry-run measurement + the live go/no-go)                    #
# --------------------------------------------------------------------------- #
@dataclass
class EdgeResult:
    """Modeled economics of an N-leg kalshi<->poly hedge for ``size`` units on each leg.

    For a MECE market (2-way or 3-way 1x2) you buy ``size`` units of every outcome; exactly one
    wins, so payout is a guaranteed ``size`` dollars regardless of N. The kalshi_* / poly_* scalars
    are aggregates (sum across that venue's legs; the *_fill_price is the first such leg) kept for
    backward compatibility; ``per_leg`` carries the full per-leg breakdown."""
    size: float
    kalshi_fill_price: float
    poly_fill_price: float
    kalshi_cost: float
    poly_cost: float
    kalshi_fee: float
    poly_fee: float
    total_cost: float
    payout: float                 # guaranteed return: exactly one outcome wins -> $size
    net_profit: float
    net_edge_pct: float
    arb_survived: bool
    per_leg: list = field(default_factory=list)   # [{"venue","fill_price","cost","fee"}] (N-leg)


def edge_after_costs(size: float, kalshi_fill_price: float, poly_fill_price: float,
                     kalshi_count: int, poly_fee_rate: float = DEFAULT_POLY_FEE_RATE) -> EdgeResult:
    """Net edge of buying ``size`` complementary shares on each venue at the walked fill prices.

    Each share pays $1 if its outcome wins; the two legs cover complementary outcomes, so payout is
    a guaranteed ``size`` dollars. Profit = payout - (kalshi cost + poly cost + BOTH taker fees). Kalshi
    fees use the integer contract count; Poly fees are the share-based sports taker fee."""
    k_cost = size * kalshi_fill_price
    p_cost = size * poly_fill_price
    k_fee = kalshi_fee_usd(kalshi_count, kalshi_fill_price)
    p_fee = poly_fee_usd(size, poly_fill_price, poly_fee_rate)
    total_cost = k_cost + p_cost + k_fee + p_fee
    payout = float(size)
    net = payout - total_cost
    edge_pct = (net / total_cost * 100.0) if total_cost > 0 else 0.0
    return EdgeResult(
        size=size, kalshi_fill_price=kalshi_fill_price, poly_fill_price=poly_fill_price,
        kalshi_cost=k_cost, poly_cost=p_cost, kalshi_fee=k_fee, poly_fee=p_fee,
        total_cost=total_cost, payout=payout, net_profit=net, net_edge_pct=edge_pct,
        arb_survived=net > 0.0,
        per_leg=[{"venue": "kalshi", "fill_price": kalshi_fill_price, "cost": k_cost, "fee": k_fee},
                 {"venue": "polymarket", "fill_price": poly_fill_price, "cost": p_cost, "fee": p_fee}],
    )


def edge_after_costs_n(size: float, leg_fills: list[tuple[str, float]],
                       poly_fee_rate: float = DEFAULT_POLY_FEE_RATE) -> EdgeResult:
    """N-leg generalization of :func:`edge_after_costs`.

    ``leg_fills`` is one (venue, fill_price) per leg. The legs together are MECE over the market's
    outcomes, so payout = ``size`` (exactly one wins). Cost = size*Σ(fill_price) + Σ(taker fees): a
    Kalshi taker fee per kalshi leg (integer contract count) and the share-based sports taker fee per
    Polymarket leg."""
    per_leg: list[dict[str, Any]] = []
    total_leg_cost = 0.0
    k_fee_total = 0.0
    p_fee_total = 0.0
    for venue, fp in leg_fills:
        cost = size * fp
        if venue == "kalshi":
            fee = kalshi_fee_usd(int(size), fp)
            k_fee_total += fee
        elif venue == "polymarket":
            fee = poly_fee_usd(size, fp, poly_fee_rate)
            p_fee_total += fee
        else:
            fee = 0.0
        total_leg_cost += cost
        per_leg.append({"venue": venue, "fill_price": fp, "cost": cost, "fee": fee})
    total_cost = total_leg_cost + k_fee_total + p_fee_total
    payout = float(size)
    net = payout - total_cost
    edge_pct = (net / total_cost * 100.0) if total_cost > 0 else 0.0
    k_fps = [fp for v, fp in leg_fills if v == "kalshi"]
    p_fps = [fp for v, fp in leg_fills if v == "polymarket"]
    return EdgeResult(
        size=size,
        kalshi_fill_price=k_fps[0] if k_fps else 0.0,
        poly_fill_price=p_fps[0] if p_fps else 0.0,
        kalshi_cost=size * sum(k_fps), poly_cost=size * sum(p_fps),
        kalshi_fee=k_fee_total, poly_fee=p_fee_total,
        total_cost=total_cost, payout=payout, net_profit=net, net_edge_pct=edge_pct,
        arb_survived=net > 0.0, per_leg=per_leg,
    )
