"""Honest walk-to-stake sizing for OG multi-sport arbs — a THIN wrapper over ``src.og_sizing``.

Given the legs ``arbitrage.compute_arb`` chose (best book per outcome) plus a per-leg ``extras`` map
(the walked exchange ladder + its fee tag for Kalshi/Polymarket legs, or a flat fixed-book limit),
build the leg specs ``og_sizing.honest_size`` expects and re-size the arb HONESTLY — walking each
exchange leg through its real ask book and holding fixed books at their flat price/limit. Identical in
spirit to the soccer OG's ``run._og_leg_specs`` + ``honest_size`` call, minus any network (the ladders
are fetched once, up front, by scan.py and handed in).
"""
from __future__ import annotations

from typing import Any, Optional

from .. import bookmath, og_sizing
from ..arbitrage import ArbResult


def build_leg_specs(res: ArbResult, extras: dict[tuple[str, int], dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate the chosen ArbResult legs into ``honest_size`` leg dicts using ``extras`` keyed by
    ``(book, outcome_id)``. Exchange legs carry a validated ascending ``ladder`` + ``fee_book`` (their
    ``top_odds`` is re-derived from the genuine best ask, like run.py); fixed books carry a flat
    ``limit`` (None => unconstrained, e.g. 1xbet)."""
    specs: list[dict[str, Any]] = []
    for leg in res.legs:
        ex = extras.get((leg.book, leg.outcome_id)) or {}
        spec: dict[str, Any] = {"outcome": leg.outcome_name, "book": leg.book,
                                "top_odds": leg.eff_odds, "limit": ex.get("size_limit")}
        ladder = ex.get("ladder")
        if ladder:
            asks = bookmath.valid_asks(ladder)
            if asks:
                spec["ladder"] = asks
                spec["top_odds"] = 1.0 / asks[0][0]     # the genuine best ask, not the stale tick
                spec["fee_book"] = ex.get("fee_book")   # 'kalshi' / 'polymarket' -> exact per-share fee
        specs.append(spec)
    return specs


def honest_size_arb(res: ArbResult, extras: dict[tuple[str, int], dict[str, Any]], *,
                    bankroll_cap: float, poly_fee_rate: float) -> Optional[og_sizing.HonestSize]:
    """Honestly size a computed arb (NET of exact exchange taker fees). Returns a HonestSize, or None
    when not even the first unit of payout is profitable at the margin (below-floor / no edge)."""
    return og_sizing.honest_size(build_leg_specs(res, extras),
                                 bankroll_cap=bankroll_cap, poly_fee_rate=poly_fee_rate)
