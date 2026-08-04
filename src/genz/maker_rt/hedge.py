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

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from ... import bookmath
from ...executor.fees_sizing import kalshi_fee_usd, poly_fee_usd
from ...executor.poly_exec import market_buy_shortfall
from .quotes import hedge_taker_fee


def kalshi_actual_fee(res: Any, filled: int, price: float) -> float:
    """ACTUAL Kalshi taker fee in DOLLARS for a hedge fill. Prefer the venue's reported fee when it is
    present AND agrees with the official formula (a guard against a cents/dollars unit slip — the same
    class of bug as the $500-for-$5 settlement), else the exact official ``ceil_to_cent(0.07·C·P·(1-P))``
    which EQUALS ``average_fee_paid`` for a taker fill by construction. Computed from the ACTUAL fill
    count + fill price (venue truth), never the pre-fire quoted ask."""
    formula = kalshi_fee_usd(int(filled), float(price))
    sources = [res, (res or {}).get("raw") if isinstance(res, dict) else None]
    for src in sources:
        if not isinstance(src, dict):
            continue
        # "fee" is the normalizer's venue-truth sum (taker_fees_dollars + maker_fees_dollars) and is
        # authoritative when present; the rest are legacy/raw spellings.
        for k in ("fee", "average_fee_paid", "fee_paid", "fees_paid"):
            v = src.get(k)
            if v is None:
                continue
            try:
                rep = abs(float(v))
            except (TypeError, ValueError):
                continue
            for cand in (rep, rep / 100.0):                    # accept dollars or cents, if it matches
                if abs(cand - formula) <= max(0.02, formula * 0.5):
                    return round(cand, 6)
    return round(formula, 6)


def mark_hedge(ask_ladder: list, size: float, hedge_venue: str,
               poly_rate: float = 0.05) -> Optional[dict]:
    """Walk ``ask_ladder`` for ``size`` shares (VWAP) and add the venue taker fee. Returns
    {avg_price, shares, fee, cost, cost_per_share, requested, fully_filled} or None if the book is empty.

    ``requested``/``fully_filled`` are part of the CONTRACT, not decoration: a walk that ran out of book
    returns the VWAP of the SHALLOW part it could fill, which is CHEAPER than the price a real sweep for
    the full size would pay. A caller that reads ``avg_price``/``cost_per_share`` without checking
    ``fully_filled`` is pricing a hedge it cannot actually get — that is exactly how the 2026-07-28 01:21Z
    PHIMIA fill passed the pre-hedge gate on a ~5c partial walk and then swept to 7c for a $1.01/share
    guaranteed loss."""
    w = bookmath.walk_book(bookmath.valid_asks(ask_ladder), size)
    if w.filled <= 0:
        return None
    fee = hedge_taker_fee(hedge_venue, w.avg_price, poly_rate) * w.filled
    cost = w.cost + fee
    return {"avg_price": w.avg_price, "shares": w.filled, "fee": fee, "cost": cost,
            "cost_per_share": cost / w.filled,
            "requested": float(size), "fully_filled": bool(w.fully_filled)}


def locked_net(quote_price: float, hedge_cost_per_share: float) -> float:
    """The locked per-share net edge: 1 − rest fill price − hedge cost/share (fee-inclusive)."""
    return 1.0 - float(quote_price) - float(hedge_cost_per_share)


def _apply_cap(limit: float, cap: Optional[float]) -> float:
    """Clamp a marketable hedge limit to ``cap`` (the price the pre-hedge gate approved), FLOORED to a
    1c tick so the venue always gets a valid, never-more-permissive price. ``cap`` None -> unchanged."""
    if cap is None:
        return float(limit)
    capped = min(float(limit), float(cap))
    return max(0.0, math.floor(capped * 100.0) / 100.0)          # floor to tick == never round UP into a loss


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
    hedge_fee: Optional[float] = None    # ACTUAL taker fee paid on the hedge leg ($) — fee-honest pnl/net
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

    # Executed-vs-expected divergence beyond this is reported, never silently booked.
    DIVERGENCE_WARN = 0.10

    def _warn_if_diverged(self, what: Any, expected: Any, executed: Any, limit: float,
                          res: Any = None) -> None:
        """Scream when the EXECUTED hedge price lands far from the book we priced it off.

        A price-SPACE error is invisible to every other check — it produces a perfectly well-formed
        number, just the complement of the truth — but it always shows up here: Fortaleza priced a 95c
        ask and "executed" at 5c, a 90c divergence, and nothing said a word. This cannot decide which
        value is right, so it does not try; it reports, and the booking invariant refuses."""
        if executed is None or expected is None or not self.log:
            return
        try:
            gap = abs(float(executed) - float(expected))
        except (TypeError, ValueError):
            return
        if gap >= self.DIVERGENCE_WARN:
            self.log.error("[MAKER_RT][LIVE] HEDGE PRICE DIVERGENCE %s: expected ~%.4f (book), executed "
                           "%.4f (limit sent %.4f, source %s) — gap %.4f. A gap this large is usually a "
                           "price-SPACE error, not a moved book.", what, float(expected), float(executed),
                           float(limit), (res or {}).get("avg_price_source"), gap)

    def hedge(self, fill: dict, hedge: dict) -> HedgeResult:
        """Lift the Kalshi complement with a marketable IOC (limit at ask+buffer). Returns a HedgeResult
        with the ACTUAL Kalshi fill count: "locked" (full), "partial" (some), "missed" (0), or "error".

        It does NOT unwind on a miss -- the CALLER (the executor) owns the ONE verified unwind, because a
        naked fill must be sold AND the position REST-confirmed flat before we can claim 'unwound'. The
        old in-hedger unwind logged success off avg_price presence (a killed FOK still has a limit price),
        which is exactly how the -$2.35 orphan slipped through."""
        size = int(round(float(fill.get("size") or 0)))
        if size <= 0 or self.kalshi is None:
            return HedgeResult("error", detail={"reason": "no_size_or_client"})
        ticker, k_side = hedge.get("ticker"), hedge.get("side", "yes")
        cap = hedge.get("max_price")
        if cap is not None and float(cap) <= 0.0:
            # There is NO price at which this hedge clears the gate's floor. Placing a 1-tick order that
            # cannot fill would still burn a round trip and a slot — report the miss and let the caller
            # run its verified unwind.
            return HedgeResult("missed", freeze_market=True, detail={"reason": "cap_not_payable"})
        limit = min(0.99, float(hedge.get("best_ask") or 0.5) + self.buffer)   # marketable limit
        limit = _apply_cap(limit, cap)                          # never pay more than the gate approved
        try:
            res = self.kalshi.place_order(ticker, k_side, size, limit,
                                          time_in_force="immediate_or_cancel")
        except Exception as exc:  # noqa: BLE001 - a placement error -> treat as a miss
            res = {"status": "error", "fill_count": 0, "error": str(exc)}
        filled = int(res.get("fill_count", 0) or 0)
        avg = res.get("avg_price")
        self._warn_if_diverged(ticker, hedge.get("best_ask"), avg, limit, res)
        if filled >= size:
            px = float(avg if avg is not None else limit)
            fee = kalshi_actual_fee(res, filled, px)           # ACTUAL fee (venue/official), not the model
            pnl = (1.0 - float(fill.get("price") or 0) - px) * filled - fee
            if self.log:
                self.log.info("[MAKER_RT][LIVE] hedge LOCKED %s x%d @ %.4f (fee $%.3f) -> pnl $%.2f",
                              ticker, filled, px, fee, pnl)
            return HedgeResult("locked", hedged_shares=filled, hedge_avg_price=px,
                               hedge_fee=fee, locked_pnl=round(pnl, 4), detail={"kalshi": res})
        # MISS / PARTIAL -> report the shortfall; the executor does the VERIFIED unwind + freezes.
        status = "partial" if filled > 0 else "missed"
        if self.log:
            self.log.warning("[MAKER_RT][LIVE] hedge %s %s (got %d/%d) -> caller must unwind %d.",
                             status.upper(), ticker, filled, size, size - filled)
        return HedgeResult(status, hedged_shares=filled, hedge_avg_price=avg, freeze_market=True,
                           detail={"kalshi": res})

    def hedge_poly(self, fill: dict, hedge: dict) -> HedgeResult:
        """Lift the POLYMARKET complement with a marketable FAK taker BUY sized to the fill — the rest_kalshi
        direction's hedge (mirror of ``hedge`` for the reverse venue). Returns a HedgeResult with the ACTUAL
        poly fill: "locked" (full), "partial" (some), "missed" (0), or "error". Does NOT unwind on a miss —
        the executor owns the ONE verified unwind (the naked Kalshi position, IOC-sold then REST-confirmed
        flat via the portfolio positions endpoint)."""
        size = float(fill.get("size") or 0)
        if size <= 0 or self.poly is None:
            return HedgeResult("error", detail={"reason": "no_size_or_client"})
        token, best_ask = hedge.get("token"), hedge.get("best_ask")
        cap = hedge.get("max_price")
        if cap is not None and float(cap) <= 0.0:
            return HedgeResult("missed", freeze_market=True, detail={"reason": "cap_not_payable"})
        # THE PRICE WE EXPECT TO CLEAR AT — the walk of the ask ladder for exactly this size, which the
        # caller has just done for the pre-hedge gate. A Poly market BUY spends its whole USDC amount, so
        # this is what decides how many shares come back; sizing the spend off the padded LIMIT instead
        # is what handed 2 ticks' worth of pad back as unhedged shares on every single hedge.
        walk = hedge.get("walk_price")
        if walk is None:
            walk = best_ask
        try:
            walk = float(walk) if walk is not None else None
        except (TypeError, ValueError):
            walk = None
        try:
            # A capped hedge passes its LIMIT explicitly; uncapped (price=None) keeps the client's own
            # best_ask + 2 ticks marketable default. Either way the SPEND comes from ``walk``.
            res = self.poly.place_market_buy(token, size, expected_price=walk) if cap is None \
                else self.poly.place_market_buy(token, size, price=_apply_cap(1.0, cap),
                                                expected_price=walk)
        except Exception as exc:  # noqa: BLE001 - a placement error -> treat as a miss
            res = {"status": "error", "shares": 0, "error": str(exc)}
        filled = float((res or {}).get("shares") or 0.0) if isinstance(res, dict) else 0.0
        avg = res.get("avg_price") if isinstance(res, dict) else None
        self._warn_if_diverged(str(token)[:12], best_ask, avg, float(cap or 1.0), res)
        # "DID I GET WHAT I ASKED FOR?" — judged against the venue's own amount precision, not against
        # zero. A market BUY spends whole cents, so the last sub-cent of the target is unbuyable: the
        # live proof of 2026-08-04 delivered 2.666664 sh against a 2.6667 sh target, which at a 1e-9
        # tolerance is a MISS and would send every hedge down the unwind-or-verify path. Clamped to a
        # half share so a genuinely short hedge can never hide inside it.
        tol = max(1e-9, min(0.5, market_buy_shortfall(avg if avg else (best_ask or 1.0))))
        if filled >= size - tol:
            px = float(avg if avg is not None else (best_ask or 0.0))
            fee = poly_fee_usd(filled, px, self.poly_rate)     # ACTUAL Poly taker fee (maker rest = 0)
            pnl = (1.0 - float(fill.get("price") or 0) - px) * filled - fee
            if self.log:
                self.log.info("[MAKER_RT][LIVE] poly hedge LOCKED %s x%.0f @ %.4f (fee $%.3f) -> pnl $%.2f",
                              str(token)[:12], filled, px, fee, pnl)
            return HedgeResult("locked", hedged_shares=filled, hedge_avg_price=px,
                               hedge_fee=round(fee, 6), locked_pnl=round(pnl, 4), detail={"poly": res})
        status = "partial" if filled > 0 else "missed"
        if self.log:
            self.log.warning("[MAKER_RT][LIVE] poly hedge %s %s (got %.0f/%.0f) -> caller must unwind.",
                             status.upper(), str(token)[:12], filled, size)
        return HedgeResult(status, hedged_shares=filled, hedge_avg_price=avg, freeze_market=True,
                           detail={"poly": res})
