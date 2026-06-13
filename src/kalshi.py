"""Kalshi-direct supplemental odds source (kalshi.com prediction exchange).

THIRD data source, structured exactly like src/theoddsapi.py: it merges into the OddsPapi pipeline
by emitting `bookmakerOdds` fragments keyed by the SAME canonical OddsPapi fixtureId / marketId /
outcomeId the engine already uses. Once merged, the arb math, clone-dedup, staleness, and
mapping-guard run unchanged (see normalize.parse_odds_payload). Nothing here touches arbitrage.py.

Why Kalshi-direct: OddsPapi returns the `kalshi` book SUSPENDED for the World Cup, so we re-source
it live from Kalshi's public market-data API (no auth) and OVERRIDE that suspended stub — the same
recover-a-suspended-book move the-odds-api makes for 1xbet.

Kalshi facts this module is built on:
  * Base https://external-api.kalshi.com/trade-api/v2 ; market data needs NO auth.
  * Per match there are 3 Yes markets — home win / regulation tie / away win — grouped under ONE
    event (event_ticker). They map to the canonical Full Time Result marketId's home/draw/away
    outcomeIds.
  * Prices are cents (1–99). To BACK an outcome you buy Yes at `yes_ask`, so the decimal odds are
    1 / (yes_ask / 100)  (see decimal_from_cents).
  * The order-book depth at the best yes-ask level is the real stake limit for that leg.

Reuse, don't duplicate: team normalization, fixture matching, and the canonical market reverse-index
are imported from src.theoddsapi — Kalshi events match canonical fixtures by the SAME team-identity +
kickoff rule, and resolve to the SAME marketId/outcomeIds.

Safety posture mirrors the-odds-api: a mis-mapped leg is a phantom arb, so a (fixture, book) is
injected only on an exact team-identity + kickoff match, home/draw/away come from the canonical
fixture identity (never a provider tag), and the source stays SHADOW (config kalshi.enabled false /
not actionable) until verified across a few live runs.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

# Reused verbatim from the the-odds-api source — same normalization, matcher, market index, and the
# "override OddsPapi's suspended book" gate (_oddspapi_has_active).
from .theoddsapi import (  # noqa: F401  (re-exported for B2 + tests)
    FixtureMatch,
    MarketIndex,
    _oddspapi_has_active,
    build_market_index,
    match_event_to_fixture,
    normalize_team,
)

DEFAULT_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"


# --------------------------------------------------------------------------- #
# HTTP client (public market-data endpoints; no auth)                           #
# --------------------------------------------------------------------------- #
class KalshiError(Exception):
    """Any failure talking to Kalshi. Caught in run.py so it never breaks the OddsPapi run."""


class KalshiRateLimited(KalshiError):
    """Transient HTTP 429 / 5xx — retried with exponential backoff (the public read limit is tight)."""


class KalshiClient:
    """Thin client over the public Kalshi market-data endpoints used by this source.

    Only the endpoints the probe + B2 build need are exposed: get a series, list markets, get an
    event (with its nested markets), and read a market's order book. All are unauthenticated GETs
    returning JSON — NO auth headers. Quota is not metered the way the-odds-api meters it, but the
    unauthenticated read limit is TIGHT, so calls are (a) throttled to >= min_interval apart and
    (b) retried on 429/5xx with exponential backoff. Never sweep all markets — query by series.
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 30.0,
                 session: requests.Session | None = None, min_interval: float = 0.5) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.min_interval = min_interval     # min seconds between requests (rate-limit cushion)
        self._last_request_ts = 0.0

    # -- endpoints -----------------------------------------------------------
    def series(self, series_ticker: str) -> Any:
        """GET /series/{series_ticker} — confirm the series exists and read its title/metadata."""
        return self._get(f"/series/{series_ticker}", {})

    def markets(self, *, series_ticker: Optional[str] = None, event_ticker: Optional[str] = None,
                status: Optional[str] = "open", limit: int = 100,
                cursor: Optional[str] = None) -> Any:
        """GET /markets — filter by series_ticker / event_ticker / status; paginate via cursor."""
        params: dict[str, Any] = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return self._get("/markets", params)

    def event(self, event_ticker: str, *, with_nested_markets: bool = True) -> Any:
        """GET /events/{event_ticker} — the event plus (optionally) its nested markets."""
        params = {"with_nested_markets": "true"} if with_nested_markets else {}
        return self._get(f"/events/{event_ticker}", params)

    def orderbook(self, ticker: str, *, depth: Optional[int] = None) -> Any:
        """GET /markets/{ticker}/orderbook — resting yes/no levels; depth at best yes-ask = limit."""
        params = {"depth": depth} if depth is not None else {}
        return self._get(f"/markets/{ticker}/orderbook", params)

    # -- transport -----------------------------------------------------------
    def _throttle(self) -> None:
        """Space requests at least min_interval apart so we stay under the public read limit."""
        if self.min_interval <= 0:
            return
        wait = self.min_interval - (time.monotonic() - self._last_request_ts)
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.monotonic()

    @retry(retry=retry_if_exception_type(KalshiRateLimited),
           wait=wait_exponential(multiplier=1, min=1, max=30),
           stop=stop_after_attempt(5), reraise=True)
    def _get(self, path: str, params: dict[str, Any]) -> Any:
        self._throttle()
        try:
            resp = self.session.get(self.base_url + path, params=params, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise KalshiError(f"network error on {path}: {exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise KalshiRateLimited(f"{resp.status_code} on {path}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise KalshiError(f"{resp.status_code} on {path}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise KalshiError(f"non-JSON response on {path}: {resp.text[:200]}") from exc

    def iter_markets(self, *, series_ticker: Optional[str] = None, status: Optional[str] = "open",
                     limit: int = 100, max_pages: int = 50) -> list[dict[str, Any]]:
        """Page through /markets following the `cursor` until exhausted (bounded by max_pages)."""
        out: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(max_pages):
            page = self.markets(series_ticker=series_ticker, status=status, limit=limit, cursor=cursor)
            batch = (page or {}).get("markets") or []
            out.extend(m for m in batch if isinstance(m, dict))
            cursor = (page or {}).get("cursor") or None
            if not cursor or not batch:
                break
        return out


# --------------------------------------------------------------------------- #
# Price + liquidity helpers                                                     #
# --------------------------------------------------------------------------- #
# UNITS — get this exactly right or it mints phantom arbs. Kalshi's list endpoint reports
# `yes_ask_dollars` ALREADY IN DOLLARS (e.g. "0.3200" = $0.32). To BACK an outcome you buy Yes at
# that price, so the decimal odds are simply 1 / yes_ask_dollars  ($0.32 -> 3.125). Do NOT divide
# by 100 and do NOT read any integer-cent field.
def decimal_from_dollars(yes_ask_dollars: Any) -> Optional[float]:
    """Decimal odds to back an outcome = 1 / float(yes_ask_dollars). $0.32 -> 3.125.

    Returns None unless the price is a real two-sided ask in (0, 1) dollars."""
    try:
        price = float(yes_ask_dollars)
    except (TypeError, ValueError):
        return None
    if not (0.0 < price < 1.0):
        return None
    return 1.0 / price


def leg_limit(yes_ask_size_fp: Any, yes_ask_dollars: Any) -> float:
    """Real max stake at the best ask = contracts available × price = size_fp × yes_ask_dollars
    (dollars). A genuine limit — these legs are NOT low_confidence (unlike the-odds-api)."""
    try:
        return float(yes_ask_size_fp) * float(yes_ask_dollars)
    except (TypeError, ValueError):
        return 0.0


# The event-ticker date is US-LOCAL, so for a fixture kicking off in UTC small-hours it can be one
# calendar day BEHIND the fixture's UTC date (e.g. ticker 26JUN13 vs kickoff 2026-06-14T01:00Z). We
# therefore match on team identity within the scan window, allowing the ticker date to differ from
# the fixture's UTC date by up to ±1 day. Anchoring the event "commence" at noon and allowing ±36h
# spans the whole of (ticker_date − 1) through (ticker_date + 1). A WC team-pair is unique inside a
# 2-day window, so this stays unambiguous — and match_event_to_fixture still drops any pair that
# (hypothetically) matched two in-window fixtures as `ambiguous`, never guessing.
_DAY_MATCH_TOLERANCE_MIN = 36 * 60


def _event_commence_iso(event_ticker: str) -> Optional[str]:
    """Parse the date from KXWCGAME-<YYMMMDD><HOME3><AWAY3> (e.g. ...-26JUN13USAPAR -> 2026-06-13)
    and return it as an ISO instant at 12:00:00Z. None if the ticker shape is unexpected."""
    if "-" not in event_ticker:
        return None
    seg = event_ticker.split("-", 1)[1]
    if len(seg) < 7:
        return None
    try:
        d = datetime.strptime(seg[:7].title(), "%y%b%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return d.replace(hour=12).strftime("%Y-%m-%dT%H:%M:%SZ")


def _leg_price_limit(market: dict[str, Any]) -> Optional[tuple[float, float]]:
    """(decimal_odds, limit) for one Kalshi market, or None if it is not an active, priced ask.

    Skips any market whose status != active — a non-active market has no live resting ask."""
    if str(market.get("status") or "").lower() != "active":
        return None
    dec = decimal_from_dollars(market.get("yes_ask_dollars"))
    if dec is None:
        return None
    return dec, leg_limit(market.get("yes_ask_size_fp"), market.get("yes_ask_dollars"))


def _player_line(price: float, limit: float, changed_at: str) -> dict[str, Any]:
    """One canonical priced outcome carrying a REAL limit (size×price) so the engine does NOT mark
    it low_confidence. changedAt = scan time: a best-ask is a live resting order, not a stale line."""
    return {"price": price, "priceAmerican": None, "limit": limit,
            "changedAt": changed_at, "mainLine": True, "active": True}


def _add_leg(entry: dict[str, Any], mid: int, oid: int, price: float, limit: float, changed_at: str) -> None:
    mkt = entry["markets"].setdefault(str(mid), {"marketActive": True, "outcomes": {}})
    mkt["outcomes"][str(oid)] = {"players": {"0": _player_line(price, limit, changed_at)}}


# --------------------------------------------------------------------------- #
# Coverage report (mirrors theoddsapi.Coverage)                                 #
# --------------------------------------------------------------------------- #
@dataclass
class Coverage:
    events_total: int = 0
    matched: int = 0
    unmatched_name: list[str] = field(default_factory=list)
    time_mismatch: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    recovered: int = 0                                       # #fixtures where kalshi was injected
    deferred: int = 0                                        # #fixtures OddsPapi already had active
    incomplete: list[str] = field(default_factory=list)      # matched but not exactly 1 Tie + 2 teams

    def lines(self) -> list[str]:
        out = [
            f"KALSHI: {self.matched}/{self.events_total} events mapped "
            f"(unmatched-name {len(self.unmatched_name)}, time-mismatch {len(self.time_mismatch)}, "
            f"ambiguous {len(self.ambiguous)}) | recovered {self.recovered} fixture(s), "
            f"deferred {self.deferred}",
        ]
        for label, names in (("unmatched-name", self.unmatched_name),
                             ("time-mismatch", self.time_mismatch), ("ambiguous", self.ambiguous),
                             ("incomplete (!= 1 Tie + 2 teams)", self.incomplete)):
            if names:
                out.append(f"  {label}: {', '.join(names)}")
        return out


# --------------------------------------------------------------------------- #
# Merge entry point                                                             #
# --------------------------------------------------------------------------- #
def merge_into(
    raw_by_fixture: dict[str, dict[str, Any]],
    by_fixture: dict[str, dict[str, Any]],
    market_index: MarketIndex,
    markets: list[dict[str, Any]],
    *,
    now: datetime,
    log,
) -> tuple[Coverage, dict[str, set[str]]]:
    """Merge Kalshi per-match World Cup result markets into raw_by_fixture (override the suspended
    OddsPapi `kalshi` stub) and return (coverage, kalshi_books) where kalshi_books maps canonical
    fixtureId -> {"kalshi"} for every fixture this source injected — so run.py can keep those legs
    SHADOW on first rollout, exactly like the the-odds-api toa_books contract.

    `markets` is the flat list from /markets?series_ticker=KXWCGAME (already paginated). Each event
    (one match) has 3 mutually-exclusive Yes markets — home win / Tie / away win. Outcomes are keyed
    by `yes_sub_title` IDENTITY (the "Tie" market -> draw; the other two matched to the canonical
    fixture's p1/p2 via normalize_team), never by ticker order — and home/away come from the
    fixture's p1/p2 identity, never a Kalshi tag (the anti-phantom rule). An event is injected only
    if it matches exactly one in-window fixture AND all three outcomes are active+priced.
    """
    cov = Coverage()
    kalshi_books: dict[str, set[str]] = {}
    h2h = market_index.h2h
    if not h2h:
        log.warning("[KALSHI] no canonical Full Time Result (1x2) market in the index — cannot map; skipping.")
        return cov, kalshi_books

    by_event: dict[str, list[dict[str, Any]]] = {}
    for m in markets:
        if isinstance(m, dict):
            by_event.setdefault(str(m.get("event_ticker") or ""), []).append(m)
    cov.events_total = len(by_event)
    changed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    for ev_ticker, ms in by_event.items():
        label = ev_ticker or "(no event_ticker)"
        ev_commence = _event_commence_iso(ev_ticker)
        if ev_commence is None:
            cov.unmatched_name.append(label)
            log.warning("[KALSHI] %s: cannot parse a date from the event ticker — skipping.", label)
            continue

        # Split the (active, priced) markets into the Tie leg and the two team legs, by yes_sub_title.
        tie_leg: Optional[tuple[float, float]] = None
        team_legs: dict[str, tuple[float, float]] = {}
        team_raw_names: list[str] = []
        for m in ms:
            pl = _leg_price_limit(m)
            if pl is None:
                continue
            sub = str(m.get("yes_sub_title") or "").strip()
            if sub.lower() == "tie":
                tie_leg = pl
            else:
                team_legs[normalize_team(sub)] = pl
                team_raw_names.append(sub)

        if tie_leg is None or len(team_legs) != 2:
            cov.incomplete.append(label)
            log.warning("[KALSHI] %s: need 1 active Tie + 2 active team markets (got tie=%s, teams=%s) — skipping.",
                        label, tie_leg is not None, len(team_legs))
            continue

        fm, reason = match_event_to_fixture(team_raw_names[0], team_raw_names[1], ev_commence,
                                            by_fixture, _DAY_MATCH_TOLERANCE_MIN)
        if fm is None:
            bucket = getattr(cov, reason, None)
            if isinstance(bucket, list):
                bucket.append(label)
            log.info("[KALSHI] %s (%s): no unique fixture match — skipping.", label, reason)
            continue

        home_pl = team_legs.get(fm.p1_norm)
        away_pl = team_legs.get(fm.p2_norm)
        if home_pl is None or away_pl is None:
            cov.incomplete.append(label)
            log.warning("[KALSHI] %s: team labels %s do not both map to fixture p1/p2 (%s/%s) — skipping.",
                        label, team_raw_names, fm.p1_norm, fm.p2_norm)
            continue
        cov.matched += 1
        fid = fm.fixture_id

        # Ensure a fixture envelope exists, then apply the override gate: defer only if OddsPapi
        # already supplied an ACTIVE kalshi book (it returns kalshi suspended for the WC, so we
        # normally overwrite that suspended stub with our live entry).
        fx_raw = raw_by_fixture.get(fid)
        if fx_raw is None:
            info = by_fixture.get(fid, {})
            fx_raw = {"fixtureId": fid, "startTime": info.get("start_time"),
                      "statusId": info.get("status_id"), "hasOdds": True, "bookmakerOdds": {}}
            raw_by_fixture[fid] = fx_raw
        book_odds = fx_raw.setdefault("bookmakerOdds", {})
        if _oddspapi_has_active(book_odds.get("kalshi")):
            cov.deferred += 1
            log.info("[KALSHI] %s: OddsPapi already supplies an active kalshi book — deferring.", label)
            continue

        entry = {"bookmakerIsActive": True, "suspended": False, "markets": {}}
        _add_leg(entry, h2h["marketId"], h2h["home_oid"], home_pl[0], home_pl[1], changed_at)
        _add_leg(entry, h2h["marketId"], h2h["draw_oid"], tie_leg[0], tie_leg[1], changed_at)
        _add_leg(entry, h2h["marketId"], h2h["away_oid"], away_pl[0], away_pl[1], changed_at)
        book_odds["kalshi"] = entry      # override any suspended OddsPapi stub
        kalshi_books.setdefault(fid, set()).add("kalshi")
        cov.recovered += 1

        info = by_fixture.get(fid, {})
        log.info("[KALSHI] %s -> %s vs %s | home %.3f ($%.0f) / draw %.3f ($%.0f) / away %.3f ($%.0f)",
                 label, info.get("p1"), info.get("p2"),
                 home_pl[0], home_pl[1], tie_leg[0], tie_leg[1], away_pl[0], away_pl[1])

    return cov, kalshi_books
