"""In-memory order books for the quotable universe + per-SIDE views.

A maker rests a bid on ONE outcome (a poly token, or a Kalshi YES/NO side) and hedges by lifting the
COMPLEMENT outcome's ask on the other venue. So every book must answer, for a given side: best_bid
(the price to join/improve), best_ask (so the quote is never crossable), the size resting AT a bid
price (queue-ahead), and the ascending ask ladder (to walk a hedge fee-inclusively).

POLY: one book per token (bids/asks in dollars). KALSHI: one book per ticker holding resting YES bids
and NO bids in CENTS; a YES ask is the complement of the best NO bid ((100−c)/100) and vice-versa —
the exact geometry src/executor/kalshi_exec._ask_levels_from_orderbook uses. seq is tracked (Kalshi
delta channel) so a gap forces a resync.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SideView:
    """One outcome's view of a book: the numbers the quote engine + hedge walk need (dollars)."""
    best_bid: Optional[float]
    best_ask: Optional[float]
    bid_sizes: dict          # price(dollars) -> resting size on the side we would REST on
    ask_ladder: list         # ascending [(price_dollars, size)] to BUY this outcome (hedge walk)

    def bid_size_at(self, price: float) -> float:
        for p, s in self.bid_sizes.items():
            if abs(p - price) < 1e-9:
                return float(s)
        return 0.0


@dataclass
class PolyBook:
    """A single Polymarket token book (prices in dollars)."""
    bids: dict = field(default_factory=dict)   # price -> size
    asks: dict = field(default_factory=dict)
    last_update: float = 0.0

    def replace(self, bids: dict, asks: dict) -> None:
        self.bids = {float(p): float(s) for p, s in bids.items() if float(s) > 0}
        self.asks = {float(p): float(s) for p, s in asks.items() if float(s) > 0}

    def apply_change(self, price: float, side: str, size: float) -> None:
        book = self.bids if str(side).upper() in ("BUY", "BID", "YES") else self.asks
        price, size = float(price), float(size)
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size

    def view(self) -> SideView:
        bb = max(self.bids) if self.bids else None
        ba = min(self.asks) if self.asks else None
        ladder = sorted(((p, s) for p, s in self.asks.items() if s > 0), key=lambda x: x[0])
        return SideView(best_bid=bb, best_ask=ba, bid_sizes=dict(self.bids), ask_ladder=ladder)


@dataclass
class KalshiBook:
    """A single Kalshi market book: resting YES bids and NO bids, keyed by integer CENTS."""
    yes: dict = field(default_factory=dict)    # cents(int) -> size
    no: dict = field(default_factory=dict)
    seq: Optional[int] = None
    last_update: float = 0.0

    def replace(self, yes_levels, no_levels, seq: Optional[int] = None) -> None:
        self.yes = {int(c): float(s) for c, s in yes_levels if float(s) > 0}
        self.no = {int(c): float(s) for c, s in no_levels if float(s) > 0}
        self.seq = seq

    def apply_delta(self, side: str, price_cents: int, delta: float, seq: Optional[int] = None) -> None:
        book = self.yes if str(side).lower() == "yes" else self.no
        price_cents = int(price_cents)
        book[price_cents] = max(0.0, book.get(price_cents, 0.0) + float(delta))
        if book[price_cents] <= 0:
            book.pop(price_cents, None)
        if seq is not None:
            self.seq = seq

    def view(self, side: str) -> SideView:
        """View for BUYING/RESTING ``side`` (YES or NO). best_bid = top of that side's resting bids;
        best_ask = complement of the OPPOSITE side's best bid; ask_ladder = complement of the opposite
        side's bids ascending; bid_sizes = this side's resting bids (queue-ahead when we join)."""
        s = str(side).lower()
        mine = self.yes if s == "yes" else self.no
        opp = self.no if s == "yes" else self.yes
        best_bid = (max(mine) / 100.0) if mine else None
        # A resting OPPOSITE-side bid at c cents is an offer to BUY our side at (100 - c) cents, so the
        # ask ladder (and the best ask) for our side is the complement of the opposite side's bids.
        ladder = sorted((((100 - c) / 100.0, sz) for c, sz in opp.items() if sz > 0), key=lambda x: x[0])
        best_ask = ladder[0][0] if ladder else None
        bid_sizes = {c / 100.0: sz for c, sz in mine.items()}
        return SideView(best_bid=best_bid, best_ask=best_ask, bid_sizes=bid_sizes, ask_ladder=ladder)
