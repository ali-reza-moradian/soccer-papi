"""Turn an odds-by-tournaments payload into per-fixture/market/outcome candidate tables.

The API already aligns every book under one canonical fixtureId and shared canonical
outcomeIds, so there is NO fuzzy team-name / outcome matching here — we just walk the
nested structure and keep the standard (player "0") line for each priced outcome.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class RawCandidate:
    book: str
    outcome_id: int
    price: float
    price_american: Optional[str]
    limit: Optional[float]
    changed_at: Optional[str]
    main_line: bool
    exchange_meta: Optional[dict[str, Any]]


@dataclass
class FixtureFeed:
    fixture_id: str
    participant1_id: Optional[int]
    participant2_id: Optional[int]
    tournament_id: Optional[int]
    status_id: Optional[int]
    start_time: Optional[str]
    updated_at: Optional[str]
    has_odds: bool
    # markets[market_id][outcome_id] -> list[RawCandidate]
    markets: dict[int, dict[int, list[RawCandidate]]] = field(default_factory=dict)
    books_present: set[str] = field(default_factory=set)
    fixture_paths: dict[str, str] = field(default_factory=dict)


def _as_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def exchange_liquidity(exchange_meta: Optional[dict[str, Any]], fallback_limit: Optional[float]) -> Optional[float]:
    """Best-effort available liquidity for an exchange leg.

    exchangeMeta shape varies by exchange; try common keys, else fall back to `limit`.
    """
    if isinstance(exchange_meta, dict):
        for key in ("availableLiquidity", "available", "liquidity", "backSize", "size", "available_volume"):
            v = _as_float(exchange_meta.get(key))
            if v is not None and v > 0:
                return v
    return fallback_limit


def parse_odds_payload(payload: Any) -> list[FixtureFeed]:
    """Parse the list of fixtures returned by /v4/odds-by-tournaments (or /v4/odds)."""
    if isinstance(payload, dict):
        # Some responses may wrap fixtures; accept common shapes defensively.
        payload = payload.get("fixtures") or payload.get("data") or [payload]
    if not isinstance(payload, list):
        return []

    feeds: list[FixtureFeed] = []
    for fx in payload:
        if not isinstance(fx, dict):
            continue
        feed = FixtureFeed(
            fixture_id=str(fx.get("fixtureId")),
            participant1_id=fx.get("participant1Id"),
            participant2_id=fx.get("participant2Id"),
            tournament_id=fx.get("tournamentId"),
            status_id=fx.get("statusId"),
            start_time=fx.get("startTime"),
            updated_at=fx.get("updatedAt"),
            has_odds=bool(fx.get("hasOdds", True)),
        )

        for book, bdata in (fx.get("bookmakerOdds") or {}).items():
            if not isinstance(bdata, dict):
                continue
            if bdata.get("bookmakerIsActive") is False:
                continue
            if bdata.get("suspended") is True:
                continue
            path = bdata.get("fixturePath")
            if path:
                feed.fixture_paths[book] = path

            priced_any = False
            for mid_str, mdata in (bdata.get("markets") or {}).items():
                if not isinstance(mdata, dict):
                    continue
                if mdata.get("marketActive") is False:
                    continue
                try:
                    mid = int(mid_str)
                except (TypeError, ValueError):
                    continue

                for oid_str, odata in (mdata.get("outcomes") or {}).items():
                    if not isinstance(odata, dict):
                        continue
                    players = odata.get("players") or {}
                    p = players.get("0")  # standard (non player-prop) line
                    if not isinstance(p, dict):
                        continue
                    if p.get("active") is False:
                        continue
                    price = _as_float(p.get("price"))
                    if price is None or price <= 1.0:
                        continue
                    try:
                        oid = int(oid_str)
                    except (TypeError, ValueError):
                        continue

                    rc = RawCandidate(
                        book=book,
                        outcome_id=oid,
                        price=price,
                        price_american=p.get("priceAmerican"),
                        limit=_as_float(p.get("limit")),
                        changed_at=p.get("changedAt"),
                        main_line=bool(p.get("mainLine")),
                        exchange_meta=p.get("exchangeMeta"),
                    )
                    feed.markets.setdefault(mid, {}).setdefault(oid, []).append(rc)
                    priced_any = True

            if priced_any:
                feed.books_present.add(book)

        feeds.append(feed)

    return feeds


def seen_market_ids(feeds: list[FixtureFeed]) -> set[int]:
    ids: set[int] = set()
    for f in feeds:
        ids.update(f.markets.keys())
    return ids
