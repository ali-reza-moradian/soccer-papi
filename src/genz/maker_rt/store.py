"""BookStore — the in-memory books for the whole quotable universe + event application.

The feeds are thin socket shells; ALL the state logic lives here so it is unit-testable without a
socket: applying parsed poly/kalshi events to the books, Kalshi sequence-gap detection (drop the book
+ flag a resubscribe), tick-size tracking, and turning public trades into normalized prints the fill
model consumes. A ``rest_ref`` = (venue, identifier, side) uniquely names an outcome's book and is the
SAME tuple the quote driver arms a shadow quote with, so a print matches its quote.
"""
from __future__ import annotations

from typing import Optional

from .books import KalshiBook, PolyBook, SideView
from .parsing import seq_gap


class BookStore:
    def __init__(self) -> None:
        self.poly: dict = {}          # token -> PolyBook
        self.kalshi: dict = {}        # ticker -> KalshiBook
        self.kalshi_seq: dict = {}    # sid -> last applied orderbook seq (Kalshi seq is per-connection)
        self.tick: dict = {}          # token -> current tick size
        self._need_resync: bool = False   # a seq gap dropped ALL kalshi books -> full resubscribe

    # -- poly ---------------------------------------------------------------
    def apply_poly(self, events: list) -> list:
        """Apply parsed poly-market events; return public trade prints [(rest_ref, price, volume)]."""
        prints: list = []
        for e in events or []:
            k, token = e.get("kind"), e.get("token")
            if not token:
                continue
            if k == "poly_book":
                bk = self.poly.setdefault(token, PolyBook())
                bk.replace(e.get("bids") or {}, e.get("asks") or {})
            elif k == "poly_price":
                bk = self.poly.setdefault(token, PolyBook())
                for price, side, size in e.get("changes") or []:
                    bk.apply_change(price, "BID" if side in ("BUY", "BID", "YES") else "ASK", size)
            elif k == "poly_tick":
                if e.get("tick"):
                    self.tick[token] = e["tick"]
            elif k == "poly_trade":
                price, size = e.get("price"), e.get("size")
                if price is not None and size:
                    prints.append((("polymarket", token, "BUY"), float(price), float(size)))
        return prints

    def poly_view(self, token: str) -> Optional[SideView]:
        bk = self.poly.get(token)
        return bk.view() if bk else None

    def poly_tick(self, token: str, default: float = 0.01) -> float:
        return float(self.tick.get(token, default))

    # -- kalshi -------------------------------------------------------------
    def apply_kalshi(self, events: list) -> list:
        """Apply parsed Kalshi events; return public trade prints. The orderbook ``seq`` is per-sid and
        monotonic across ALL tickers, so a GAP means a message was missed SOMEWHERE -> drop every book
        on that connection and flag a full resubscribe (fresh snapshots heal it). Trade prints are
        emitted for BOTH sides (yes at yes_price, no at no_price) of each print (dollars)."""
        prints: list = []
        for e in events or []:
            k = e.get("kind")
            ticker = e.get("ticker")
            if k == "kalshi_snapshot" and ticker:
                bk = self.kalshi.setdefault(ticker, KalshiBook())
                bk.replace(e.get("yes") or [], e.get("no") or [], seq=e.get("seq"))
                self.kalshi_seq[e.get("sid")] = e.get("seq")
            elif k == "kalshi_delta" and ticker:
                sid = e.get("sid")
                prev = self.kalshi_seq.get(sid)
                if seq_gap(prev, e.get("seq")):
                    self.kalshi.clear()                      # drop EVERY book (seq is per-connection)
                    self.kalshi_seq.pop(sid, None)
                    self._need_resync = True                 # -> full resubscribe for fresh snapshots
                    continue
                bk = self.kalshi.get(ticker)
                if bk is not None and e.get("price") is not None and e.get("delta") is not None:
                    bk.apply_delta(e.get("side"), e.get("price"), e.get("delta"), seq=e.get("seq"))
                self.kalshi_seq[sid] = e.get("seq")
            elif k == "kalshi_trade" and ticker:
                cnt = e.get("count")
                if cnt:
                    yp, np_ = e.get("yes_price"), e.get("no_price")
                    if yp is not None:
                        prints.append((("kalshi", ticker, "yes"), float(yp), float(cnt)))
                    if np_ is not None:
                        prints.append((("kalshi", ticker, "no"), float(np_), float(cnt)))
        return prints

    def kalshi_view(self, ticker: str, side: str) -> Optional[SideView]:
        bk = self.kalshi.get(ticker)
        return bk.view(side) if bk else None

    def need_resync(self) -> bool:
        """True after a seq gap dropped the books -> the feed must resubscribe ALL tickers."""
        return self._need_resync

    def clear_resync(self) -> None:
        self._need_resync = False
