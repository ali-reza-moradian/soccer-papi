"""Polymarket CLOB TRADING adapter (Phase 1) — authenticated order placement + unwind.

Separate from src/polymarket.py (the read-only, no-auth Gamma+CLOB market-data source — the scanner
and the dry-run book-fetch path use THAT, never this module). This module signs and posts orders via
py-clob-client-V2. v2 is required: py-clob-client v1 cannot read pUSD (Polymarket's post-April-2026
collateral) so get_balance returned $0 on a funded wallet; v2 reads the real pUSD collateral. Ported
to the proven sibling-bot patterns:

  * Wallet resolution: signer from POLYGON_PRIVATE_KEY; signature_type from POLY_SIGNATURE_TYPE
    (Polymarket supports 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE — there is NO 3; default 1 =
    POLY_PROXY, the email/magic deposit wallet most accounts use); funder from POLY_FUNDER_ADDRESS
    or the derived deposit/proxy wallet. L2 creds are derived ONCE and the ClobClient is cached
    (lru_cache). The client is built WITH that signature_type, which is what scopes balance reads to
    the funder.
  * can_place_polymarket_orders() preflight that catches the known deposit-wallet/EOA mismatch
    ("signer address has to be the address of the API KEY" / "maker address not allowed") and
    returns (False, <clear fix message>).
  * place_order builds an OrderArgs + v2 PartialCreateOrderOptions; FOK by default; tick_size and
    neg_risk are RE-FETCHED for the token at call time (cached values are not trusted). Returns a
    normalized {status, shares, usd, avg_price, order_id, raw}.

py_clob_client_v2 / eth_account are imported LAZILY so this module (and its pure helpers) import
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


def market_buy_price(price: float) -> float:
    """The whole-cent, in-range LIMIT for a marketable BUY.

    A BUY limit is a CEILING (execution still happens at the resting ask prices), so it rounds UP —
    rounding down would make the order non-marketable on a 0.001-tick book. Callers that pass a
    gate-approved cap already pass a whole cent (``hedge._apply_cap`` floors to the tick), so the
    ceiling is a no-op there and the cap is never exceeded."""
    p = math.ceil(max(0.0, float(price)) * 100.0 - 1e-9) / 100.0
    return min(0.99, max(0.01, p))


#: USDC decimals the venue accepts on a MARKET BUY's maker amount. This is the rule that 400'd the CSKA
#: hedge ("the market buy orders maker amount supports a max accuracy of 2 decimals"); the CLOB client's
#: own ``get_market_order_amounts`` applies exactly this floor (``round_down(amount, round_config.size)``,
#: and ``size`` is 2 for every tick size), which is why specifying the AMOUNT is the venue-legal way to
#: buy a share count and quantizing a share count against a price is not.
MARKET_BUY_AMOUNT_DP = 2


def market_buy_spend(target_shares: float, expected_price: float) -> tuple[float, float]:
    """(usdc_to_spend, shares_that_buys) for a market BUY of ``target_shares``. PURE.

    THE OVERSHOOT FIX (2026-08-04). A Polymarket market BUY is denominated in USDC: the venue spends the
    WHOLE maker amount and hands back ``amount / fill_price`` shares, so price improvement returns as
    EXTRA SHARES, never as a refund. The old sizing sent ``floor(target) x (best_ask + 2 ticks)`` worth of
    dollars, which meant the 2-tick marketability pad came straight back as unhedged shares — overshoot =
    ``2 ticks / fill price``, i.e. +5.6% at 36c and **+40% at 5c**. Five of five rest-kalshi hedges
    overshot; 26AUG04JEJBMU O/U5.5 rode 6.5722 shares naked and lost $2.27 against the pair's +$1.80.

    So the spend is computed from the price we actually expect to CLEAR at (the walked ask ladder), not
    from the padded limit — the pad stays on the LIMIT, where it belongs, and buys marketability without
    buying shares. Flooring to the venue's cent keeps ``expected_shares <= target_shares`` by
    construction: the only shortfall is the sub-cent remainder the venue's own amount precision forces,
    which is ``0.01 / price`` of a share (0.034 sh at 29c, 0.2 sh at 5c) — an order of magnitude inside
    the executor's 0.5-share HEDGE_SHARE_TOL, so it books as LOCKED and never fabricates a partial.

    Returns (0.0, 0.0) when there is nothing legal to send."""
    try:
        target, px = float(target_shares), float(expected_price)
    except (TypeError, ValueError):
        return 0.0, 0.0
    if target <= 0.0 or not (0.0 < px <= 1.0):
        return 0.0, 0.0
    scale = 10 ** MARKET_BUY_AMOUNT_DP
    usd = math.floor(target * px * scale + 1e-9) / scale
    if usd <= 0.0:
        return 0.0, 0.0
    shares = usd / px
    # THE INVARIANT, ASSERTED BEFORE SIGNING. Floating point can put ``usd/px`` a hair over the target
    # even when ``usd`` was floored; step one cent down rather than send an order that is over by 1e-13.
    while shares > target and usd > 0.0:
        usd = round(usd - 1.0 / scale, MARKET_BUY_AMOUNT_DP)
        shares = usd / px if usd > 0.0 else 0.0
    return (round(usd, MARKET_BUY_AMOUNT_DP), shares) if usd > 0.0 else (0.0, 0.0)


def market_buy_shortfall(price: float) -> float:
    """The most a venue-legal market BUY can fall SHORT of its target, in shares.

    The spend is quantized to the venue's cent, so the missing piece is at most one cent's worth of
    stock: ``0.01 / price``. That is 0.034 sh at 29c and 0.013 sh at 76c — real, tiny, and NOT a partial
    hedge. Without a tolerance shaped like this, the live proof of 2026-08-04 (2.666664 sh delivered
    against a 2.6667 sh target) reads as a miss by 0.000036 of a share and sends every single hedge down
    the unwind-or-verify path instead of booking LOCKED.

    Below ~2c the cent is worth more than half a share and this exceeds the executor's own
    HEDGE_SHARE_TOL; the caller clamps, so a genuinely short hedge can never hide behind this."""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return 0.0
    return (0.1 ** MARKET_BUY_AMOUNT_DP) / p if p > 0.0 else 0.0


def market_buy_amounts(price: float, shares: float) -> tuple[float, float]:
    """Quantize (limit price, share count) so the CLOB will ACCEPT a share-denominated FAK BUY.

    SUPERSEDED for hedging by :func:`market_buy_spend` — a market BUY is priced in USDC, so quantizing a
    SHARE count against a price could never stop the venue handing back extra shares (see that function).
    Kept because the rule it encodes is still the rule, and the CSKA regression tests are written against
    it: a whole-cent price times a whole-share count is the only (price, size) family whose product is
    always 2 decimals.

    A BUY's ``makerAmount`` is the USDC leg (price x shares) and the venue caps it at **2 decimals**;
    ``takerAmount`` (shares) is capped at 4. The order builder does NOT enforce that: on a 0.001-tick
    market its rounding config allows a 5-decimal maker amount, so the order signs cleanly here and is
    REJECTED at the venue with ``invalid amounts, the market buy orders maker amount supports a max
    accuracy of 2 decimals``.

    That is not hypothetical. Kalshi fills FRACTIONAL contract counts (``count_fp: "114.49"``), so the
    first fractional rest-kalshi fill produced a hedge of 114.49 shares; at ANY price with more than 2
    significant decimals in the product (0.76 x 114.49 = 87.0124) the hedge 400s, the fill is left naked,
    and the chain falls through to an unwind. That is the 2026-07-28 17:21Z CSKA/Trnava incident.

    A WHOLE-CENT price times a WHOLE-SHARE count is exactly 2 decimals for every price and every tick
    size, so that is what we send. The share count is floored: never buy more hedge than the fill needs.
    The sub-share remainder is dust below the venue's minimum tradable size — see PregameLiveExecutor's
    dust rule."""
    return market_buy_price(price), float(math.floor(float(shares) + 1e-9))


_VALID_TICKS = ("0.1", "0.01", "0.001", "0.0001")


def tick_size_str(tick: Any) -> Optional[str]:
    """Canonical tick-size string ('0.1'/'0.01'/'0.001'/'0.0001') for v2's PartialCreateOrderOptions,
    accepting either the SDK string form or a float (e.g. the scanner's persisted 0.001). None when
    unmappable -> v2 then resolves the tick from the chain itself (the re-fetch-at-call-time intent).
    v2 wants the LITERAL string; passing a float would KeyError its ROUNDING_CONFIG."""
    if tick is None:
        return None
    s = str(tick).strip()
    if s in _VALID_TICKS:
        return s
    try:
        f = float(tick)
    except (TypeError, ValueError):
        return None
    for cand in _VALID_TICKS:
        if abs(f - float(cand)) < 1e-12:
            return cand
    return None


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
    the signer address. signature_type comes from POLY_SIGNATURE_TYPE (override); when unset it
    defaults to 1 = POLY_PROXY (Polymarket's only valid types are 0=EOA, 1=POLY_PROXY,
    2=POLY_GNOSIS_SAFE — there is no 3). Raises PolyExecError if the private key is absent."""
    pk = os.environ.get("POLYGON_PRIVATE_KEY")
    if not pk:
        raise PolyExecError("POLYGON_PRIVATE_KEY not set.")
    sig_type = int(os.environ.get("POLY_SIGNATURE_TYPE", "1"))
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
    """Build + cache the authenticated v2 ClobClient ONCE (derives L2 creds). Lazy SDK import.

    Built the v2 way — ClobClient(host, chain_id, key=, signature_type=, funder=) — with the
    resolved signature_type, so authenticated reads (incl. get_balance_allowance) are scoped to the
    funder/proxy wallet and read pUSD correctly. L2 creds are derived once (create_or_derive) and
    attached."""
    try:
        from py_clob_client_v2.client import ClobClient
    except ImportError as exc:  # pragma: no cover - SDK missing
        raise PolyExecError(
            "py-clob-client-v2 not installed — pip install py-clob-client-v2.") from exc

    w = resolve_wallet()
    client = ClobClient(
        CLOB_HOST,
        POLYGON_CHAIN_ID,
        key=w["private_key"],
        signature_type=w["signature_type"],
        funder=w["funder"],
    )
    # Derive L2 api creds once and attach them (idempotent: create-or-derive). v2 renamed this from
    # create_or_derive_api_creds() (v1) to create_or_derive_api_key().
    try:
        creds = client.create_or_derive_api_key()
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
                    "POLY_SIGNATURE_TYPE correctly (0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE; "
                    "1 for the usual email/magic deposit wallet), or re-derive API creds for "
                    f"signer {w.get('signer_address')}. Underlying error: {msg}")
            return False, f"Polymarket preflight failed: {msg}"
        return True, f"OK (signer={w.get('signer_address')}, funder={w.get('funder')}, sig_type={w.get('signature_type')})"

    # -- token metadata (re-fetched at call time) ----------------------------
    def _tick_and_negrisk(self, token_id: str) -> tuple[Any, bool]:
        """RE-FETCH tick_size and neg_risk for ``token_id`` at call time (never trust cached). v2's
        get_tick_size returns the canonical string ('0.01'); kept as-is for PartialCreateOrderOptions
        (do NOT float() it — v2 keys its rounding config by the string)."""
        tick = self.client.get_tick_size(token_id)
        neg = bool(self.client.get_neg_risk(token_id))
        return tick, neg

    # -- orders --------------------------------------------------------------
    def place_order(self, token_id: str, price: float, size: float, side: str = "BUY", *,
                    order_type: str = "FOK", tick_size: Optional[float] = None,
                    neg_risk: Optional[bool] = None) -> dict[str, Any]:
        """Place a CLOB order (FOK by default). tick_size / neg_risk are re-fetched if not given.
        Returns normalized {status, shares, usd, avg_price, order_id, raw}."""
        from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import BUY, SELL

        if tick_size is None or neg_risk is None:
            tick_size, neg_risk = self._tick_and_negrisk(token_id)
        side_const = BUY if str(side).upper() == "BUY" else SELL
        args = OrderArgs(token_id=token_id, price=float(price), size=float(size), side=side_const)
        options = PartialCreateOrderOptions(tick_size=tick_size_str(tick_size), neg_risk=bool(neg_risk))
        signed = self.client.create_order(args, options)
        ot = getattr(OrderType, order_type, order_type)
        raw = self.client.post_order(signed, ot)
        return self._normalize(raw, price=price, requested_shares=size, side=side)

    def place_market_sell(self, token_id: str, shares: float, *, price: Optional[float] = None,
                          order_type: str = "FAK") -> dict[str, Any]:
        """UNWIND: market-sell ``shares`` of ``token_id`` with a MARKETABLE limit that sweeps DOWN to
        ``best_bid - 2 ticks`` (so a thin/moving book still fills the available size) using FAK
        (fill-and-kill: partial OK, remainder killed -- NEVER rests). Returns the normalized result keyed
        off the ACTUAL matched amount. (FOK at exactly best_bid was killed by thin in-play books, and the
        caller then logged a fake 'unwound' -- the -$2.35 orphan bug.)"""
        tick_size, neg_risk = self._tick_and_negrisk(token_id)
        try:
            tick = float(tick_size)
        except (TypeError, ValueError):
            tick = 0.01
        if price is None:
            book = self.get_orderbook(token_id)
            bids = book.get("bids") or []
            best_bid = float(bids[0][0]) if bids else None
            price = max(tick, round((best_bid - 2 * tick) if best_bid is not None else tick, 6))
        from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import SELL

        args = OrderArgs(token_id=token_id, price=float(price), size=float(shares), side=SELL)
        options = PartialCreateOrderOptions(tick_size=tick_size_str(tick_size), neg_risk=bool(neg_risk))
        signed = self.client.create_order(args, options)
        ot = getattr(OrderType, order_type, order_type)                # FAK by default
        raw = self.client.post_order(signed, ot)
        return self._normalize(raw, price=price, requested_shares=shares, side="SELL")

    def place_market_buy(self, token_id: str, shares: float, *, price: Optional[float] = None,
                         expected_price: Optional[float] = None,
                         order_type: str = "FAK") -> dict[str, Any]:
        """HEDGE (rest_kalshi direction): market-BUY ``shares`` of ``token_id`` with a MARKETABLE limit that
        sweeps UP to ``best_ask + 2 ticks`` (so a thin/moving book still lifts the size) using FAK. Mirrors
        place_market_sell for the taker complement leg. Returns the normalized result.

        SENT AS AN AMOUNT, NOT A SIZE, because that is what a Polymarket market BUY IS: the venue spends
        the whole USDC maker amount and returns ``amount / fill_price`` shares. Sending a SHARE count with
        a padded limit therefore handed the entire 2-tick pad back as unhedged shares (see
        :func:`market_buy_spend` for the measurements). The spend is computed from ``expected_price`` —
        the price we expect to CLEAR at, i.e. the caller's walk of the ask ladder — while the padded
        limit stays on the order as a ceiling, buying marketability without buying shares.

        The client's ``get_market_order_amounts`` floors the amount to 2 decimals, which is precisely the
        venue rule that 400'd the CSKA hedge, so the amount path enforces it for us rather than us
        quantizing a (price, size) pair against it."""
        tick_size, neg_risk = self._tick_and_negrisk(token_id)
        try:
            tick = float(tick_size)
        except (TypeError, ValueError):
            tick = 0.01
        best_ask = None
        if price is None or expected_price is None:
            # One book read serves both jobs: the marketable limit AND the fallback clearing price. The
            # hedge path normally supplies both from the walk it has just done, so this is not on the
            # hot path — it is the safety net for a caller that has neither.
            try:
                asks = (self.get_orderbook(token_id) or {}).get("asks") or []
                best_ask = float(asks[0][0]) if asks else None
            except Exception:  # noqa: BLE001 — an unreadable book must not raise into the hedge chain
                best_ask = None
        if price is None:
            price = min(1.0 - tick, round((best_ask + 2 * tick) if best_ask is not None else 1.0 - tick, 6))
        requested = float(shares)
        price = market_buy_price(price)
        # CLEARING PRICE, in order of authority: the caller's walk of the ladder for THIS size, then the
        # book's best ask, then the limit itself. The last is the old behaviour and is a floor on how
        # wrong we can be, not a good estimate — so it is logged when it happens.
        clearing = expected_price if expected_price is not None else (best_ask if best_ask is not None
                                                                      else price)
        try:
            clearing = float(clearing)
        except (TypeError, ValueError):
            clearing = price
        if not (0.0 < clearing <= 1.0):
            clearing = price
        if expected_price is None and best_ask is None and self.log:
            self.log.warning("[POLY] market BUY of %.4f sh on %s has no walked or quoted clearing price "
                             "— sizing the spend at the LIMIT %.2f, which can over-buy if it fills "
                             "better.", requested, str(token_id)[:12], price)
        usd, expect = market_buy_spend(requested, clearing)
        if usd <= 0.0 or expect < 1.0:           # sub-share dust: below the smallest amount the venue
            return {"status": "none", "shares": 0.0, "usd": 0.0,   # can price at all -> not an order
                    "avg_price": None, "order_id": None,
                    "raw": {"skipped": "sub_share_size", "requested_shares": requested,
                            "clearing_price": clearing, "usd": usd}}
        # THE INVARIANT, ASSERTED BEFORE SIGNING: at the price we expect to clear at, this order cannot
        # buy more than the fill it is hedging. Fail CLOSED — an over-hedge is naked directional risk
        # booked as if it were a lock, which is exactly what this change exists to end.
        if expect > requested + 1e-9:
            raise PolyExecError(f"refusing a hedge that would over-buy: ${usd:.2f} at {clearing:.4f} is "
                                f"{expect:.4f} sh against a {requested:.4f} sh fill")
        from py_clob_client_v2.clob_types import MarketOrderArgsV2, OrderType, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import BUY

        ot = getattr(OrderType, order_type, order_type)                # FAK by default
        args = MarketOrderArgsV2(token_id=token_id, amount=float(usd), side=BUY, price=float(price),
                                 order_type=ot)
        options = PartialCreateOrderOptions(tick_size=tick_size_str(tick_size), neg_risk=bool(neg_risk))
        signed = self.client.create_market_order(args, options)
        raw = self.client.post_order(signed, ot)
        return self._normalize(raw, price=price, requested_shares=expect, side="BUY")

    # -- position + approval (SELL side; the source of truth for verification/reconciliation) ---------
    def conditional_balance(self, token_id: str) -> float:
        """ACTUAL outcome-token (CTF) shares held for ``token_id`` -- the source of truth for unwind
        verification + position reconciliation (get_trades omits our maker-side fills, positions do not).
        CTF balances are 1e6-scaled (like USDC). Returns whole shares (0.0 if flat / on any read error)."""
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
        try:
            ba = self.client.get_balance_allowance(BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL, token_id=token_id,
                signature_type=self._resolved_signature_type()))
        except Exception:  # noqa: BLE001 - a read failure must be caught by the caller as "unknown"
            raise
        bal = ba.get("balance") if isinstance(ba, dict) else None
        try:
            return float(bal) / 1e6 if bal is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def settle_conditional_balance(self, token_id: str, predicate, *, timeout_s: float = 6.0,
                                   poll_s: float = 0.75) -> Optional[float]:
        """Poll conditional_balance until ``predicate(bal)`` holds or ``timeout_s`` elapses, forcing an
        on-chain re-sync each round via update_balance_allowance. Returns the LAST balance read (or None
        if every read raised). Fill/settlement is not instantaneous: right after a buy/sell fills, the
        CLOB balance endpoint can still report the PRE-trade amount for a few seconds — reading it too
        soon is exactly what made the sell-side smoke read 0 shares after a 5-share buy, and what would
        make a verify read see a stale (non-flat) balance and falsely scream unwind_FAILED. Callers pass
        the predicate for the direction they expect (>= for a buy, <= for a sell-to-flat)."""
        import time as _time
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
        deadline = _time.monotonic() + max(0.0, timeout_s)
        last: Optional[float] = None
        while True:
            try:
                self.client.update_balance_allowance(BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL, token_id=token_id,
                    signature_type=self._resolved_signature_type()))
            except Exception:  # noqa: BLE001 — a sync failure just means we rely on the plain read
                pass
            try:
                last = self.conditional_balance(token_id)
            except Exception:  # noqa: BLE001 — transient read error; keep the prior value and retry
                pass
            if last is not None and predicate(last):
                return last
            if _time.monotonic() >= deadline:
                return last
            _time.sleep(poll_s)

    def list_positions(self) -> list:
        """All non-zero CTF positions for the FUNDER wallet (Polymarket Data API) -- used by reconciliation
        to catch orphans on tokens this run never traded (e.g. a stranded position from a prior run).
        Best-effort: raises on a network/parse error so the caller can degrade to per-token reads."""
        import requests
        funder = resolve_wallet().get("funder")
        if not funder:
            return []
        r = requests.get("https://data-api.polymarket.com/positions",
                         params={"user": funder, "sizeThreshold": 0.1}, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else (data.get("positions") or data.get("data") or [])

    def ctf_allowance_ok(self, token_id: str) -> bool:
        """True iff the exchange is approved to move our CTF (outcome) tokens for ``token_id`` -- required
        to SELL/unwind (buying with USDC never needed it). Reads the CONDITIONAL allowance."""
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
        ba = self.client.get_balance_allowance(BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL, token_id=token_id,
            signature_type=self._resolved_signature_type()))
        allows = (ba.get("allowances") if isinstance(ba, dict) else None) or {}
        for v in allows.values():
            try:
                if int(str(v)) > 0:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def set_ctf_approval(self, token_id: str) -> Any:
        """Grant the exchange approval to move our CTF tokens (one-time setApprovalForAll) so unwinds can
        SELL. On-chain tx; call once at armed startup when live-enabled if ctf_allowance_ok is False."""
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
        return self.client.update_balance_allowance(BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL, token_id=token_id,
            signature_type=self._resolved_signature_type()))

    def _normalize(self, raw: Any, *, price: float, requested_shares: float,
                   side: str = "BUY") -> dict[str, Any]:
        """Map a post_order response to {status, shares, usd, avg_price, order_id, raw}.

        A FOK either fully fills or is killed; a GTC that is ACCEPTED simply RESTS (nothing matched
        yet) — that is NOT a fill. We classify by the MATCHED amount (never the order ``size``, which is
        the requested quantity, not a fill) plus the response ``status``: 0 matched + a live/open status
        -> "resting"; 0 matched otherwise -> "none"; >= requested -> "filled"; else "partial". (Treating
        an accepted rest as "filled" would make a maker fire a phantom hedge on every quote.)

        ``shares`` MUST be counted in SHARES, and making/taking amounts are SIDE-dependent base/quote
        legs — NOT interchangeable:
            BUY  -> makingAmount = USDC paid,   takingAmount = SHARES received
            SELL -> makingAmount = SHARES sold, takingAmount = USDC received
        Reading ``makingAmount`` as "shares" is correct for a SELL (why the sell-side smoke passed) but
        for a BUY it counts DOLLARS as shares and UNDER-reports the fill: a $16.21 buy of ~17.4 shares
        reads as 16.2, so a FULL hedge masquerades as a partial and the executor unwinds a phantom
        remainder — the 2026-07-23 TBTOR double-unwind + false orphan. Pick the SHARE leg by side; if
        only the dollar leg is present, derive shares = dollars / avg fill price."""
        d = raw if isinstance(raw, dict) else {}
        resp_status = str(d.get("status") or "").lower()
        resting_states = ("live", "open", "resting", "delayed", "new")
        success = d.get("success")
        is_buy = str(side).upper() == "BUY"
        def _num(*names: str) -> Optional[float]:
            for nm in names:
                v = d.get(nm)
                if v is None or v == "":
                    continue
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
            return None

        # VENUE CASH FIRST. making/taking are the SIDE's base/quote legs (BUY: making=USDC,
        # taking=SHARES; SELL: the reverse), so their RATIO is the executed price — exact, and exact
        # even when the sweep crossed several levels. Without this the fallback below books the LIMIT
        # we sent, and a marketable buy is deliberately sent 2 ticks THROUGH the ask: every Poly hedge
        # booked 2c dearer than it filled. On 2026-07-29 that alone turned six genuinely +3%/+1% pairs
        # into "HEDGED AT A LOSS" alerts (Tottenham hedges filled 0.35, booked 0.37).
        usd_leg = _num("makingAmount", "making_amount") if is_buy else _num("takingAmount", "taking_amount")
        sh_leg = _num("takingAmount", "taking_amount") if is_buy else _num("makingAmount", "making_amount")
        avg = None
        avg_src = None
        if usd_leg is not None and sh_leg is not None and sh_leg > 0 and usd_leg > 0:
            avg, avg_src = usd_leg / sh_leg, "venue_cash"
        if avg is None:
            for k in ("price", "avg_price", "average_price"):
                if d.get(k) is not None:
                    try:
                        avg, avg_src = float(d[k]), k
                        break
                    except (TypeError, ValueError):
                        continue
        if avg is None:
            avg, avg_src = float(price), "limit_fallback"
        filled = None
        for k in ("size_matched", "filled_size", "matched_size"):   # explicit SHARE fields (side-agnostic)
            if d.get(k) is not None:
                try:
                    filled = float(d[k])
                    break
                except (TypeError, ValueError):
                    continue
        if filled is None:                                     # the SIDE's SHARE leg of making/taking
            for k in (("takingAmount", "taking_amount") if is_buy else ("makingAmount", "making_amount")):
                if d.get(k) is not None:
                    try:
                        filled = float(d[k])
                        break
                    except (TypeError, ValueError):
                        continue
        if filled is None and avg > 0:                         # only the DOLLAR leg present -> $ / price
            for k in (("makingAmount", "making_amount") if is_buy else ("takingAmount", "taking_amount")):
                if d.get(k) is not None:
                    try:
                        filled = float(d[k]) / avg
                        break
                    except (TypeError, ValueError):
                        continue
        if filled is None:                                     # no matched field -> infer from status
            if resp_status in resting_states:
                filled = 0.0
            elif resp_status == "matched":
                filled = float(requested_shares)
            else:
                filled = float(requested_shares) if success else 0.0
        if filled <= 0:
            status = "resting" if resp_status in resting_states else "none"
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
            "avg_price_source": avg_src,
            # actual USDC moved, when the venue reported it — book cost from THIS, not price x count
            "cash_debit": (round(usd_leg, 6) if (usd_leg is not None and filled > 0) else None),
            "order_id": d.get("orderID") or d.get("order_id") or d.get("id"),
            "raw": raw,
        }

    # -- cancels / order reads (live maker lifecycle) ------------------------
    def cancel_order(self, order_id: str) -> Any:
        """Cancel ONE resting order by its CLOB order id. v2's cancel takes an ``OrderPayload`` (not a
        bare string) — wrap it so the maker's PolyOrderClient.cancel(order_id) works end-to-end."""
        from py_clob_client_v2.clob_types import OrderPayload
        return self.client.cancel_order(OrderPayload(orderID=order_id))

    def cancel_all(self) -> Any:
        """Cancel EVERY resting order on the account (shutdown / stray-order safety)."""
        return self.client.cancel_all()

    def open_orders(self, *, market: Optional[str] = None, token_id: Optional[str] = None) -> Any:
        """List resting orders, optionally filtered to a market (condition_id) or token. Used for the
        startup stray-order sweep (any live order left by a previous run is ours -> cancel it)."""
        from py_clob_client_v2.clob_types import OpenOrderParams
        params = OpenOrderParams(market=market, asset_id=token_id) if (market or token_id) else None
        return self.client.get_open_orders(params)

    def get_order(self, order_id: str) -> Any:
        """Authoritative REST status read for one order (confirm placement / cancellation)."""
        return self.client.get_order(order_id)

    def derive_l2_creds(self) -> dict[str, Any]:
        """The L2 API creds for the Poly USER websocket auth. create-or-derive is idempotent; normalize
        ApiCreds -> the WS ``{apiKey, secret, passphrase}`` shape the user channel expects."""
        creds = self.client.create_or_derive_api_key()
        get = (lambda k: getattr(creds, k, None)) if not isinstance(creds, dict) else creds.get
        return {"apiKey": get("api_key"), "secret": get("api_secret"), "passphrase": get("api_passphrase")}

    # -- reads ---------------------------------------------------------------
    def get_orderbook(self, token_id: str) -> dict[str, Any]:
        """Live CLOB book for ``token_id`` plus a normalized ascending ask ladder for BUYS."""
        raw = self.client.get_order_book(token_id)
        # py-clob returns an object with .asks/.bids of price/size objects; normalize generically.
        asks = _ask_levels(_book_as_dict(raw))
        bids = _bid_levels(_book_as_dict(raw))
        return {"asks": asks, "bids": bids, "raw": raw}

    def _resolved_signature_type(self) -> int:
        """signature_type to scope balance reads to the FUNDER/PROXY wallet (default 1 = POLY_PROXY;
        valid Polymarket types are 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE — there is no 3).
        Resolved from the wallet; falls back to POLY_SIGNATURE_TYPE when the key is absent (e.g. an
        injected test client)."""
        try:
            return int(resolve_wallet().get("signature_type", 1))
        except PolyExecError:
            return int(os.environ.get("POLY_SIGNATURE_TYPE", "1"))

    def get_balance(self) -> Any:
        """pUSD/USDC collateral balance/allowance for the FUNDER/PROXY wallet.

        Pass the resolved ``signature_type`` into the v2 BalanceAllowanceParams; combined with the
        client being built with that same signature_type, the read is scoped to the funder/proxy
        wallet's COLLATERAL. Under v2 this returns the real pUSD collateral (Polymarket's post-Apr-2026
        token); v1 could not read pUSD and returned $0 on a funded wallet."""
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL,
                                        signature_type=self._resolved_signature_type())
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
