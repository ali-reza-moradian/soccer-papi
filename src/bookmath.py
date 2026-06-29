"""Pure order-book math shared by the SCANNER and (by parity test) the executor.

No network, no SDKs, no project imports — just deterministic functions over ascending ASK ladders
so the scanner's depth-aware Polymarket pricing and the executor's fill simulation can't drift.

Two primitives:
  * ``walk_book(levels, size)``  — consume up to ``size`` units off an ascending ask ladder and
    report the realized fill + VWAP. Semantics are IDENTICAL to executor.fees_sizing.walk_book
    (guarded by a parity test); the executor keeps its own copy for package isolation.
  * ``vwap_within_band(levels, slippage_pct)`` — the size-weighted average price, and the fillable
    size, of all depth from the best ask outward up to ``slippage_pct`` worse than the best ask.
    This is what makes a thin top-of-book quote honest: a deep leg keeps ~its best-ask price while a
    thin/flickering longshot leg prices to a worse VWAP over a much smaller fillable size.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass
class Walk:
    """Result of walking an ask ladder for a target size (mirrors executor.fees_sizing.Walk)."""
    filled: float                 # size actually obtainable from the ladder
    avg_price: float              # volume-weighted average fill price (0.0 if nothing filled)
    cost: float                   # filled * avg_price
    fully_filled: bool            # filled >= requested
    levels_consumed: int


def walk_book(levels: Iterable[tuple[float, float]], size: float) -> Walk:
    """Walk an ascending (price, size) ASK ladder consuming up to ``size`` units to BUY.

    Returns the obtainable fill, its VWAP, total cost, and whether the full ``size`` was available.
    Identical semantics to executor.fees_sizing.walk_book (kept in sync by a parity test)."""
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


def valid_asks(levels: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Ascending-by-price list of valid (price, size) asks: price a probability in (0,1), size > 0.

    Use this to sanitize a raw CLOB ask ladder before ``walk_book`` (which walks in the given order
    and does NOT sort/validate) — so a walk-to-stake consumes cheapest-first, exactly like a real
    market buy and like the probe's ``sorted(...)``."""
    out: list[tuple[float, float]] = []
    for p, s in levels:
        try:
            price, sz = float(p), float(s)
        except (TypeError, ValueError):
            continue
        if 0.0 < price < 1.0 and sz > 0.0:
            out.append((price, sz))
    out.sort(key=lambda x: x[0])
    return out


_valid_asks = valid_asks   # internal alias (kept so existing call sites read unchanged)


def vwap_within_band(levels: Iterable[tuple[float, float]],
                     slippage_pct: float) -> Optional[tuple[float, float]]:
    """Depth-aware quote for an ascending ASK ladder -> (vwap_price, fillable_size) or None.

    Accumulate depth from the best ask outward, INCLUDING every level whose price is at most
    ``slippage_pct`` percent worse than the best ask, and stop once a level is more than that worse.
    Returns the size-weighted average price across the accumulated band and the total fillable size
    (shares) inside it. None if the ladder has no valid ask. ``slippage_pct <= 0`` keeps only the
    best-priced level(s). A deep leg's band spans large size at ~the best ask; a thin leg's band is
    a small slice priced to a slightly worse VWAP — exactly the honesty fix the scanner needs."""
    asks = _valid_asks(levels)
    if not asks:
        return None
    best = asks[0][0]
    ceiling = best * (1.0 + max(0.0, float(slippage_pct)) / 100.0)
    cost = 0.0
    shares = 0.0
    for price, sz in asks:
        if price > ceiling + 1e-12:
            break
        cost += price * sz
        shares += sz
    if shares <= 0.0:
        return None
    return cost / shares, shares
