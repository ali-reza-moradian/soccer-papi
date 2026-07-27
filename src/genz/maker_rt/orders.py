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


KALSHI_COID_PREFIX = "mrt-"                    # client_order_id tag so our resting orders are recognizable


class KalshiOrderClient:
    """Kalshi order client: RESTING maker limits (rest_kalshi live direction) AND the IOC-emulated
    marketable hedge/unwind. ``exec_client`` is a src.executor.kalshi_exec.KalshiExec (or a fake) —
    place_order supports both a resting limit (time_in_force=None) and immediate_or_cancel."""

    def __init__(self, exec_client: Any, *, buffer: float = 0.01, log: Any = None) -> None:
        self.ex = exec_client
        self.buffer = buffer
        self.log = log
        self._open: dict[str, dict] = {}          # order_id -> {ticker, side, price, count, coid}
        self._n = 0

    def _coid(self) -> str:
        self._n += 1
        return f"{KALSHI_COID_PREFIX}{self._n}-{int(__import__('time').time()*1000)}"

    @staticmethod
    def clamp_count(count: float) -> int:
        """Whole contracts, at least the Kalshi minimum of 1."""
        return int(max(1, round(float(count))))

    # -- resting maker (rest_kalshi live direction) --------------------------
    def rest(self, ticker: str, side: str, price: float, count: float,
             *, client_order_id: Optional[str] = None) -> dict:
        """Post a RESTING maker limit BUY of outcome ``side`` (yes/no) at ``price`` dollars. v2 rests via
        time_in_force='good_till_canceled' + post_only=True (a true maker order); never-crossable is also
        enforced by the caller. Returns the executor's normalized {status, fill_count, avg_price, order_id}."""
        n = self.clamp_count(count)
        coid = client_order_id or self._coid()
        res = self.ex.place_order(ticker, side, n, float(price), action="buy",
                                  time_in_force="good_till_canceled", post_only=True,
                                  client_order_id=coid)
        oid = res.get("order_id")
        if oid:
            self._open[oid] = {"ticker": ticker, "side": side, "price": price, "count": n, "coid": coid}
        # Surface the client_order_id so the caller (the executor) can re-resolve this order by coid in
        # the resting list if a later single-order read comes back empty (the cancel verify-or-scream path).
        if isinstance(res, dict) and "client_order_id" not in res:
            res = dict(res, client_order_id=coid)
        return res

    def cancel(self, order_id: str) -> Any:
        self._open.pop(order_id, None)
        return self.ex.cancel_order(order_id) if hasattr(self.ex, "cancel_order") else None

    def cancel_all(self) -> int:
        ids = list(self._open)
        for oid in ids:
            try:
                self.cancel(oid)
            except Exception as exc:  # noqa: BLE001 - cancel-all must never raise on shutdown
                if self.log:
                    self.log.warning("[MAKER_RT] kalshi cancel %s failed: %s", oid, exc)
        return len(ids)

    def open_order_ids(self) -> list:
        return list(self._open)

    def order_status(self, order_id: str) -> dict:
        """AUTHORITATIVE single-order read: prefers KalshiExec.get_order (GET /portfolio/orders/{id}),
        falling back to a get_orders scan. Returns the order dict (or {} if truly absent). Used for
        cancel confirmation + the REST backup fill poll."""
        if hasattr(self.ex, "get_order"):
            try:
                o = self.ex.get_order(order_id)
                if isinstance(o, dict) and o:
                    return o
            except Exception:  # noqa: BLE001 — fall through to the list scan
                pass
        try:
            resp = self.ex.get_orders() if hasattr(self.ex, "get_orders") else None
        except Exception:  # noqa: BLE001
            return {}
        orders = resp.get("orders") if isinstance(resp, dict) else (resp or [])
        for o in orders or []:
            if isinstance(o, dict) and (o.get("order_id") == order_id or o.get("id") == order_id):
                return o
        return {}

    def resting_orders(self, ticker: Optional[str] = None) -> list:
        """OUR resting orders (client_order_id prefix ``mrt-``), optionally scoped to one ``ticker``.
        Backs the cancel re-resolve + the pre-placement stack guard. Propagates a read error (the caller
        FAILS CLOSED — an unreadable venue is treated as "still resting", never "gone")."""
        if not hasattr(self.ex, "list_resting"):
            return []
        rows = self.ex.list_resting(ticker=ticker) or []
        return [o for o in rows if isinstance(o, dict)
                and str(o.get("client_order_id") or "").startswith(KALSHI_COID_PREFIX)]

    def find_resting(self, *, order_id: Optional[str] = None, client_order_id: Optional[str] = None,
                     ticker: Optional[str] = None) -> Optional[dict]:
        """The resting order matching ``order_id`` OR ``client_order_id`` (None if positively absent from
        the resting list). Raises on a read failure so the caller can fail closed."""
        if not hasattr(self.ex, "list_resting"):
            return None
        for o in (self.ex.list_resting(ticker=ticker) or []):
            if not isinstance(o, dict):
                continue
            if order_id and (o.get("order_id") == order_id or o.get("id") == order_id):
                return o
            if client_order_id and o.get("client_order_id") == client_order_id:
                return o
        return None

    def fills_since(self, min_ts: int) -> list:
        """WS-INDEPENDENT fill authority: every account fill since ``min_ts`` (one call covers all open
        orders). Empty list when the venue read fails — the caller treats that as 'no news', never as
        'no fills'."""
        if not hasattr(self.ex, "get_fills"):
            return []
        try:
            return list(self.ex.get_fills(min_ts=min_ts) or [])
        except Exception as exc:  # noqa: BLE001
            if self.log:
                self.log.warning("[MAKER_RT] kalshi fills poll failed: %s", exc)
            return []

    # -- marketable hedge / unwind (both directions) -------------------------
    def marketable_limit(self, best_ask: float) -> float:
        """Cross by one buffer tick so the IOC lifts immediately; clamp inside (0,1)."""
        return min(0.99, max(0.01, float(best_ask) + self.buffer))

    def ioc_buy(self, ticker: str, side: str, count: int, best_ask: float,
                client_order_id: Optional[str] = None) -> dict:
        limit = self.marketable_limit(best_ask)
        return self.ex.place_order(ticker, side, int(count), limit,
                                   time_in_force="immediate_or_cancel",
                                   client_order_id=client_order_id or self._coid())
