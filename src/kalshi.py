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
    outcomeIds. The per-type series each carry one market kind (KXWCGAME = regulation moneyline
    Home/Tie/Away; KXWCTOTAL = total goals; KXWCBTTS = both-teams-to-score; …), and the event
    ticker is <SERIES>-<YYMMMDD><AWAY3><HOME3> (e.g. KXWCGAME-26JUN30CIVNOR), so a fixture's event
    can be addressed DIRECTLY from teams+date instead of sweeping the whole series (see
    discover_markets).
  * PRICES ARE NOW DOLLARS, not cents: read yes_ask_dollars / yes_bid_dollars / no_ask_dollars /
    no_bid_dollars / last_price_dollars — floats in (0,1). WC legs are commonly ONE-SIDED (Kalshi
    quotes the deep NO side only, so the YES fields are null); derive the YES ask from the best NO
    bid (yes_ask = 1 − no_bid_dollars) and the YES bid from the best NO ask (see yes_ask_price). A
    legacy integer-cent fallback (yes_ask/100) is kept for any market still on the old schema. To
    BACK an outcome you buy Yes at the (effective) yes ask, so the decimal odds are
    1 / yes_ask_dollars (see decimal_from_dollars).
  * The order book is now under `orderbook_fp` with `yes_dollars` / `no_dollars` ladders
    ([price_dollars, size] as strings); the depth to BUY YES is the complement of the `no_dollars`
    ladder (ask_ladder). A legacy `orderbook.yes`/`.no` integer-cent fallback is kept.

Reuse, don't duplicate: team normalization, fixture matching, and the canonical market reverse-index
are imported from src.theoddsapi — Kalshi events match canonical fixtures by the SAME team-identity +
kickoff rule, and resolve to the SAME marketId/outcomeIds.

Safety posture mirrors the-odds-api: a mis-mapped leg is a phantom arb, so a (fixture, book) is
injected only on an exact team-identity + kickoff match, home/draw/away come from the canonical
fixture identity (never a provider tag), and the source stays SHADOW (config kalshi.enabled false /
not actionable) until verified across a few live runs.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

# Reused verbatim from the the-odds-api source — same normalization, matcher, market index, and the
# "override OddsPapi's suspended book" gate (_oddspapi_has_active).
from .theoddsapi import (  # noqa: F401  (re-exported for B2 + tests)
    SCOPE_PER_GAME,
    FixtureMatch,
    MarketIndex,
    _oddspapi_has_active,
    build_market_index,
    match_event_to_fixture,
    normalize_team,
)
# Targeted per-fixture discovery (point 3) builds the event ticker <SERIES>-<YYMMMDD><A3><H3> from
# teams + date; reuse the SAME FIFA 3-letter code map the Polymarket source already curates (every
# provider spelling + the cross-provider equivalences collapse to one code) instead of duplicating it.
from .polymarket import _team_code as _poly_team_code

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
# UNITS — get this exactly right or it mints phantom arbs. Kalshi reports prices ALREADY IN DOLLARS
# (e.g. "0.3200" = $0.32) in the *_dollars fields. To BACK an outcome you buy Yes at the (effective)
# yes ask, so the decimal odds are simply 1 / yes_ask_dollars  ($0.32 -> 3.125). Do NOT divide by 100
# and do NOT read any integer-cent field except as the legacy fallback below.
def _f(v: Any) -> Optional[float]:
    """float(v) or None — never raises."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def yes_ask_price(market: dict[str, Any]) -> Optional[float]:
    """The effective price (DOLLARS, 0-1) to BUY this market's YES outcome — the number the engine
    inverts to a decimal (1/price). Resolution order:
      1. yes_ask_dollars (direct, new schema);
      2. DERIVE from the deep NO side when YES is one-sided/empty: a best NO bid at b implies a YES
         offer at 1-b  (yes_ask = 1 - no_bid_dollars) — this is what makes Kalshi WC legs price at all
         now that they're quoted only on the NO side;
      3. legacy integer-cent yes_ask / 100 (old schema), then its NO-derived complement.
    None if no usable price is recoverable."""
    d = _f(market.get("yes_ask_dollars"))
    if d is not None and 0.0 < d < 1.0:
        return d
    nb = _f(market.get("no_bid_dollars"))
    if nb is not None and 0.0 < nb < 1.0:
        return 1.0 - nb
    c = _f(market.get("yes_ask"))                       # legacy integer cents (1–99)
    if c is not None and 0.0 < c < 100.0:
        return c / 100.0
    cnb = _f(market.get("no_bid"))
    if cnb is not None and 0.0 < cnb < 100.0:
        return 1.0 - cnb / 100.0
    return None


def no_ask_price(market: dict[str, Any]) -> Optional[float]:
    """The effective price (DOLLARS, 0-1) to BUY this market's NO outcome (Under / BTTS-No). Mirror of
    yes_ask_price: no_ask_dollars; else derive from the best YES bid (1 - yes_bid_dollars); else the
    legacy integer-cent no_ask / 100 and its YES-derived complement. None if unrecoverable."""
    d = _f(market.get("no_ask_dollars"))
    if d is not None and 0.0 < d < 1.0:
        return d
    yb = _f(market.get("yes_bid_dollars"))
    if yb is not None and 0.0 < yb < 1.0:
        return 1.0 - yb
    c = _f(market.get("no_ask"))                        # legacy integer cents
    if c is not None and 0.0 < c < 100.0:
        return c / 100.0
    cyb = _f(market.get("yes_bid"))
    if cyb is not None and 0.0 < cyb < 100.0:
        return 1.0 - cyb / 100.0
    return None


def decimal_from_dollars(price_dollars: Any) -> Optional[float]:
    """Decimal odds to back an outcome = 1 / float(price_dollars). $0.32 -> 3.125. The caller passes
    the EFFECTIVE ask from yes_ask_price / no_ask_price (already derived from the NO side when needed).
    Returns None unless the price is a real ask in (0, 1) dollars."""
    price = _f(price_dollars)
    if price is None or not (0.0 < price < 1.0):
        return None
    return 1.0 / price


def leg_limit(size_fp: Any, price_dollars: Any) -> float:
    """Real max stake at the best ask = contracts available × price = size_fp × price (dollars). A
    genuine limit — these legs are NOT low_confidence (unlike the-odds-api). 0.0 on bad input."""
    s, p = _f(size_fp), _f(price_dollars)
    return s * p if (s is not None and p is not None) else 0.0


def _yes_ask_size(market: dict[str, Any]) -> float:
    """Contracts available at the effective YES ask: yes_ask_size_fp, else the size of the best NO bid
    we'd lift to buy YES (no_bid_size_fp). 0.0 if neither — the engine then treats the leg's limit as
    UNVERIFIED via the assumed cap, never a fantasy size."""
    s = _f(market.get("yes_ask_size_fp"))
    if s is not None and s > 0:
        return s
    s = _f(market.get("no_bid_size_fp"))
    return s if (s is not None and s > 0) else 0.0


def _no_ask_size(market: dict[str, Any]) -> float:
    """Contracts available at the effective NO ask: no_ask_size_fp, else the best YES bid size
    (yes_bid_size_fp) we'd lift to buy NO. 0.0 if neither."""
    s = _f(market.get("no_ask_size_fp"))
    if s is not None and s > 0:
        return s
    s = _f(market.get("yes_bid_size_fp"))
    return s if (s is not None and s > 0) else 0.0


def ask_ladder(book: Any, side: str = "YES") -> list[tuple[float, float]]:
    """Ascending (price_dollars, size) ask ladder to BUY ``side`` from a Kalshi orderbook response.

    NEW schema: book['orderbook_fp'] = {'yes_dollars': [[price_str, size], …], 'no_dollars': […]},
    each a list of resting BIDS in dollars. To BUY YES you lift resting NO bids — a NO bid at n is a
    YES offer at (1-n) — so buying YES walks the `no_dollars` ladder (symmetric for NO). LEGACY
    fallback: book['orderbook'] = {'yes': [[cents,size],…], 'no': […]} in integer cents (also handles
    the already-unwrapped orderbook dict). Returns [] when there is no usable depth."""
    book = book if isinstance(book, dict) else {}
    opp = "no" if str(side).upper() == "YES" else "yes"
    out: list[tuple[float, float]] = []
    ofp = book.get("orderbook_fp")
    if isinstance(ofp, dict):
        for lvl in ofp.get(f"{opp}_dollars") or []:
            try:
                p, s = float(lvl[0]), float(lvl[1])
            except (TypeError, ValueError, IndexError):
                continue
            price = 1.0 - p                            # complement: lifting the opposite side's bid
            if 0.0 < price < 1.0 and s > 0:
                out.append((price, s))
    if not out:                                        # legacy integer-cent fallback
        ob = book.get("orderbook")
        ob = ob if isinstance(ob, dict) else book
        for lvl in ob.get(opp) or []:
            try:
                c, s = float(lvl[0]), float(lvl[1])
            except (TypeError, ValueError, IndexError):
                continue
            price = (100.0 - c) / 100.0
            if 0.0 < price < 1.0 and s > 0:
                out.append((price, s))
    out.sort(key=lambda x: x[0])
    return out


# The event-ticker date is US-LOCAL, so for a fixture kicking off in UTC small-hours it can be one
# calendar day BEHIND the fixture's UTC date (e.g. ticker 26JUN13 vs kickoff 2026-06-14T01:00Z). We
# therefore match on team identity within the scan window, allowing the ticker date to differ from
# the fixture's UTC date by up to ±1 day. Anchoring the event "commence" at noon and allowing ±36h
# spans the whole of (ticker_date − 1) through (ticker_date + 1). A WC team-pair is unique inside a
# 2-day window, so this stays unambiguous — and match_event_to_fixture still drops any pair that
# (hypothetically) matched two in-window fixtures as `ambiguous`, never guessing.
_DAY_MATCH_TOLERANCE_MIN = 36 * 60

# The 1x2 tie leg's yes_sub_title varies by event ("Tie", "Draw", "Tie (Regulation)", …). Classify it
# by CONTAINING 'tie' or 'draw' as a WORD (case-insensitive), never by equality — a drifted label like
# "Tie (Regulation)" or "Draw" must still be the tie leg and never fall into the two team legs.
_TIE_RE = re.compile(r"\b(?:tie|draw)\b", re.IGNORECASE)


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
    """(decimal_odds, limit) to BACK the YES side of one Kalshi market, or None if it is not an
    active, priced ask. Uses the effective yes ask (derived from the NO side when one-sided) and the
    contracts at that ask. Skips any market whose status != active (no live resting ask)."""
    if str(market.get("status") or "").lower() != "active":
        return None
    price = yes_ask_price(market)
    dec = decimal_from_dollars(price)
    if dec is None:
        return None
    return dec, leg_limit(_yes_ask_size(market), price)


def _venue(ticker: Any, side: str) -> dict[str, Any]:
    """Execution metadata for a Kalshi leg: the exact market ticker we priced + the side to BACK
    (buy YES at yes_ask, or buy NO at no_ask). Persisted so the executor re-pulls the SAME book it
    was priced from rather than re-discovering a possibly-different ticker."""
    return {"venue": "kalshi", "venueId": (str(ticker) if ticker else None), "venueSide": side}


def _player_line(price: float, limit: float, changed_at: str,
                 venue: dict[str, Any] | None = None) -> dict[str, Any]:
    """One canonical priced outcome carrying a REAL limit (size×price) so the engine does NOT mark
    it low_confidence. changedAt = scan time: a best-ask is a live resting order, not a stale line.

    ``venue`` (additive, optional) records the execution identifiers (ticker + side) for the
    automated executor; it never affects the arb math or the manual pipeline."""
    line = {"price": price, "priceAmerican": None, "limit": limit,
            "changedAt": changed_at, "mainLine": True, "active": True}
    if venue:
        line.update(venue)
    return line


def _add_leg(entry: dict[str, Any], mid: int, oid: int, price: float, limit: float, changed_at: str,
             venue: dict[str, Any] | None = None) -> None:
    mkt = entry["markets"].setdefault(str(mid), {"marketActive": True, "outcomes": {}})
    mkt["outcomes"][str(oid)] = {"players": {"0": _player_line(price, limit, changed_at, venue)}}


# --------------------------------------------------------------------------- #
# SAFE-TIER extras: total goals O/U (KXWCTOTAL) + Both Teams To Score (KXWCBTTS) #
# --------------------------------------------------------------------------- #
# These match-level markets settle on the SAME basis as Kalshi's 1x2 — "90 minutes plus stoppage
# time (does not include extra time or penalties)" — which is exactly how standard bookmaker
# totals/BTTS settle, so they are settlement-compatible and safe to make actionable (see the audit).
# Each Kalshi market is binary: BACK the Over/Yes by buying Yes (yes_ask), BACK the Under/No by
# buying No (no_ask). no_ask_size_fp is often null -> limit 0 -> the engine treats that leg as
# UNVERIFIED via the assumed cap (never a fantasy size).
_TOTAL_LINE_RE = re.compile(r"over\s+(\d+(?:\.\d+)?)", re.IGNORECASE)


def _no_leg_price_limit(market: dict[str, Any]) -> Optional[tuple[float, float]]:
    """(decimal_odds, limit) to BACK the NO side (buy No at the effective no ask, derived from the YES
    side when one-sided), or None if not active/priced."""
    if str(market.get("status") or "").lower() != "active":
        return None
    price = no_ask_price(market)
    dec = decimal_from_dollars(price)
    if dec is None:
        return None
    return dec, leg_limit(_no_ask_size(market), price)


def _total_line(market: dict[str, Any]) -> Optional[float]:
    """Parse the O/U line from a KXWCTOTAL market (yes_sub_title 'Over 2.5 goals scored')."""
    mm = _TOTAL_LINE_RE.search(str(market.get("yes_sub_title") or market.get("title") or ""))
    if not mm:
        return None
    try:
        return float(mm.group(1))
    except ValueError:
        return None


def _game_key(event_ticker: str) -> str:
    """Game suffix shared across series: KXWCTOTAL-26JUN18CZERSA -> 26JUN18CZERSA."""
    return event_ticker.split("-", 1)[1] if "-" in event_ticker else event_ticker


def _series_of(event_ticker: str) -> str:
    """Series prefix: KXWCTOTAL-26JUN18CZERSA -> KXWCTOTAL."""
    return event_ticker.split("-", 1)[0] if "-" in event_ticker else event_ticker


def _inject_totals(entry: dict[str, Any], total_markets: list[dict[str, Any]],
                   market_index: MarketIndex, changed_at: str) -> int:
    """Inject Kalshi total-goals O/U legs (Over=yes_ask, Under=no_ask) for every line the canonical
    index knows. Returns the number of lines injected (both outcomes present)."""
    n = 0
    for m in total_markets:
        line = _total_line(m)
        if line is None:
            continue
        tidx = market_index.totals.get(line)
        if not tidx or tidx.get("scope") != SCOPE_PER_GAME:
            continue
        over = _leg_price_limit(m)          # Yes side -> Over
        under = _no_leg_price_limit(m)      # No side  -> Under
        if over is None or under is None:
            continue
        tk = m.get("ticker")
        _add_leg(entry, tidx["marketId"], tidx["over_oid"], over[0], over[1], changed_at, _venue(tk, "YES"))
        _add_leg(entry, tidx["marketId"], tidx["under_oid"], under[0], under[1], changed_at, _venue(tk, "NO"))
        n += 1
    return n


def _inject_btts(entry: dict[str, Any], btts_markets: list[dict[str, Any]],
                 market_index: MarketIndex, changed_at: str) -> int:
    """Inject the Kalshi Both-Teams-To-Score Yes/No leg (Yes=yes_ask, No=no_ask). Returns 1 if done."""
    if not market_index.btts or market_index.btts.get("scope") != SCOPE_PER_GAME:
        return 0
    for m in btts_markets:
        yes = _leg_price_limit(m)
        no = _no_leg_price_limit(m)
        if yes is None or no is None:
            continue
        b = market_index.btts
        tk = m.get("ticker")
        _add_leg(entry, b["marketId"], b["yes_oid"], yes[0], yes[1], changed_at, _venue(tk, "YES"))
        _add_leg(entry, b["marketId"], b["no_oid"], no[0], no[1], changed_at, _venue(tk, "NO"))
        return 1
    return 0


# --------------------------------------------------------------------------- #
# Discovery — address each fixture's event DIRECTLY by its deterministic ticker  #
# --------------------------------------------------------------------------- #
# Kalshi WC event tickers are <SERIES>-<YYMMMDD><AWAY3><HOME3> (e.g. KXWCGAME-26JUN30CIVNOR), so a
# fixture's markets can be pulled by event_ticker from teams + date instead of sweeping the whole
# series. Codes are the UPPERCASE FIFA 3-letter codes (the Polymarket source's curated map); fixture
# matching is still by team IDENTITY (match_event_to_fixture), so the AWAY/HOME order in the ticker is
# only a guess — we try BOTH orderings (and the prior US-local day) and stop at the first that returns
# markets.
def _team_code(name: Any) -> Optional[str]:
    """UPPERCASE Kalshi/FIFA 3-letter code for a team (any provider spelling), or None if it is not a
    coded WC nation (so we never build a ticker for a non-WC fixture)."""
    c = _poly_team_code(name)
    return c.upper() if c else None


def _yymmmdd(start_time: Any, *, day_delta: int = 0) -> Optional[str]:
    """The Kalshi ticker date code (e.g. '26JUN30') for a fixture's UTC start (optionally shifted by
    ``day_delta`` days), or None if no YYYY-MM-DD is recoverable."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(start_time or ""))
    if not m:
        return None
    try:
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None
    return (dt + timedelta(days=day_delta)).strftime("%y%b%d").upper()


def _event_ticker_suffixes(info: dict[str, Any]) -> list[str]:
    """Deterministic <YYMMMDD><X3><Y3> game suffixes for one by_fixture entry, or [] if either team
    is non-coded or the date is unrecoverable. Both team orderings × {kickoff UTC day, prior day}
    (a small-hours UTC kickoff can be listed under the previous US-local calendar date)."""
    c1, c2 = _team_code(info.get("p1")), _team_code(info.get("p2"))
    if not c1 or not c2:
        return []
    out: list[str] = []
    for delta in (0, -1):
        dd = _yymmmdd(info.get("start_time"), day_delta=delta)
        if not dd:
            continue
        for a, b in ((c1, c2), (c2, c1)):
            suf = f"{dd}{a}{b}"
            if suf not in out:
                out.append(suf)
    return out


def _add_market(found: dict[str, dict[str, Any]], m: Any) -> None:
    """De-dup markets by their unique market ticker (the same market can surface via several calls)."""
    if isinstance(m, dict):
        tk = str(m.get("ticker") or "")
        if tk:
            found.setdefault(tk, m)


def discover_markets(client: "KalshiClient", by_fixture: dict[str, dict[str, Any]], *,
                     result_series: str = "KXWCGAME", extra_series: tuple[str, ...] = (),
                     log=None) -> list[dict[str, Any]]:
    """Find each fixture's Kalshi markets and return the flat list merge_into consumes.

    PRIMARY: for each fixture build the <result_series>-<suffix> event ticker from teams + date and
    pull it by event_ticker (both orderings + the prior day). The first ticker that returns markets
    wins; for that same game suffix we then pull each ``extra_series`` (totals/BTTS) directly. FALLBACK:
    fixtures left unresolved (non-coded nations / a ticker Kalshi spells differently) trigger ONE
    per-series sweep so nothing regresses. Every failure is logged and skipped — the feed must never
    break the run."""
    found: dict[str, dict[str, Any]] = {}
    resolved: set[str] = set()
    series_all = tuple(s for s in (result_series, *extra_series) if s)

    for fid, info in by_fixture.items():
        for suf in _event_ticker_suffixes(info):
            et = f"{result_series}-{suf}"
            try:
                page = client.markets(event_ticker=et, status="open")
            except KalshiError as exc:
                if log:
                    log.warning("[KALSHI] event %s fetch failed (%s) — trying next.", et, exc)
                continue
            ms = [m for m in (page or {}).get("markets") or [] if isinstance(m, dict)]
            if not ms:
                continue
            for m in ms:
                _add_market(found, m)
            resolved.add(fid)
            for series in extra_series:                # same game suffix, sibling series
                if not series:
                    continue
                et2 = f"{series}-{suf}"
                try:
                    page2 = client.markets(event_ticker=et2, status="open")
                except KalshiError as exc:
                    if log:
                        log.warning("[KALSHI] event %s fetch failed (%s) — skipping that series.", et2, exc)
                    continue
                for m in (page2 or {}).get("markets") or []:
                    _add_market(found, m)
            break                                      # first ordering/date that hits wins

    unresolved = [fid for fid in by_fixture if fid not in resolved]
    if unresolved:
        if log:
            log.info("[KALSHI] %d fixture(s) not addressable by ticker — sweeping %s as fallback.",
                     len(unresolved), ", ".join(series_all))
        for series in series_all:
            try:
                for m in client.iter_markets(series_ticker=series, status="open"):
                    _add_market(found, m)
            except KalshiError as exc:
                if log:
                    log.warning("[KALSHI] series %s sweep failed (%s) — skipping.", series, exc)
    return list(found.values())


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
    totals_fixtures: int = 0                                  # #fixtures with >=1 total-goals line injected
    totals_lines: int = 0                                     # total O/U lines injected across fixtures
    btts_fixtures: int = 0                                    # #fixtures with a BTTS leg injected

    def lines(self) -> list[str]:
        out = [
            f"KALSHI: {self.matched}/{self.events_total} events mapped "
            f"(unmatched-name {len(self.unmatched_name)}, time-mismatch {len(self.time_mismatch)}, "
            f"ambiguous {len(self.ambiguous)}) | recovered {self.recovered} fixture(s), "
            f"deferred {self.deferred} | SAFE-TIER: totals {self.totals_lines} line(s) on "
            f"{self.totals_fixtures} fixture(s), BTTS on {self.btts_fixtures} fixture(s)",
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

    # Group ALL markets by GAME KEY (the suffix shared across series) so the totals (KXWCTOTAL) and
    # BTTS (KXWCBTTS) markets for a match ride on the SAME fixture the 1x2 (KXWCGAME) result anchored.
    # Classify by series prefix; unknown series (e.g. KXWCGOAL/KXWCCORNERS) are ignored here.
    by_game: dict[str, dict[str, Any]] = {}
    for m in markets:
        if not isinstance(m, dict):
            continue
        et = str(m.get("event_ticker") or "")
        g = by_game.setdefault(_game_key(et), {"result": [], "total": [], "btts": [], "ticker": None})
        series = _series_of(et)
        if series.endswith("TOTAL"):
            g["total"].append(m)
        elif series.endswith("BTTS"):
            g["btts"].append(m)
        elif series.endswith("GAME"):
            g["result"].append(m)
            if g["ticker"] is None and "-" in et:
                g["ticker"] = et
    # Only game keys with a 1x2 result group are matchable units.
    matchable = {gk: g for gk, g in by_game.items() if g["result"]}
    cov.events_total = len(matchable)
    changed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    for gk, g in matchable.items():
        ms = g["result"]
        ev_ticker = g["ticker"] or gk
        label = ev_ticker or "(no event_ticker)"
        ev_commence = _event_commence_iso(ev_ticker)
        if ev_commence is None:
            cov.unmatched_name.append(label)
            log.warning("[KALSHI] %s: cannot parse a date from the event ticker — skipping.", label)
            continue

        # Split the (active, priced) markets into the Tie leg and the two team legs, by yes_sub_title.
        # Track the per-leg market TICKER alongside the price so the executor can re-pull the exact
        # book each leg was priced from.
        tie_leg: Optional[tuple[float, float]] = None
        tie_ticker: Any = None
        team_legs: dict[str, tuple[float, float]] = {}
        team_tickers: dict[str, Any] = {}
        team_raw_names: list[str] = []
        all_subs: list[str] = []                       # every subtitle seen (for self-diagnosing label drift)
        for m in ms:
            sub = str(m.get("yes_sub_title") or "").strip()
            all_subs.append(sub)
            pl = _leg_price_limit(m)
            if pl is None:
                continue
            if _TIE_RE.search(sub):                    # 'Tie', 'Draw', 'Tie (Regulation)', … -> the tie leg
                tie_leg = pl
                tie_ticker = m.get("ticker")
            else:
                norm = normalize_team(sub)
                team_legs[norm] = pl
                team_tickers[norm] = m.get("ticker")
                team_raw_names.append(sub)

        if tie_leg is None or len(team_legs) != 2:
            cov.incomplete.append(label)
            log.warning("[KALSHI] %s: need 1 active Tie + 2 active team markets (got tie=%s, teams=%s; "
                        "subtitles=%s) — skipping.", label, tie_leg is not None, len(team_legs), all_subs)
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
        _add_leg(entry, h2h["marketId"], h2h["home_oid"], home_pl[0], home_pl[1], changed_at,
                 _venue(team_tickers.get(fm.p1_norm), "YES"))
        _add_leg(entry, h2h["marketId"], h2h["draw_oid"], tie_leg[0], tie_leg[1], changed_at,
                 _venue(tie_ticker, "YES"))
        _add_leg(entry, h2h["marketId"], h2h["away_oid"], away_pl[0], away_pl[1], changed_at,
                 _venue(team_tickers.get(fm.p2_norm), "YES"))

        # SAFE-TIER: add total-goals O/U and BTTS legs (same regulation settlement) onto this fixture.
        nt = _inject_totals(entry, g["total"], market_index, changed_at)
        if nt:
            cov.totals_fixtures += 1
            cov.totals_lines += nt
        nb = _inject_btts(entry, g["btts"], market_index, changed_at)
        if nb:
            cov.btts_fixtures += 1

        book_odds["kalshi"] = entry      # override any suspended OddsPapi stub
        kalshi_books.setdefault(fid, set()).add("kalshi")
        cov.recovered += 1

        info = by_fixture.get(fid, {})
        log.info("[KALSHI] %s -> %s vs %s | home %.3f ($%.0f) / draw %.3f ($%.0f) / away %.3f ($%.0f)"
                 " | +%d total line(s)%s",
                 label, info.get("p1"), info.get("p2"),
                 home_pl[0], home_pl[1], tie_leg[0], tie_leg[1], away_pl[0], away_pl[1],
                 nt, " + BTTS" if nb else "")

    return cov, kalshi_books
