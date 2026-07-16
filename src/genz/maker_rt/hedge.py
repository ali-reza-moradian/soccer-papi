"""Hedge marking (shadow) + live hedge execution (built, locked).

SHADOW: on a shadow fill we WALK the hedge venue's live in-memory ask ladder for the fill size and
add that venue's taker fee, giving the fee-inclusive cost-per-share and the locked net edge. We also
record the hedge mid at +1s/+5s/+30s (adverse selection).

LIVE (only when armed): buy the COMPLEMENT outcome on the other venue with a marketable IOC-emulated
limit (Kalshi) sized to the matched amount. Filled -> locked pnl. Miss/partial at the timeout ->
market-unwind the original fill (SELL, taker fee applies) and freeze that market. This module places
orders ONLY through injected order clients, and the caller supplies those ONLY when the live gate is
open (see LiveGate). One in-flight hedge at a time is enforced by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ... import bookmath
from .quotes import hedge_taker_fee


def mark_hedge(ask_ladder: list, size: float, hedge_venue: str,
               poly_rate: float = 0.05) -> Optional[dict]:
    """Walk ``ask_ladder`` for ``size`` shares (VWAP) and add the venue taker fee. Returns
    {avg_price, shares, fee, cost, cost_per_share} or None if the book is empty."""
    w = bookmath.walk_book(bookmath.valid_asks(ask_ladder), size)
    if w.filled <= 0:
        return None
    fee = hedge_taker_fee(hedge_venue, w.avg_price, poly_rate) * w.filled
    cost = w.cost + fee
    return {"avg_price": w.avg_price, "shares": w.filled, "fee": fee, "cost": cost,
            "cost_per_share": cost / w.filled}


def locked_net(quote_price: float, hedge_cost_per_share: float) -> float:
    """The locked per-share net edge: 1 − rest fill price − hedge cost/share (fee-inclusive)."""
    return 1.0 - float(quote_price) - float(hedge_cost_per_share)


def hedge_mid(view) -> Optional[float]:
    """(best_bid + best_ask)/2 of a hedge SideView — the adverse-selection drift probe."""
    bb, ba = getattr(view, "best_bid", None), getattr(view, "best_ask", None)
    if bb is None and ba is None:
        return None
    if bb is None:
        return ba
    if ba is None:
        return bb
    return (bb + ba) / 2.0


# --------------------------------------------------------------------------- #
# LIVE hedger (built + unit-tested with fakes; used ONLY when armed)             #
# --------------------------------------------------------------------------- #
@dataclass
class HedgeResult:
    status: str                      # "locked" | "unwound" | "partial_unwound" | "error"
    hedged_shares: float = 0.0
    hedge_avg_price: Optional[float] = None
    locked_pnl: Optional[float] = None
    unwind_cost: Optional[float] = None
    freeze_market: bool = False
    detail: dict = field(default_factory=dict)


class LiveHedger:
    """Executes the live hedge for one fill. Both clients are INJECTED (never built here); the caller
    passes them only when the live gate is open, so an unarmed process can never place an order."""

    def __init__(self, kalshi_client: Any = None, poly_client: Any = None, *,
                 buffer: float = 0.01, poly_rate: float = 0.05, log: Any = None) -> None:
        self.kalshi = kalshi_client
        self.poly = poly_client
        self.buffer = buffer
        self.poly_rate = poly_rate
        self.log = log

    def hedge(self, fill: dict, hedge: dict) -> HedgeResult:
        """fill = {token_id, side, price, size} (our Polymarket maker fill). hedge describes the Kalshi
        complement leg to LIFT: {ticker, side, best_ask}. IOC-emulated marketable limit at ask+buffer;
        on miss/partial, market-unwind the poly fill (SELL) and freeze the market."""
        size = int(round(float(fill.get("size") or 0)))
        if size <= 0 or self.kalshi is None:
            return HedgeResult("error", detail={"reason": "no_size_or_client"})
        ticker, k_side = hedge.get("ticker"), hedge.get("side", "yes")
        limit = min(0.99, float(hedge.get("best_ask") or 0.5) + self.buffer)   # marketable limit
        try:
            res = self.kalshi.place_order(ticker, k_side, size, limit,
                                          time_in_force="immediate_or_cancel")
        except Exception as exc:  # noqa: BLE001 - a placement error -> treat as a miss and unwind
            res = {"status": "error", "fill_count": 0, "error": str(exc)}
        filled = int(res.get("fill_count", 0) or 0)
        avg = res.get("avg_price")
        if filled >= size:
            fee = hedge_taker_fee("kalshi", avg or limit, self.poly_rate) * filled
            pnl = (1.0 - float(fill.get("price") or 0) - (avg or limit)) * filled - fee
            if self.log:
                self.log.info("[MAKER_RT][LIVE] hedge LOCKED %s x%d @ %.4f -> pnl $%.2f",
                              ticker, filled, avg or limit, pnl)
            return HedgeResult("locked", hedged_shares=filled, hedge_avg_price=avg or limit,
                               locked_pnl=round(pnl, 4), detail={"kalshi": res})
        # MISS / PARTIAL -> unwind the naked poly fill (SELL) and freeze this market 10 min.
        remainder = size - filled
        unwind = None
        if self.poly is not None:
            try:
                unwind = self.poly.place_market_sell(fill.get("token_id"), remainder)
            except Exception as exc:  # noqa: BLE001
                unwind = {"status": "error", "error": str(exc)}
        cost = None
        if isinstance(unwind, dict) and unwind.get("avg_price") is not None:
            cost = round((float(fill.get("price") or 0) - float(unwind["avg_price"])) * remainder, 4)
        if self.log:
            self.log.warning("[MAKER_RT][LIVE] hedge MISS %s (got %d/%d) -> unwound %s; freeze market.",
                             ticker, filled, size, remainder)
        status = "partial_unwound" if filled > 0 else "unwound"
        return HedgeResult(status, hedged_shares=filled, hedge_avg_price=avg, unwind_cost=cost,
                           freeze_market=True, detail={"kalshi": res, "unwind": unwind})
