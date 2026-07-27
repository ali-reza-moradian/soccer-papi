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


# --------------------------------------------------------------------------- #
# V2 order encoding — PURE functions (the create-order-v2 book is YES-space)     #
# --------------------------------------------------------------------------- #
# Kalshi's v2 create-order endpoint (POST /portfolio/events/orders) speaks a single YES-space book:
# `side` is "bid" (buy YES) or "ask" (sell YES), and `price` is ALWAYS the YES price. Every outcome/
# action collapses onto that book. The dangerous case is the UNWIND: closing a YES position is a SELL of
# YES ("ask"), but closing a NO position is a BUY of YES ("bid") — a sign error there turns a closer into
# an opener, so these are pure + exhaustively unit-tested (v2_order_side / v2_yes_price).
def v2_order_side(outcome: str, action: str) -> str:
    """v2 book side for (outcome in {YES,NO}, action in {buy,sell}): "bid" = buy YES, "ask" = sell YES.
    buy NO == sell YES ("ask"); sell NO == buy YES ("bid")."""
    o = str(outcome).strip().upper()
    a = str(action).strip().lower()
    if o not in ("YES", "NO"):
        raise KalshiExecError(f"outcome must be YES or NO, got {outcome!r}")
    if a not in ("buy", "sell"):
        raise KalshiExecError(f"action must be buy or sell, got {action!r}")
    buys_yes = (o == "YES" and a == "buy") or (o == "NO" and a == "sell")
    return "bid" if buys_yes else "ask"


def v2_yes_price(outcome: str, price: float) -> float:
    """YES-space price for an order in ``outcome``'s own price terms: a YES order passes its price as-is;
    a NO order at price p is the YES price (1 - p) (direction is carried by bid/ask, NOT the price)."""
    o = str(outcome).strip().upper()
    if o not in ("YES", "NO"):
        raise KalshiExecError(f"outcome must be YES or NO, got {outcome!r}")
    return float(price) if o == "YES" else round(1.0 - float(price), 6)


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


def fp_num(row: Any, *names: str) -> Optional[float]:
    """Read a COUNT-ish numeric field off a Kalshi order/position/fill row across API generations.

    v2 suffixes contract counts with ``_fp`` and serializes them as fixed-point STRINGS ("50.00");
    v1 used the bare name with an int. Every read path must accept both — the v1->v2 order migration
    kept the bare v1 names on the READ side, so `fill_count` was permanently absent and every fill on a
    resting Kalshi order read as "no fill" (the 2026-07-23 invisible-fill incident). Tries each name,
    then each name + "_fp". Returns None when no variant is present."""
    if not isinstance(row, dict):
        return None
    for n in names:
        for cand in (n, f"{n}_fp"):
            v = row.get(cand)
            if v is None or v == "":
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _ask_levels_from_orderbook(book: dict[str, Any], side: str) -> list[tuple[float, float]]:
    """Normalized ascending (price_dollars, size) ladder you would CONSUME to BUY ``side``.

    Kalshi's orderbook gives resting YES bids and NO bids. To BUY YES you lift the cheapest available
    YES offers, which are the complements of resting NO bids: a NO bid at price p implies a YES offer
    at (1 - p). Symmetric for buying NO.

    TWO wire shapes are accepted, because the live v2 API serves the second one and reading only the
    first silently yields an EMPTY ladder (the same class of defect as the `_fp` count fields):
      v1: {"orderbook": {"yes"|"no": [[price_CENTS, size], ...]}}
      v2: {"orderbook_fp": {"yes_dollars"|"no_dollars": [["0.4100", "55221.00"], ...]}}"""
    src = book or {}
    opp = "no" if str(side).upper() == "YES" else "yes"
    ob = src.get("orderbook_fp")
    if isinstance(ob, dict) and (ob.get(f"{opp}_dollars") is not None or ob.get(opp) is not None):
        raw = ob.get(f"{opp}_dollars") or ob.get(opp) or []
        scale = 1.0                                   # already dollars
    else:
        ob = src.get("orderbook", src) or {}
        raw = ob.get(f"{opp}_dollars") or ob.get(opp) or []
        scale = 1.0 if ob.get(f"{opp}_dollars") is not None else 0.01   # bare name == cents
    levels: list[tuple[float, float]] = []
    for lvl in raw:
        try:
            p, size = float(lvl[0]) * scale, float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        price = 1.0 - p                               # complement: their bid -> our offer
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
                    action: str = "buy", time_in_force: str = "immediate_or_cancel",
                    post_only: bool = False, client_order_id: Optional[str] = None) -> dict[str, Any]:
        """Create a v2 order (POST /portfolio/events/orders). ``side`` = outcome YES|NO, ``action`` =
        buy|sell, ``price`` = price in the OUTCOME's own terms (dollars). Encoded onto the YES-space book
        via v2_order_side / v2_yes_price (the pure, tested mappers — buy NO = ask@(1-p), sell NO = bid@(1-p)).
        Returns a normalized {status, fill_count, avg_price, order_id, raw} with STRICT fill classification."""
        body = {
            "ticker": ticker,
            "side": v2_order_side(side, action),
            "count": fmt_count(count),
            "price": fmt_price(v2_yes_price(side, price)),
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id or f"exec-{int(time.time()*1000)}",
        }
        if post_only:
            body["post_only"] = True
        raw = self._request("POST", "/portfolio/events/orders", json_body=body)
        return self._normalize_order_response(raw, count)

    def place_market_sell(self, ticker: str, side: str, count: int, *,
                          client_order_id: Optional[str] = None) -> dict[str, Any]:
        """UNWIND: marketable IOC SELL of ``count`` contracts of the HELD outcome ``side`` (YES|NO) to
        flatten. Sells at an aggressive outcome-price (0.01) so it crosses; the v2 side is the UNWIND flip
        (sell YES -> ask, sell NO -> bid) done inside place_order/v2_order_side."""
        return self.place_order(ticker, side, count, 0.01, action="sell",
                                time_in_force="immediate_or_cancel",
                                client_order_id=client_order_id or f"unwind-{int(time.time()*1000)}")

    def cancel_order(self, order_id: str) -> Any:
        """v2 CancelOrder is ``DELETE /portfolio/orders/{id}`` — the ``events/`` segment belongs ONLY to
        the batch CREATE path (``POST /portfolio/events/orders``). Sending the DELETE to
        ``/portfolio/events/orders/{id}`` returned ``404 not_found`` for EVERY cancel, and the caller
        trusted that as "already gone" and stacked replacement orders that all filled (the 2026-07-25
        ghost-order incident on KXUFCFIGHT-26JUL25ZAYRZE). The reads (get_order/get_orders) already used
        ``/portfolio/orders``; the cancel now matches them."""
        return self._request("DELETE", f"/portfolio/orders/{order_id}")

    def _normalize_order_response(self, raw: Any, requested: int) -> dict[str, Any]:
        order = (raw or {}).get("order", raw) if isinstance(raw, dict) else {}
        # fp_num accepts BOTH the v1 bare names and the v2 "_fp" fixed-point strings ("3.00").
        n = fp_num(order, "fill_count", "filled_count", "taker_fill_count", "count_filled")
        if n is None:                                # last resort: initial - remaining
            init = fp_num(order, "initial_count", "count")
            rem = fp_num(order, "remaining_count")
            n = (init - rem) if (init is not None and rem is not None) else None
        filled = int(n) if n is not None else 0
        avg = None
        for k in ("average_fill_price", "avg_fill_price", "fill_price", "avg_price"):
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

    def get_settlements(self, *, limit: Optional[int] = None) -> Any:
        """Settled markets for this account (GET /portfolio/settlements) — per-ticker market_result +
        revenue. The settled-pnl reconciler nets this (the Kalshi leg's true payout) against the Poly
        redemption to write the venue-truth realized pnl of a hedged pair."""
        params = {"limit": limit} if limit is not None else None
        return self._request("GET", "/portfolio/settlements", params=params)

    def get_orders(self, *, status: Optional[str] = None, ticker: Optional[str] = None) -> Any:
        """List orders (optionally by status e.g. 'resting' / ticker). Used by the startup stray-order
        sweep to find + cancel any of ours (client_order_id prefix) left resting by a previous run."""
        params = {k: v for k, v in (("status", status), ("ticker", ticker)) if v}
        return self._request("GET", "/portfolio/orders", params=params or None)

    def list_resting(self, *, ticker: Optional[str] = None, limit: int = 200,
                     max_pages: int = 5) -> list[dict[str, Any]]:
        """Every RESTING order (optionally for one ``ticker``), cursor-paged. This is the AUTHORITATIVE
        "is this order still live?" read used by the cancel re-resolve (after a DELETE, confirm the order
        is actually terminal) and the pre-placement stack guard (adopt/cancel any of our orders resting on
        a market before adding another). Raises on a read failure so the caller can FAIL CLOSED (treat an
        unreadable venue as "still resting", never as "gone")."""
        out: list[dict[str, Any]] = []
        cursor = None
        for _ in range(max(1, max_pages)):
            params: dict[str, Any] = {"status": "resting", "limit": limit}
            if ticker:
                params["ticker"] = ticker
            if cursor:
                params["cursor"] = cursor
            raw = self._request("GET", "/portfolio/orders", params=params)
            page = (raw or {}).get("orders") if isinstance(raw, dict) else (raw or [])
            out.extend(o for o in (page or []) if isinstance(o, dict))
            cursor = (raw or {}).get("cursor") if isinstance(raw, dict) else None
            if not cursor:
                break
        return out

    def get_order(self, order_id: str) -> dict[str, Any]:
        """Single-order read (GET /portfolio/orders/{id}) — the AUTHORITATIVE per-order status. Falls back
        to a list scan on 404/error so a venue quirk degrades rather than blinds us. Returns {} if truly
        absent. NOTE: a FILLED order is NOT 'resting', so it must never be looked up with status='resting'
        (that is exactly how the 2026-07-23 invisible fills stayed invisible)."""
        try:
            raw = self._request("GET", f"/portfolio/orders/{order_id}")
            if isinstance(raw, dict):
                return raw.get("order", raw) or {}
        except Exception:  # noqa: BLE001 — fall through to the list scan
            pass
        try:
            resp = self.get_orders()
        except Exception:  # noqa: BLE001
            return {}
        orders = resp.get("orders") if isinstance(resp, dict) else (resp or [])
        for o in orders or []:
            if isinstance(o, dict) and (o.get("order_id") == order_id or o.get("id") == order_id):
                return o
        return {}

    def get_fills(self, *, min_ts: Optional[int] = None, ticker: Optional[str] = None,
                  order_id: Optional[str] = None, limit: int = 200,
                  max_pages: int = 10) -> list[dict[str, Any]]:
        """AUTHORITATIVE fill history (GET /portfolio/fills), cursor-paged. This is the same data the
        private WS 'fill' channel carries, so it is the WS-INDEPENDENT fill authority: one call covers
        every open order at once. ``min_ts`` is a unix-seconds lower bound."""
        params_base: dict[str, Any] = {"limit": limit}
        for k, v in (("min_ts", min_ts), ("ticker", ticker), ("order_id", order_id)):
            if v is not None:
                params_base[k] = v
        out: list[dict[str, Any]] = []
        cursor = None
        for _ in range(max(1, max_pages)):
            params = dict(params_base)
            if cursor:
                params["cursor"] = cursor
            raw = self._request("GET", "/portfolio/fills", params=params)
            page = (raw or {}).get("fills") if isinstance(raw, dict) else None
            if not page:
                break
            out.extend(x for x in page if isinstance(x, dict))
            cursor = (raw or {}).get("cursor")
            if not cursor:
                break
        return out
