"""Order clients for the LIVE maker/hedger — BUILT and unit-tested, used ONLY when armed.

PolyOrderClient rests GTC maker orders (Polymarket has NO post-only flag and GTD carries a ~1min
venue buffer, so we use GTC + an explicit cancel), tick-aware and min-5-share aware, with cancel /
cancel-all. KalshiOrderClient lifts the hedge with an IOC-emulated marketable limit (limit at the ask
+ buffer, immediate-or-cancel). Both delegate to the authenticated executor adapters (src/executor/
poly_exec, kalshi_exec) which are only constructed when the live gate is open; injected fakes drive
the tests. NOTHING here is reached unless the caller has already armed.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from .quotes import POLY_MIN_SHARES


class PolyOrderClient:
    """Tick-/min-share-aware GTC resting on Polymarket. ``exec_client`` is a src.executor.poly_exec
    .PolyExec (or a fake). Never trusts cached tick/neg_risk — they are fetched at rest time."""

    def __init__(self, exec_client: Any, *, log: Any = None) -> None:
        self.ex = exec_client
        self.log = log
        self._open: dict[str, dict] = {}          # order_id -> {token_id, price, size}

    @staticmethod
    def clamp_size(size: float, price: float, tick: float = 0.01) -> float:
        """At least the venue minimum (5 shares) and >= $1 notional; whole shares."""
        n = max(float(size), float(POLY_MIN_SHARES))
        min_for_dollar = math.ceil(1.0 / max(price, 1e-9))
        return float(max(int(round(n)), POLY_MIN_SHARES, min_for_dollar))

    def rest(self, token_id: str, price: float, size: float, *, tick_size: Optional[float] = None,
             neg_risk: Optional[bool] = None) -> dict:
        """Post a GTC resting BUY. Returns the executor's normalized result (incl. order_id)."""
        size = self.clamp_size(size, price, tick_size or 0.01)
        res = self.ex.place_order(token_id, price, size, "BUY", order_type="GTC",
                                  tick_size=tick_size, neg_risk=neg_risk)
        oid = res.get("order_id")
        if oid:
            self._open[oid] = {"token_id": token_id, "price": price, "size": size}
        return res

    def cancel(self, order_id: str) -> Any:
        self._open.pop(order_id, None)
        return self.ex.cancel_order(order_id) if hasattr(self.ex, "cancel_order") else None

    def cancel_all(self) -> int:
        """Cancel every tracked open order (best-effort). Returns the count attempted."""
        ids = list(self._open)
        for oid in ids:
            try:
                self.cancel(oid)
            except Exception as exc:  # noqa: BLE001 - cancel-all must never raise on shutdown
                if self.log:
                    self.log.warning("[MAKER_RT] poly cancel %s failed: %s", oid, exc)
        return len(ids)

    def open_order_ids(self) -> list:
        return list(self._open)


class KalshiOrderClient:
    """IOC-emulated marketable hedge on Kalshi. ``exec_client`` is a src.executor.kalshi_exec.KalshiExec
    (or a fake) — place_order already supports time_in_force='immediate_or_cancel'."""

    def __init__(self, exec_client: Any, *, buffer: float = 0.01, log: Any = None) -> None:
        self.ex = exec_client
        self.buffer = buffer
        self.log = log

    def marketable_limit(self, best_ask: float) -> float:
        """Cross by one buffer tick so the IOC lifts immediately; clamp inside (0,1)."""
        return min(0.99, max(0.01, float(best_ask) + self.buffer))

    def ioc_buy(self, ticker: str, side: str, count: int, best_ask: float,
                client_order_id: Optional[str] = None) -> dict:
        limit = self.marketable_limit(best_ask)
        return self.ex.place_order(ticker, side, int(count), limit,
                                   time_in_force="immediate_or_cancel", client_order_id=client_order_id)
