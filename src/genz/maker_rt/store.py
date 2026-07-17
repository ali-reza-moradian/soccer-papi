"""BookStore — the in-memory books for the whole quotable universe + event application.

The feeds are thin socket shells; ALL the state logic lives here so it is unit-testable without a
socket: applying parsed poly/kalshi events to the books, Kalshi sequence-gap detection (drop the book
+ flag a resubscribe), tick-size tracking, and turning public trades into normalized prints the fill
model consumes. A ``rest_ref`` = (venue, identifier, side) uniquely names an outcome's book and is the
SAME tuple the quote driver arms a shadow quote with, so a print matches its quote.
"""
from __future__ import annotations

import time
from typing import Optional

from .books import KalshiBook, PolyBook, SideView
from .parsing import seq_gap

_MID_WINDOW_S = 60.0              # seconds of per-book mid history kept for shock detection


class BookStore:
    def __init__(self) -> None:
        self.poly: dict = {}          # token -> PolyBook
        self.kalshi: dict = {}        # ticker -> KalshiBook
        self.kalshi_seq: dict = {}    # sid -> last applied orderbook seq (Kalshi seq is per-connection)
        self.tick: dict = {}          # token -> current tick size
        self._need_resync: bool = False   # a seq gap dropped ALL kalshi books -> full resubscribe
        # IN-PLAY rails support: per-book freshness + a short mid history (identifier = token | ticker).
        self.updated: dict = {}       # identifier -> last-update wall-clock ts
        self.mid_hist: dict = {}      # identifier -> [(ts, mid), ...] within _MID_WINDOW_S

    def _touch(self, identifier: str, now_ts: float, mid: Optional[float]) -> None:
        """Record a book update: freshness ts + (if known) the mid, pruned to the mid window."""
        self.updated[identifier] = now_ts
        if mid is None:
            return
        h = self.mid_hist.setdefault(identifier, [])
        h.append((now_ts, mid))
        cutoff = now_ts - _MID_WINDOW_S
        while h and h[0][0] < cutoff:
            h.pop(0)

    def is_fresh(self, identifier: str, now_ts: float, fresh_s: float) -> bool:
        """True if ``identifier``'s book updated within ``fresh_s`` seconds of ``now_ts``."""
        t = self.updated.get(identifier)
        return t is not None and (now_ts - t) <= fresh_s

    def mid_move(self, identifier: str, now_ts: float, window_s: float) -> float:
        """The max mid RANGE (max−min) over the last ``window_s`` seconds — a spike-and-revert still
        registers. 0 when fewer than two samples in the window."""
        h = self.mid_hist.get(identifier)
        if not h:
            return 0.0
        recent = [m for (t, m) in h if t >= now_ts - window_s]
        if len(recent) < 2:
            return 0.0
        return max(recent) - min(recent)

    @staticmethod
    def _mid(v: Optional[SideView]) -> Optional[float]:
        if v is None or v.best_bid is None or v.best_ask is None:
            return None
        return (v.best_bid + v.best_ask) / 2.0

    # -- poly ---------------------------------------------------------------
    def apply_poly(self, events: list, now_ts: Optional[float] = None) -> list:
        """Apply parsed poly-market events; return public trade prints [(rest_ref, price, volume)].
        Records per-token freshness + mid history (for the in-play rails)."""
        prints: list = []
        now_ts = time.time() if now_ts is None else now_ts
        for e in events or []:
            k, token = e.get("kind"), e.get("token")
            if not token:
                continue
            if k == "poly_book":
                bk = self.poly.setdefault(token, PolyBook())
                bk.replace(e.get("bids") or {}, e.get("asks") or {})
                self._touch(token, now_ts, self._mid(bk.view()))
            elif k == "poly_price":
                bk = self.poly.setdefault(token, PolyBook())
                for price, side, size in e.get("changes") or []:
                    bk.apply_change(price, "BID" if side in ("BUY", "BID", "YES") else "ASK", size)
                self._touch(token, now_ts, self._mid(bk.view()))
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
    def apply_kalshi(self, events: list, now_ts: Optional[float] = None) -> list:
        """Apply parsed Kalshi events; return public trade prints. The orderbook ``seq`` is per-sid and
        monotonic across ALL tickers, so a GAP means a message was missed SOMEWHERE -> drop every book
        on that connection and flag a full resubscribe (fresh snapshots heal it). Trade prints are
        emitted for BOTH sides (yes at yes_price, no at no_price) of each print (dollars). Records
        per-ticker freshness + mid history (YES-side mid; NO mid is its complement)."""
        prints: list = []
        now_ts = time.time() if now_ts is None else now_ts
        for e in events or []:
            k = e.get("kind")
            ticker = e.get("ticker")
            if k == "kalshi_snapshot" and ticker:
                bk = self.kalshi.setdefault(ticker, KalshiBook())
                bk.replace(e.get("yes") or [], e.get("no") or [], seq=e.get("seq"))
                self.kalshi_seq[e.get("sid")] = e.get("seq")
                self._touch(ticker, now_ts, self._mid(bk.view("yes")))
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
                    self._touch(ticker, now_ts, self._mid(bk.view("yes")))
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
