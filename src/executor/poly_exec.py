"""Polymarket CLOB TRADING adapter (Phase 1) — authenticated order placement + unwind.

Separate from src/polymarket.py (the read-only, no-auth Gamma+CLOB market-data source). This
module signs and posts orders via py-clob-client. Ported to the proven sibling-bot patterns:

  * Wallet resolution: signer from POLYGON_PRIVATE_KEY; signature_type from POLY_SIGNATURE_TYPE
    (default 3 = deposit/proxy wallet); funder from POLY_FUNDER_ADDRESS or the derived
    deposit/proxy wallet. L2 creds are derived ONCE and the ClobClient is cached (lru_cache).
  * can_place_polymarket_orders() preflight that catches the known deposit-wallet/EOA mismatch
    ("signer address has to be the address of the API KEY" / "maker address not allowed") and
    returns (False, <clear fix message>).
  * place_order wraps create_and_post_order; FOK by default; tick_size and neg_risk are
    RE-FETCHED for the token at call time (cached values are not trusted). Returns a normalized
    {status, shares, usd, avg_price, order_id, raw}.

py-clob-client / eth_account are imported LAZILY so this module (and its pure helpers) import
cleanly in test/CI environments without the trading SDK installed.
"""
from __future__ import annotations

import math
import os
from functools import lru_cache
from typing import Any, Optional

POLYGON_CHAIN_ID = 137
CLOB_HOST = "https://clob.polymarket.com"

# Substrings of the two known deposit-wallet/EOA-mismatch errors we surface as a clear preflight fail.
_WALLET_MISMATCH_HINTS = (
    "signer address has to be the address of the api key",
    "maker address not allowed",
)


class PolyExecError(Exception):
    """Any failure talking to the Polymarket CLOB trading API."""


# --------------------------------------------------------------------------- #
# Pure helpers (no SDK) — unit-tested directly                                   #
# --------------------------------------------------------------------------- #
def min_poly_shares(price: float) -> int:
    """Minimum share count to clear Polymarket's ~$1 minimum order at ``price``: ceil(1.05/price).
    (The 1.05 gives a small cushion over the hard $1 floor.)"""
    p = max(float(price), 1e-9)
    return max(1, math.ceil(1.05 / p))


def is_wallet_mismatch_error(msg: str) -> bool:
    """True if ``msg`` is the known deposit-wallet/EOA signer mismatch."""
    m = str(msg).lower()
    return any(h in m for h in _WALLET_MISMATCH_HINTS)


def _best_ask(book: Any) -> Optional[tuple[float, float]]:
    """(price, size) at the lowest ask of a CLOB book, or None. Mirrors src.polymarket.best_ask
    but kept local so this trading module never imports the scanner's data module."""
    best: Optional[tuple[float, float]] = None
    for lvl in (book or {}).get("asks") or []:
        try:
            if isinstance(lvl, dict):
                p, s = float(lvl.get("price")), float(lvl.get("size"))
            else:
                p, s = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0.0 < p < 1.0 and s > 0 and (best is None or p < best[0]):
            best = (p, s)
    return best


def _ask_levels(book: Any) -> list[tuple[float, float]]:
    """Normalized ascending (price, size) ask ladder to BUY this token — what the engine walks."""
    levels: list[tuple[float, float]] = []
    for lvl in (book or {}).get("asks") or []:
        try:
            if isinstance(lvl, dict):
                p, s = float(lvl.get("price")), float(lvl.get("size"))
            else:
                p, s = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0.0 < p < 1.0 and s > 0:
            levels.append((p, s))
    levels.sort(key=lambda x: x[0])
    return levels


# --------------------------------------------------------------------------- #
# Wallet resolution + cached client                                             #
# --------------------------------------------------------------------------- #
def resolve_wallet() -> dict[str, Any]:
    """Resolve signer/funder/signature_type from env WITHOUT importing the SDK where avoidable.

    Returns {private_key, signer_address, funder, signature_type}. signer_address is derived from
    POLYGON_PRIVATE_KEY via eth_account (lazy import); funder defaults to POLY_FUNDER_ADDRESS or
    the signer address. Raises PolyExecError if the private key is absent."""
    pk = os.environ.get("POLYGON_PRIVATE_KEY")
    if not pk:
        raise PolyExecError("POLYGON_PRIVATE_KEY not set.")
    sig_type = int(os.environ.get("POLY_SIGNATURE_TYPE", "3"))
    signer_address: Optional[str] = None
    try:
        from eth_account import Account
        signer_address = Account.from_key(pk).address
    except ImportError:  # pragma: no cover - SDK missing in CI
        signer_address = None
    funder = os.environ.get("POLY_FUNDER_ADDRESS") or signer_address
    return {
        "private_key": pk,
        "signer_address": signer_address,
        "funder": funder,
        "signature_type": sig_type,
    }


@lru_cache(maxsize=1)
def _cached_client() -> Any:
    """Build + cache the authenticated ClobClient ONCE (derives L2 creds). Lazy SDK import."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError as exc:  # pragma: no cover - SDK missing
        raise PolyExecError("py-clob-client not installed — pip install py-clob-client.") from exc

    w = resolve_wallet()
    client = ClobClient(
        CLOB_HOST,
        key=w["private_key"],
        chain_id=POLYGON_CHAIN_ID,
        signature_type=w["signature_type"],
        funder=w["funder"],
    )
    # Derive L2 api creds once and attach them (idempotent: create-or-derive).
    try:
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
    except Exception as exc:  # pragma: no cover - network/cred error surfaced to caller
        raise PolyExecError(f"failed to derive Polymarket L2 creds: {exc}") from exc
    return client


def clear_client_cache() -> None:
    """Drop the cached ClobClient (used by tests / after a credential change)."""
    _cached_client.cache_clear()


# --------------------------------------------------------------------------- #
# Adapter                                                                        #
# --------------------------------------------------------------------------- #
class PolyExec:
    """Authenticated Polymarket CLOB trading client. Inject ``client`` for tests; in production it
    lazily builds and caches the real ClobClient from env."""

    def __init__(self, *, client: Any = None, log: Any = None) -> None:
        self._client = client
        self.log = log

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = _cached_client()
        return self._client

    # -- preflight -----------------------------------------------------------
    def can_place_polymarket_orders(self) -> tuple[bool, str]:
        """Preflight: confirm the configured wallet can actually MAKE orders. Returns (ok, reason).
        Catches the known deposit-wallet/EOA mismatch and returns a clear fix message."""
        try:
            w = resolve_wallet()
        except PolyExecError as exc:
            return False, str(exc)
        try:
            client = self.client
            # A tiny authenticated read that exercises the signer/api-key pairing.
            client.get_api_keys()
        except Exception as exc:  # noqa: BLE001 - surface any auth failure as a reason string
            msg = str(exc)
            if is_wallet_mismatch_error(msg):
                return False, (
                    "Polymarket signer/deposit-wallet mismatch: the API key must belong to the "
                    "signer address. Fix: set POLY_FUNDER_ADDRESS to your deposit/proxy wallet and "
                    "POLY_SIGNATURE_TYPE correctly (3 = deposit wallet), or re-derive API creds for "
                    f"signer {w.get('signer_address')}. Underlying error: {msg}")
            return False, f"Polymarket preflight failed: {msg}"
        return True, f"OK (signer={w.get('signer_address')}, funder={w.get('funder')}, sig_type={w.get('signature_type')})"

    # -- token metadata (re-fetched at call time) ----------------------------
    def _tick_and_negrisk(self, token_id: str) -> tuple[float, bool]:
        """RE-FETCH tick_size and neg_risk for ``token_id`` at call time (never trust cached)."""
        tick = float(self.client.get_tick_size(token_id))
        neg = bool(self.client.get_neg_risk(token_id))
        return tick, neg

    # -- orders --------------------------------------------------------------
    def place_order(self, token_id: str, price: float, size: float, side: str = "BUY", *,
                    order_type: str = "FOK", tick_size: Optional[float] = None,
                    neg_risk: Optional[bool] = None) -> dict[str, Any]:
        """Place a CLOB order (FOK by default). tick_size / neg_risk are re-fetched if not given.
        Returns normalized {status, shares, usd, avg_price, order_id, raw}."""
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        if tick_size is None or neg_risk is None:
            tick_size, neg_risk = self._tick_and_negrisk(token_id)
        side_const = BUY if str(side).upper() == "BUY" else SELL
        args = OrderArgs(token_id=token_id, price=float(price), size=float(size), side=side_const)
        signed = self.client.create_order(args, options={"tick_size": tick_size, "neg_risk": neg_risk})
        ot = getattr(OrderType, order_type, order_type)
        raw = self.client.post_order(signed, ot)
        return self._normalize(raw, price=price, requested_shares=size)

    def place_market_sell(self, token_id: str, shares: float, *, price: Optional[float] = None,
                          order_type: str = "FOK") -> dict[str, Any]:
        """UNWIND: sell ``shares`` of ``token_id``. If no price given, cross down to the best bid so
        the FOK sell is marketable."""
        if price is None:
            book = self.get_orderbook(token_id)
            bids = book.get("bids") or []
            price = float(bids[0][0]) if bids else 0.01
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL

        tick_size, neg_risk = self._tick_and_negrisk(token_id)
        args = OrderArgs(token_id=token_id, price=float(price), size=float(shares), side=SELL)
        signed = self.client.create_order(args, options={"tick_size": tick_size, "neg_risk": neg_risk})
        ot = getattr(OrderType, order_type, order_type)
        raw = self.client.post_order(signed, ot)
        return self._normalize(raw, price=price, requested_shares=shares)

    def _normalize(self, raw: Any, *, price: float, requested_shares: float) -> dict[str, Any]:
        """Map a post_order response to {status, shares, usd, avg_price, order_id, raw}.

        Polymarket FOK either fully fills or is killed; we classify strictly: shares filled == 0
        -> "none"; >= requested -> "filled"; otherwise "partial"."""
        d = raw if isinstance(raw, dict) else {}
        filled = None
        for k in ("size_matched", "filled_size", "matched_size", "makingAmount", "size"):
            if d.get(k) is not None:
                try:
                    filled = float(d[k])
                    break
                except (TypeError, ValueError):
                    continue
        success = d.get("success")
        if filled is None:
            filled = float(requested_shares) if success else 0.0
        avg = None
        for k in ("price", "avg_price", "average_price"):
            if d.get(k) is not None:
                try:
                    avg = float(d[k])
                    break
                except (TypeError, ValueError):
                    continue
        if avg is None:
            avg = float(price)
        if filled <= 0:
            status = "none"
        elif filled >= float(requested_shares) - 1e-9:
            status = "filled"
        else:
            status = "partial"
        if success is False:
            status = "none"
            filled = 0.0
        return {
            "status": status,
            "shares": filled,
            "usd": round(filled * avg, 6),
            "avg_price": avg,
            "order_id": d.get("orderID") or d.get("order_id") or d.get("id"),
            "raw": raw,
        }

    # -- reads ---------------------------------------------------------------
    def get_orderbook(self, token_id: str) -> dict[str, Any]:
        """Live CLOB book for ``token_id`` plus a normalized ascending ask ladder for BUYS."""
        raw = self.client.get_order_book(token_id)
        # py-clob returns an object with .asks/.bids of price/size objects; normalize generically.
        asks = _ask_levels(_book_as_dict(raw))
        bids = _bid_levels(_book_as_dict(raw))
        return {"asks": asks, "bids": bids, "raw": raw}

    def get_balance(self) -> Any:
        """USDC collateral balance/allowance for this wallet."""
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        return self.client.get_balance_allowance(params)


def _book_as_dict(raw: Any) -> dict[str, Any]:
    """Coerce a py-clob OrderBookSummary (or a plain dict) into {'asks': [...], 'bids': [...]}."""
    if isinstance(raw, dict):
        return raw
    out: dict[str, Any] = {}
    for side in ("asks", "bids"):
        levels = getattr(raw, side, None) or []
        out[side] = [
            {"price": getattr(l, "price", None) if not isinstance(l, dict) else l.get("price"),
             "size": getattr(l, "size", None) if not isinstance(l, dict) else l.get("size")}
            for l in levels
        ]
    return out


def _bid_levels(book: Any) -> list[tuple[float, float]]:
    """Descending (price, size) bid ladder to SELL into — used by the unwind market-sell."""
    levels: list[tuple[float, float]] = []
    for lvl in (book or {}).get("bids") or []:
        try:
            if isinstance(lvl, dict):
                p, s = float(lvl.get("price")), float(lvl.get("size"))
            else:
                p, s = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0.0 < p < 1.0 and s > 0:
            levels.append((p, s))
    levels.sort(key=lambda x: x[0], reverse=True)
    return levels
