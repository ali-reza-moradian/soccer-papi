"""Shadow fill model (queue simulation) + the live-fill entry point.

SHADOW (default): when a resting quote is placed/repriced we snapshot ``queue_ahead`` = the size
DISPLAYED at our price on the rest venue (everyone ahead of us). Subsequent PUBLIC trade prints at
that price consume the queue; we are FILLED when the cumulative volume printed at our price exceeds
``queue_ahead + our size`` (the queue AND our size traded through), OR the instant any print comes in
STRICTLY BELOW our price (the book traded through our level). Conservative: 20-tick wicks the socket
misses only ever UNDER-count, so the measured fill rate is a lower bound. A reprice RESETS the queue.

LIVE: this module never simulates — the real fill arrives on the Polymarket user channel ("trade"
event = our fill); handle_live_fill() just packages it for the hedger. Nothing here places an order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

_EPS = 1e-9


@dataclass
class ShadowQuote:
    key: tuple                       # (game, market_key, rest_side, direction)
    rest_ref: tuple                  # (venue, identifier, side) — matches a trade print's book
    quote_price: float
    size: float
    queue_ahead: float               # displayed size AT our price when we (re)joined
    at_best: bool
    placed_ts: float
    hedge_ctx: dict = field(default_factory=dict)   # everything the hedger needs (venue, ref, node…)
    cumulative_at_price: float = 0.0                # volume printed AT our price since we joined
    filled: bool = False


@dataclass
class FillEvent:
    key: tuple
    quote_price: float
    size: float
    at_best: bool
    quote_age_s: float
    trigger: str                     # "traded_through" | "queue_consumed"
    hedge_ctx: dict
    ts: float


class ShadowFillModel:
    """Holds every live shadow quote and turns public trade prints into fills."""

    def __init__(self) -> None:
        self.quotes: dict[tuple, ShadowQuote] = {}

    def arm(self, key: tuple, rest_ref: tuple, quote_price: float, size: float, queue_ahead: float,
            at_best: bool, ts: float, hedge_ctx: Optional[dict] = None) -> None:
        """Place OR reprice a shadow quote. A changed price/queue RESETS the consumed-so-far counter."""
        prev = self.quotes.get(key)
        if prev is not None and abs(prev.quote_price - quote_price) < _EPS and prev.rest_ref == rest_ref:
            # same price -> keep the queue progress but refresh the displayed queue if it shrank
            prev.queue_ahead = queue_ahead
            prev.size = size
            prev.at_best = at_best
            prev.hedge_ctx = hedge_ctx or prev.hedge_ctx
            return
        self.quotes[key] = ShadowQuote(key=key, rest_ref=rest_ref, quote_price=float(quote_price),
                                       size=float(size), queue_ahead=float(queue_ahead), at_best=at_best,
                                       placed_ts=ts, hedge_ctx=hedge_ctx or {})

    def disarm(self, key: tuple) -> None:
        self.quotes.pop(key, None)

    def open_keys(self) -> list:
        return list(self.quotes.keys())

    def consume_print(self, rest_ref: tuple, price: float, volume: float, ts: float) -> list:
        """Feed a public trade print (venue, identifier, side) at ``price`` for ``volume`` shares.
        Returns the FillEvent(s) it triggered (a print can only fill quotes on the SAME book)."""
        out: list = []
        for key, q in list(self.quotes.items()):
            if q.filled or q.rest_ref != rest_ref:
                continue
            trigger = None
            if price < q.quote_price - _EPS:
                trigger = "traded_through"          # book traded BELOW our resting bid -> we're filled
            elif abs(price - q.quote_price) < _EPS:
                q.cumulative_at_price += float(volume)
                if q.cumulative_at_price >= q.queue_ahead + q.size - _EPS:
                    trigger = "queue_consumed"
            if trigger:
                q.filled = True
                out.append(FillEvent(key=key, quote_price=q.quote_price, size=q.size, at_best=q.at_best,
                                     quote_age_s=max(0.0, ts - q.placed_ts), trigger=trigger,
                                     hedge_ctx=dict(q.hedge_ctx), ts=ts))
                self.quotes.pop(key, None)
        return out


def handle_live_fill(user_trade_event: dict) -> dict:
    """Package a Polymarket user-channel 'trade' event (OUR fill) for the live hedger. Returns
    {token_id, side, price, size, order_id, ts}. Never simulates; live only."""
    e = user_trade_event or {}
    return {
        "token_id": e.get("asset_id") or e.get("token_id"),
        "side": e.get("side"),
        "price": _f(e.get("price")),
        "size": _f(e.get("size")),
        "order_id": e.get("taker_order_id") or e.get("order_id") or e.get("id"),
        "ts": e.get("timestamp") or e.get("ts"),
    }


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
