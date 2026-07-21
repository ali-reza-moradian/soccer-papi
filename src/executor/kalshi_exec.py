"""Kalshi REST TRADING adapter (Phase 1) — authenticated order placement + unwind.

Separate from src/kalshi.py (which is the read-only, no-auth market-data source). This module
signs requests (RSA-PSS) and places/cancels orders. Ported to the proven sibling-bot patterns:

  * Auth: RSA-PSS / SHA-256 / MGF1-SHA256, salt length == digest length. Sign the string
    f"{timestamp_ms}{METHOD}{path}" with the query string stripped. Headers:
    KALSHI-ACCESS-KEY (api key id), KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE (b64).
    The RSA key is parsed ONCE at construction and cached (never reparsed per request).
  * place_order maps YES->bid@price, NO->ask@(1-price) on the YES book; count as "N.00",
    price as a 4-decimal string; sets self_trade_prevention; IOC by default; retries 429.
  * Fill classification is STRICT: "filled" ONLY when fill_count == requested; "partial" when
    0 < fill < count; "none" when 0. A resting/accepted order is NEVER reported as "filled".

The pure helpers (price mapping, count/price formatting, fill classification) are import-safe
with no third-party deps, so they are unit-tested directly; crypto + HTTP are lazy/injectable.
"""
from __future__ import annotations

import base64
import math
import os
import time
from typing import Any, Callable, Optional

DEFAULT_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiExecError(Exception):
    """Any failure talking to the Kalshi trading API."""


class KalshiAuthError(KalshiExecError):
    """Missing/invalid credentials or signing failure."""


# --------------------------------------------------------------------------- #
# Pure helpers (no network, no crypto) — unit-tested directly                    #
# --------------------------------------------------------------------------- #
def classify_fill(requested: int, filled: int) -> str:
    """STRICT status from requested vs actually-filled count.

    "filled" ONLY when filled == requested; "partial" when 0 < filled < requested; "none" when
    filled <= 0. A resting/accepted-but-unfilled order is "none", never "filled"."""
    if requested <= 0:
        return "none"
    if filled <= 0:
        return "none"
    if filled >= requested:
        return "filled"
    return "partial"


def yes_book_price(side: str, price: float) -> float:
    """Price to use ON THE YES BOOK for the given side.

    We always BACK (buy) an outcome: YES -> bid @ price; NO -> the equivalent YES-book price is
    (1 - price) (buying NO @ price == selling/quoting YES @ 1-price). Clamped to (0,1)."""
    s = str(side).strip().upper()
    if s not in ("YES", "NO"):
        raise KalshiExecError(f"side must be YES or NO, got {side!r}")
    p = price if s == "YES" else (1.0 - price)
    return p


def fmt_count(count: int) -> str:
    """Kalshi wire format for an integer contract count: 'N.00'."""
    return f"{int(count)}.00"


def fmt_price(price: float) -> str:
    """Kalshi wire format for a price (dollars in (0,1)) as a 4-decimal string, clamped [0.0001,0.9999]."""
    p = min(max(float(price), 0.0001), 0.9999)
    return f"{p:.4f}"


def _avg_price_cents_to_dollars(v: Any) -> Optional[float]:
    """Kalshi reports fill prices in integer cents; normalize to dollars in (0,1). None if absent."""
    try:
        c = float(v)
    except (TypeError, ValueError):
        return None
    return c / 100.0 if c > 1.0 else c   # tolerate already-dollar responses in mocks


def _ask_levels_from_orderbook(book: dict[str, Any], side: str) -> list[tuple[float, float]]:
    """Normalized ascending (price_dollars, size) ladder you would CONSUME to BUY ``side``.

    Kalshi's orderbook gives resting YES bids and NO bids as [[price_cents, size], ...]. To BUY
    YES you lift the cheapest available YES offers, which are the complements of resting NO bids:
    a NO bid at c cents implies a YES offer at (100 - c) cents. Symmetric for buying NO."""
    ob = (book or {}).get("orderbook", book) or {}
    opp = "no" if str(side).upper() == "YES" else "yes"
    levels: list[tuple[float, float]] = []
    for lvl in ob.get(opp) or []:
        try:
            c, size = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        price = (100.0 - c) / 100.0
        if 0.0 < price < 1.0 and size > 0:
            levels.append((price, size))
    levels.sort(key=lambda x: x[0])
    return levels


# --------------------------------------------------------------------------- #
# Signing (RSA-PSS) — key parsed ONCE                                            #
# --------------------------------------------------------------------------- #
def _load_private_key():
    """Parse the RSA private key ONCE from env. Supports KALSHI_PRIVATE_KEY (PEM contents) or
    KALSHI_PRIVATE_KEY_PATH / KALSHI_PRIVATE_KEY_FILE (path to a PEM). Lazy-imports cryptography."""
    pem: Optional[bytes] = None
    if os.environ.get("KALSHI_PRIVATE_KEY"):
        pem = os.environ["KALSHI_PRIVATE_KEY"].encode()
    else:
        path = os.environ.get("KALSHI_PRIVATE_KEY_PATH") or os.environ.get("KALSHI_PRIVATE_KEY_FILE")
        if path and os.path.exists(path):
            with open(path, "rb") as fh:
                pem = fh.read()
    if not pem:
        raise KalshiAuthError(
            "No Kalshi private key: set KALSHI_PRIVATE_KEY (PEM contents) or KALSHI_PRIVATE_KEY_PATH.")
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise KalshiAuthError("cryptography not installed — pip install cryptography.") from exc
    return load_pem_private_key(pem, password=None)


def make_rsa_signer(private_key=None) -> Callable[[str], str]:
    """Return a signer(msg)->base64 signature using RSA-PSS / SHA-256 / MGF1-SHA256 with salt
    length == digest length. The key object is bound once (not reparsed per call)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    key = private_key if private_key is not None else _load_private_key()

    def _sign(msg: str) -> str:
        sig = key.sign(
            msg.encode("utf-8"),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256.digest_size),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("ascii")

    return _sign


# --------------------------------------------------------------------------- #
# Adapter                                                                        #
# --------------------------------------------------------------------------- #
class KalshiExec:
    """Authenticated Kalshi trading client. Inject ``session`` and/or ``signer`` for tests; in
    production both are built from env (KALSHI_API_KEY_ID + the RSA key) at construction time."""

    def __init__(self, *, api_base: str = DEFAULT_API_BASE, api_key_id: Optional[str] = None,
                 signer: Optional[Callable[[str], str]] = None, session: Any = None,
                 timeout: float = 20.0, max_retries: int = 4, log: Any = None) -> None:
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.api_key_id = api_key_id or os.environ.get("KALSHI_API_KEY_ID") or os.environ.get("KALSHI_ACCESS_KEY")
        self._signer = signer            # parsed-once RSA signer; lazily built on first use if None
        self._session = session
        self.timeout = timeout
        self.max_retries = max_retries
        self.log = log

    # -- internals -----------------------------------------------------------
    def _ensure_signer(self) -> Callable[[str], str]:
        if self._signer is None:
            self._signer = make_rsa_signer()
        return self._signer

    def _ensure_session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def _path(self, endpoint: str) -> str:
        """The signed path = everything after the host, query stripped."""
        from urllib.parse import urlsplit
        full = self.api_base + endpoint
        return urlsplit(full).path

    def _headers(self, method: str, endpoint: str) -> dict[str, str]:
        if not self.api_key_id:
            raise KalshiAuthError("KALSHI_API_KEY_ID not set.")
        ts = str(int(time.time() * 1000))
        path = self._path(endpoint)                 # query string already stripped
        msg = f"{ts}{method.upper()}{path}"
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._ensure_signer()(msg),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, endpoint: str, *, json_body: Any = None,
                 params: Any = None) -> Any:
        """Signed request with exponential backoff on HTTP 429."""
        sess = self._ensure_session()
        url = self.api_base + endpoint
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            headers = self._headers(method, endpoint)
            resp = sess.request(method, url, headers=headers, json=json_body, params=params,
                                timeout=self.timeout)
            status = getattr(resp, "status_code", 200)
            if status == 429:
                wait = min(2 ** attempt, 8)
                if self.log:
                    self.log.warning("[KALSHI-EXEC] 429 on %s %s — backoff %ss (attempt %d).",
                                     method, endpoint, wait, attempt + 1)
                last_exc = KalshiExecError(f"429 on {endpoint}")
                time.sleep(wait)
                continue
            if status >= 400:
                text = getattr(resp, "text", "")
                raise KalshiExecError(f"{status} on {method} {endpoint}: {text[:300]}")
            return resp.json()
        raise last_exc or KalshiExecError(f"exhausted retries on {endpoint}")

    # -- orders --------------------------------------------------------------
    def place_order(self, ticker: str, side: str, count: int, price: float, *,
                    time_in_force: str = "immediate_or_cancel",
                    client_order_id: Optional[str] = None) -> dict[str, Any]:
        """Place a BACK (buy) order. YES->bid@price, NO->ask@(1-price) on the YES book. Returns a
        normalized {status, fill_count, avg_price, order_id, raw} with STRICT fill classification."""
        book_price = yes_book_price(side, price)
        body = {
            "ticker": ticker,
            "action": "buy",
            "side": str(side).lower(),
            "count": fmt_count(count),
            "type": "limit",
            "price": fmt_price(book_price),
            "yes_price": fmt_price(book_price) if str(side).upper() == "YES" else None,
            "time_in_force": time_in_force,
            "self_trade_prevention": "cancel_resting",
            "client_order_id": client_order_id or f"exec-{int(time.time()*1000)}",
        }
        body = {k: v for k, v in body.items() if v is not None}
        raw = self._request("POST", "/portfolio/orders", json_body={"order": body})
        return self._normalize_order_response(raw, count)

    def place_market_sell(self, ticker: str, side: str, count: int, *,
                          client_order_id: Optional[str] = None) -> dict[str, Any]:
        """UNWIND: market-sell ``count`` contracts of ``side`` to flatten an unhedged position."""
        body = {
            "ticker": ticker,
            "action": "sell",
            "side": str(side).lower(),
            "count": fmt_count(count),
            "type": "market",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention": "cancel_resting",
            "client_order_id": client_order_id or f"unwind-{int(time.time()*1000)}",
        }
        raw = self._request("POST", "/portfolio/orders", json_body={"order": body})
        return self._normalize_order_response(raw, count)

    def cancel_order(self, order_id: str) -> Any:
        return self._request("DELETE", f"/portfolio/orders/{order_id}")

    def _normalize_order_response(self, raw: Any, requested: int) -> dict[str, Any]:
        order = (raw or {}).get("order", raw) if isinstance(raw, dict) else {}
        filled = 0
        for k in ("fill_count", "filled_count", "taker_fill_count", "count_filled"):
            if order.get(k) is not None:
                filled = int(order[k])
                break
        avg = None
        for k in ("avg_fill_price", "average_fill_price", "fill_price", "avg_price"):
            if order.get(k) is not None:
                avg = _avg_price_cents_to_dollars(order[k])
                break
        return {
            "status": classify_fill(requested, filled),
            "fill_count": filled,
            "avg_price": avg,
            "order_id": order.get("order_id") or order.get("id"),
            "raw": raw,
        }

    # -- reads ---------------------------------------------------------------
    def get_orderbook(self, ticker: str, *, side: str = "YES", depth: Optional[int] = None) -> dict[str, Any]:
        """Live order book for ``ticker``, plus a normalized ascending ask ladder (price_dollars,
        size) for BUYING ``side`` — the ladder the dry-run engine walks."""
        params = {"depth": depth} if depth is not None else None
        raw = self._request("GET", f"/markets/{ticker}/orderbook", params=params)
        return {"asks": _ask_levels_from_orderbook(raw, side), "raw": raw}

    def get_balance(self) -> Any:
        return self._request("GET", "/portfolio/balance")

    def get_positions(self) -> Any:
        return self._request("GET", "/portfolio/positions")

    def get_orders(self, *, status: Optional[str] = None, ticker: Optional[str] = None) -> Any:
        """List orders (optionally by status e.g. 'resting' / ticker). Used by the startup stray-order
        sweep to find + cancel any of ours (client_order_id prefix) left resting by a previous run."""
        params = {k: v for k, v in (("status", status), ("ticker", ticker)) if v}
        return self._request("GET", "/portfolio/orders", params=params or None)
