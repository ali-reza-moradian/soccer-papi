"""Re-derive LIVE order params from a detected arb (Phase 2).

The executor must NOT trust the detection-time prices (they go stale in seconds). Instead, given
a detected CLEAN kalshi<->poly arb, it re-pulls the LIVE books at execution time using the
read-only market-data modules (src/kalshi.py, src/polymarket.py) — NEVER the scanner pipeline.

This module:
  * normalize_arb(arb): pull the two legs (one kalshi, one polymarket) + the venue identifiers
    (kalshi ticker, poly token_id) and detected prices out of a detected-arb dict, tolerant of
    the scanner's payload shapes (telegram_item / csv legs).
  * MarketData: a thin read-only wrapper exposing the two LIVE ask ladders the engine walks.
    The default implementation uses src.kalshi.KalshiClient / src.polymarket.PolymarketClient
    (public, no-auth). Tests inject a fake exposing the same two methods.

Venue identifiers (ticker / token_id) are STABLE and do not go stale, so they are carried on the
arb; only the BOOKS are re-pulled live. If an identifier is absent the resolver raises so the
caller can skip the arb rather than guess.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

KALSHI_BOOKS = {"kalshi"}
POLY_BOOKS = {"polymarket", "poly"}


class ResolveError(Exception):
    """The arb could not be normalized into a clean kalshi<->poly 2-leg pair."""


@dataclass
class VenueLeg:
    venue: str                    # "kalshi" | "polymarket"
    outcome: str
    detected_price: float         # implied probability = 1 / decimal_odds at detection time
    detected_decimal: float
    identifier: Optional[str]     # kalshi ticker or poly token_id
    side: str = "YES"             # kalshi side; poly is always a BUY of the outcome token
    detected_limit: Optional[float] = None   # detection-time $ depth (size * price), if known
    neg_risk: Optional[bool] = None          # poly: neg-risk market flag (re-fetched at call time if None)
    tick_size: Optional[float] = None        # poly: min price tick (re-fetched at call time if None)
    from_detection: bool = False             # True when the identifier came from the persisted leg


@dataclass
class NormalizedArb:
    """An N-leg (N>=2) arb where EVERY leg trades on kalshi or polymarket. ``legs`` is the canonical
    ordered list; ``kalshi`` / ``poly`` are convenience accessors to the FIRST leg of each venue
    (kept for the 2-leg call sites and guardrail tests)."""
    fixture: str
    fixture_id: Optional[str]
    market: str
    fingerprint: str
    legs: list[VenueLeg]
    detected_at: Optional[str] = None
    raw: Optional[dict] = None

    @property
    def kalshi(self) -> Optional[VenueLeg]:
        return next((l for l in self.legs if l.venue == "kalshi"), None)

    @property
    def poly(self) -> Optional[VenueLeg]:
        return next((l for l in self.legs if l.venue == "polymarket"), None)

    @property
    def n_legs(self) -> int:
        return len(self.legs)


def _price_from_leg(leg: dict[str, Any]) -> tuple[float, float]:
    """(implied_price, decimal_odds) from a detected leg. Prefers an explicit price, else 1/odds."""
    dec = leg.get("decimal_odds") or leg.get("odds")
    if leg.get("price") is not None:
        price = float(leg["price"])
        dec = float(dec) if dec else (1.0 / price if price else 0.0)
        return price, dec
    if not dec:
        raise ResolveError(f"leg has neither price nor decimal_odds: {leg}")
    dec = float(dec)
    return (1.0 / dec if dec else 0.0), dec


def _effective_venue(leg: dict[str, Any]) -> str:
    """The venue this leg trades on. Prefers the persisted `venue` field the scanner now emits,
    falling back to the `book` slug for older rows."""
    return str(leg.get("venue") or leg.get("book") or "").strip().lower()


def _kalshi_identifier(leg: dict[str, Any], arb: dict[str, Any]) -> Optional[str]:
    """Kalshi ticker, preferring the persisted `venue_id` (the exact market priced), then legacy keys."""
    for k in ("venue_id", "kalshi_ticker", "ticker"):
        if leg.get(k):
            return str(leg[k])
        if arb.get(k):
            return str(arb[k])
    return None


def _poly_identifier(leg: dict[str, Any], arb: dict[str, Any]) -> Optional[str]:
    """Poly CLOB token_id, preferring the persisted `venue_id`, then legacy keys."""
    for k in ("venue_id", "poly_token_id", "token_id", "token"):
        if leg.get(k):
            return str(leg[k])
        if arb.get(k):
            return str(arb[k])
    return None


def _venue_leg(leg: dict[str, Any], arb: dict[str, Any], venue: str) -> VenueLeg:
    price, dec = _price_from_leg(leg)
    if venue == "kalshi":
        ident = _kalshi_identifier(leg, arb)
        side = str(leg.get("venue_side") or leg.get("side") or "YES").upper()
        neg = tick = None
    else:
        ident = _poly_identifier(leg, arb)
        side = str(leg.get("venue_side") or "BUY").upper()
        neg, tick = leg.get("neg_risk"), leg.get("tick_size")
    return VenueLeg(
        venue=venue, outcome=str(leg.get("outcome", "")),
        detected_price=price, detected_decimal=dec, identifier=ident, side=side,
        detected_limit=leg.get("limit"), neg_risk=neg, tick_size=tick,
        from_detection=bool(leg.get("venue_id")),
    )


def normalize_arb(arb: dict[str, Any]) -> NormalizedArb:
    """Turn a detected-arb dict into an N-leg NormalizedArb (N>=2) where EVERY leg trades on kalshi
    or polymarket. The legs may be split any way across the two venues (e.g. Home+Draw on Kalshi,
    Away on Poly). Reads the persisted execution identifiers off each leg, preferring them over any
    re-discovery.

    Raises ResolveError if the arb has fewer than 2 legs or contains a NON-TRADABLE venue (e.g.
    1xbet / pinnacle / a shadow book) — the caller logs ``untradable_venue`` and skips."""
    legs = arb.get("legs") or []
    if len(legs) < 2:
        raise ResolveError(f"arb has < 2 legs ({len(legs)})")

    # Reject up front if any leg is on a venue we cannot trade.
    untradable = [_effective_venue(l) or "(unknown)" for l in legs
                  if _effective_venue(l) not in (KALSHI_BOOKS | POLY_BOOKS)]
    if untradable:
        raise ResolveError(f"untradable_venue: {sorted(set(untradable))}")

    venue_legs: list[VenueLeg] = []
    for leg in legs:
        v = _effective_venue(leg)
        venue_legs.append(_venue_leg(leg, arb, "kalshi" if v in KALSHI_BOOKS else "polymarket"))

    return NormalizedArb(
        fixture=str(arb.get("match") or arb.get("game") or arb.get("fixture") or ""),
        fixture_id=arb.get("fixture_id"),
        market=str(arb.get("market") or arb.get("market_label") or ""),
        fingerprint=str(arb.get("signature") or arb.get("fingerprint") or arb.get("arb_id") or ""),
        detected_at=arb.get("detected_at") or arb.get("detected_at_et"),
        raw=arb,
        legs=venue_legs,
    )


# --------------------------------------------------------------------------- #
# Live market data (read-only) — re-pull books at execution time                 #
# --------------------------------------------------------------------------- #
def _poly_asks(book: Any) -> list[tuple[float, float]]:
    """Ascending (price, size) BUY ladder from a Polymarket CLOB book payload. One implementation, used
    by both the ask-only reader and the combined one, so the two can never normalize differently."""
    out: list[tuple[float, float]] = []
    for lvl in (book or {}).get("asks") or []:
        try:
            p, s = float(lvl.get("price")), float(lvl.get("size"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 0.0 < p < 1.0 and s > 0:
            out.append((p, s))
    out.sort(key=lambda x: x[0])
    return out


class MarketData:
    """Read-only LIVE book source. Default uses the public no-auth clients in src/kalshi.py and
    src/polymarket.py; the engine only needs the two ask-ladder methods, so tests can inject any
    object exposing them."""

    def __init__(self, *, kalshi_client: Any = None, poly_client: Any = None) -> None:
        self._k = kalshi_client
        self._p = poly_client

    def _kalshi(self):
        if self._k is None:
            from src.kalshi import KalshiClient
            self._k = KalshiClient()
        return self._k

    def _poly(self):
        if self._p is None:
            from src.polymarket import PolymarketClient
            self._p = PolymarketClient()
        return self._p

    def kalshi_ask_ladder(self, ticker: str, side: str = "YES") -> list[tuple[float, float]]:
        """Ascending (price_dollars, size) ladder to BUY ``side`` on the LIVE Kalshi book.

        Buying YES lifts the complements of resting NO bids: a NO bid at n implies a YES offer at
        (1-n). The schema-aware reader in src.kalshi handles BOTH the new orderbook_fp (yes_dollars/
        no_dollars) and the legacy integer-cent orderbook, so the two paths can't drift."""
        from src.kalshi import ask_ladder
        return ask_ladder(self._kalshi().orderbook(ticker), side)

    def poly_ask_ladder(self, token_id: str) -> list[tuple[float, float]]:
        """Ascending (price, size) ladder to BUY the LIVE Polymarket token (CLOB best-ask side)."""
        return _poly_asks(self._poly().book(token_id))

    def kalshi_best_bid(self, ticker: str, side: str = "YES") -> Optional[float]:
        """Highest resting bid to JOIN on the LIVE Kalshi book for ``side`` (paper-maker only). None on
        any error — a maker measurement must never break the caller."""
        try:
            from src.kalshi import best_bid
            return best_bid(self._kalshi().orderbook(ticker), side)
        except Exception:  # noqa: BLE001
            return None

    def poly_best_bid(self, token_id: str) -> Optional[float]:
        """Highest resting bid to JOIN on the LIVE Polymarket book (paper-maker only). None on error."""
        try:
            from src.polymarket import best_bid
            return best_bid(self._poly().book(token_id))
        except Exception:  # noqa: BLE001
            return None

    # -- BOTH SIDES OF ONE BOOK, from ONE request ----------------------------
    # A venue book response already contains bids and asks. Reading the asks and then fetching the same
    # book again for the bids cost one extra throttled round-trip per node per cycle — ~2,059 redundant
    # order-book downloads, 85% of the soccer cycle. These are the readers the pricing path uses so the
    # bid arrives with the ask it came with.
    def kalshi_quote(self, ticker: str, side: str = "YES") -> tuple:
        """``(ask_ladder, best_bid)`` for ``side`` from ONE Kalshi orderbook read."""
        from src.kalshi import ask_ladder, best_bid
        book = self._kalshi().orderbook(ticker)
        return ask_ladder(book, side), best_bid(book, side)

    def poly_quote(self, token_id: str) -> tuple:
        """``(ask_ladder, best_bid)`` from ONE Polymarket CLOB book read."""
        from src.polymarket import best_bid
        book = self._poly().book(token_id)
        return _poly_asks(book), best_bid(book)
