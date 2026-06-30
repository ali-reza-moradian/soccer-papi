"""Polymarket-direct supplemental odds source (polymarket.com prediction exchange).

FOURTH data source, structured exactly like src/kalshi.py: it merges into the OddsPapi pipeline by
emitting `bookmakerOdds` fragments keyed by the SAME canonical OddsPapi fixtureId / marketId /
outcomeId the engine already uses. Once merged, the arb math, clone-dedup, staleness, and
mapping-guard run unchanged (see normalize.parse_odds_payload). Nothing here touches arbitrage.py.

Why Polymarket-direct: OddsPapi's `polymarket` book is often missing or stale for the World Cup, so
we re-source it live from Polymarket's PUBLIC, read-only APIs (no auth, no trading) and fill the
`polymarket` slug — the same recover-a-book move kalshi/the-odds-api make. When OddsPapi DOES supply
an active `polymarket` book for a fixture we DEFER to it (it carries more market types); we only fill
fixtures OddsPapi left without an active polymarket book.

Polymarket facts this module is built on (confirmed by scripts/probe_polymarket, validated live):
  * Two public APIs, both unauthenticated GETs returning JSON:
      - Gamma  https://gamma-api.polymarket.com  -> market/event DISCOVERY (/public-search, /events)
      - CLOB   https://clob.polymarket.com        -> live prices/depth (/book?token_id=)
  * A WC match = ONE Gamma event (negRisk=true) holding THREE separate binary Yes/No markets —
    home win / Draw / away win. Outcome identity is in each market's `groupItemTitle`
    ("Côte d'Ivoire", "Ecuador", "Draw (Côte d'Ivoire vs. Ecuador)"); when that field is absent
    (Gamma's /public-search omits it) the same identity + the match DATE are recoverable from the
    market `question` ("Will Côte d'Ivoire win on 2026-06-14?" / "...end in a draw?"). leg_role()
    prefers groupItemTitle and falls back to the question, so the source works on either payload.
  * The bettable leg is each market's "Yes" token (clobTokenIds[i] where outcomes[i] == "Yes").
  * Gamma encodes `outcomes` / `clobTokenIds` / `outcomePrices` as JSON-string arrays — parse them.
  * PRICE from the CLOB best ASK (the buy price), NOT Gamma's outcomePrices (those are last/mid).
    Prices are dollars in (0,1) = implied probability, so the decimal odds to BACK an outcome are
    1 / ask. The real per-leg limit is the best-ask SIZE × price (shares × dollars) = dollars, a
    genuine limit exactly like Kalshi's size×price (so these legs are NOT low_confidence).
  * DEPTH-AWARE pricing (default ON, ``depth_pricing``): the single best-ask tick over-states profit
    on thin/flickering legs (a longshot draw quotes 8.40 at the top but fills ~8.15 the moment you
    size up — live: Germany-Paraguay Draw quoted 5.2632 at $10k but Polymarket's avg fill was ~5.135,
    because the bot read the top tick, not the size-weighted fill). So instead of the top tick we WALK
    THE ASK LADDER to the per-leg stake the arb intends (bookmath.walk_book): the quoted odds are
    1 / (avg fill price walking cheapest-first to ``reference_size``) — the exact number our probe
    prints as "REAL book: avg fill", matching Polymarket's "Avg. Price"/"To win" to rounding. The
    ``limit`` stays the leg's fillable dollar depth within ``slippage_pct`` of the best ask. A deep leg
    (favorite) walks ~entirely at its best ask -> ~unchanged; a thin leg blends into worse levels ->
    worse odds, shrinking the arb so it stops firing as a phantom REAL. (Sizing runs in arbitrage.py
    AFTER pricing, so we walk to a configurable reference stake, never a price only the top tick fills.)
    The walk-the-book math lives in the pure src.bookmath helper, shared (by parity test) with the
    executor's fees_sizing.walk_book so the two can't drift; the scanner never imports executor code.

Reuse, don't duplicate: team normalization, the cross-provider equivalence map, fixture matching, the
canonical market reverse-index, and the corrected `_oddspapi_has_active` gate are imported from
src.theoddsapi — Polymarket events match canonical fixtures by the SAME team-identity + date rule and
resolve to the SAME marketId/outcomeIds.

Safety posture mirrors kalshi: a mis-mapped leg is a phantom arb, so a (fixture, book) is injected
only on an exact team-identity + date match (cross-sport search noise — e.g. a T20-cricket "World
Cup" event — is dropped because its team-set never matches an in-window soccer fixture), home/draw/
away come from the canonical fixture identity (never a provider tag), and the source stays SHADOW
(config polymarket.actionable false) until verified across a few live runs.
"""
from __future__ import annotations

import concurrent.futures
import json
import re
import threading
import time
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from . import bookmath, formatting

# Reused verbatim from the the-odds-api source — same normalization, matcher, market index, and the
# corrected "override OddsPapi's suspended/missing book" gate (_oddspapi_has_active). JSON-string
# array parsing (Gamma encodes outcomes/clobTokenIds as JSON strings) lives in _as_list below.
from .theoddsapi import (  # noqa: F401  (re-exported for the merge + tests)
    SCOPE_PER_GAME,
    FixtureMatch,
    MarketIndex,
    _oddspapi_has_active,
    build_market_index,
    match_event_to_fixture,
    normalize_team,
)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
USER_AGENT = "soccer-papi/1.0 (+read-only odds discovery; no auth, no trading)"

# A WC match event's date is the kickoff's UTC calendar day; we anchor it at noon UTC and allow ±36h
# so a fixture in the small hours of an adjacent UTC day still matches (same ±1-day cross-midnight
# tolerance kalshi uses for its US-local event-ticker date). A WC team-pair is unique inside a 2-day
# window, so this stays unambiguous — match_event_to_fixture still drops any pair matching two
# in-window fixtures as `ambiguous`, never guessing.
_DAY_MATCH_TOLERANCE_MIN = 36 * 60

# Cheap bulk catch for already-deployed near-term WC events; the per-fixture team searches below are
# what actually guarantee coverage of every in-window fixture.
_BROAD_TERMS = ("FIFA World Cup",)
MAX_SEARCH_TERMS = 80      # safety cap on /public-search calls per cycle
MAX_EVENTS = 60            # safety cap on events we price (CLOB /book calls) per cycle

# Depth-aware pricing: default % worse than the best ask the fillable band (the leg's `limit`) may
# span (config overrides it).
DEFAULT_DEPTH_SLIPPAGE_PCT = 2.0

# Depth-aware pricing: the per-leg stake we WALK the ask ladder to for the quoted decimal (avg fill),
# in CLOB book size units (shares) — the same target the probe walks ("REAL book: avg fill"). Pricing
# happens before arbitrage.py sizes the legs, so we walk to this reference stake (config overrides it)
# rather than the single best-ask tick. 0 -> walk to the leg's own fillable band instead.
DEFAULT_WALK_STAKE = 10000.0

# The tie-outcome label. The OG scanner keys the draw leg across the OTHER books (1xbet, Pinnacle,
# Kalshi) by the canonical draw outcomeId, but renders it as this exact human label via
# formatting.outcome_label (the "X"/draw -> "Draw" rule). We derive the constant FROM that renderer so
# the Polymarket draw leg carries byte-identical casing/string and aligns with the other venues'
# draw leg instead of being dropped or mispaired. (The cross-book merge still pairs by draw_oid; this
# is the label that keeps name-keyed matching and human output consistent — never None.)
DRAW_LABEL = formatting.outcome_label("X")     # == "Draw"


# --------------------------------------------------------------------------- #
# HTTP client (public Gamma + CLOB read endpoints; NO auth, NO trading)          #
# --------------------------------------------------------------------------- #
class PolymarketError(Exception):
    """Any failure talking to Polymarket. Caught in run.py so it never breaks the OddsPapi run."""


class PolymarketRateLimited(PolymarketError):
    """Transient HTTP 429 / 5xx — retried with exponential backoff."""


class PolymarketClient:
    """Thin client over Polymarket's public Gamma + CLOB read endpoints. Unauthenticated GETs only:
    discovery via Gamma, prices/depth via CLOB. Calls are (a) throttled to >= min_interval apart and
    (b) retried on 429/5xx with exponential backoff — mirrors src.kalshi.KalshiClient exactly. Never
    sweep all markets — discover by team/series search."""

    def __init__(self, *, gamma_base: str = GAMMA_BASE, clob_base: str = CLOB_BASE,
                 timeout: float = 30.0, session: requests.Session | None = None,
                 min_interval: float = 0.5) -> None:
        self.gamma_base = (gamma_base or GAMMA_BASE).rstrip("/")
        self.clob_base = (clob_base or CLOB_BASE).rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.min_interval = min_interval     # min seconds between requests (rate-limit cushion)
        self._last_request_ts = 0.0

    # -- Gamma (discovery) ---------------------------------------------------
    def search(self, q: str) -> Any:
        """GET /public-search?q= — fuzzy search over events/tags by title/slug/team name. Returns
        {events:[...], ...}; each event carries its nested markets (with question + clobTokenIds)."""
        return self._get(self.gamma_base + "/public-search", {"q": q})

    def event(self, event_id: Any) -> Any:
        """GET /events/{id} — one event with FULL nested markets (incl. groupItemTitle). Kept for
        completeness; the merge parses the search payload directly (which carries question), so this
        is only needed if a future Gamma change drops `question` from search results."""
        return self._get(self.gamma_base + f"/events/{event_id}", {})

    def events_by_slug(self, slug: str) -> Any:
        """GET /events?slug= — the event(s) with that exact slug, full nested markets. Used to reach
        a game's SIBLING events: the per-match "Game Lines" (totals/BTTS/spreads) live in a separate
        event whose slug is the 1x2 game slug + a suffix (e.g. '<game-slug>-more-markets'), NOT in the
        1x2 negRisk event and NOT surfaced by /public-search."""
        return self._get(self.gamma_base + "/events", {"slug": slug})

    def events_by_series(self, series_slug: str, *, closed: Optional[bool] = False,
                         page_limit: int = 100, max_pages: int = 50) -> list[dict]:
        """GET /events?series_slug=<...> PAGINATED (offset) -> the flat list of every event in a series
        (each with nested markets). This is how to enumerate ALL of a game's sibling events: a WC game
        is one 1x2 event PLUS many siblings ('<slug>-more-markets', '-total-corners', '-exact-score',
        '-halftime-result', '-player-props', …) that /public-search does NOT surface by slug. Filter
        the returned events by ``slug.startswith(<game-slug>)`` to get one game's full market set."""
        out: list[dict] = []
        for page in range(max_pages):
            params: dict[str, Any] = {"series_slug": series_slug, "limit": page_limit,
                                      "offset": page * page_limit}
            if closed is not None:
                params["closed"] = "true" if closed else "false"
            data = self._get(self.gamma_base + "/events", params)
            batch = data if isinstance(data, list) else (
                data.get("events") if isinstance(data, dict) else None)
            batch = [e for e in (batch or []) if isinstance(e, dict)]
            out.extend(batch)
            if len(batch) < page_limit:
                break
        return out

    # -- CLOB (prices + depth) ----------------------------------------------
    def book(self, token_id: str) -> Any:
        """GET /book?token_id= — full resting order book (bids/asks each {price,size}). Best ask =
        lowest ask price = the buy price; its size is the real per-leg limit."""
        return self._get(self.clob_base + "/book", {"token_id": token_id})

    def price_tokens(self, tokens: list[str], max_workers: int = 8, *,
                     depth_pricing: bool = False,
                     slippage_pct: float = DEFAULT_DEPTH_SLIPPAGE_PCT,
                     reference_size: float = DEFAULT_WALK_STAKE) -> dict[str, Optional[tuple[float, float]]]:
        """CONCURRENTLY price many CLOB tokens -> {token: (decimal_odds, limit) | None}. This is the
        scan's hot path: pricing legs sequentially through the 0.5s throttle dominated wall-time, so
        here we fan out up to `max_workers` GETs at once (bounded concurrency = the rate-limit cushion;
        the per-request throttle is bypassed). Each worker has its own Session (Sessions are not
        guaranteed thread-safe) and retries 429/5xx with bounded backoff. Drop-safe per token.

        ``depth_pricing`` (with ``slippage_pct`` + ``reference_size``) walks the ask ladder to the
        per-leg stake for an honest avg-fill decimal + fillable limit instead of the single best-ask
        tick; default False keeps the legacy top-of-book quote."""
        uniq = [t for t in dict.fromkeys(tokens) if t]
        out: dict[str, Optional[tuple[float, float]]] = {}
        if not uniq:
            return out
        local = threading.local()

        def _session() -> requests.Session:
            s = getattr(local, "s", None)
            if s is None:
                s = requests.Session()
                s.headers.setdefault("User-Agent", USER_AGENT)
                local.s = s
            return s

        def _one(token: str) -> Optional[tuple[float, float]]:
            for attempt in range(4):
                try:
                    r = _session().get(self.clob_base + "/book", params={"token_id": token},
                                       timeout=self.timeout)
                except (requests.ConnectionError, requests.Timeout):
                    return None
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                if r.status_code >= 400:
                    return None
                try:
                    book = r.json()
                except ValueError:
                    return None
                return _price_from_book(book, depth_pricing=depth_pricing, slippage_pct=slippage_pct,
                                        reference_size=reference_size)
            return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
            for token, res in zip(uniq, ex.map(_one, uniq)):
                out[token] = res
        return out

    # -- transport (throttle + retry/backoff, mirrors KalshiClient) ----------
    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        wait = self.min_interval - (time.monotonic() - self._last_request_ts)
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.monotonic()

    @retry(retry=retry_if_exception_type(PolymarketRateLimited),
           wait=wait_exponential(multiplier=1, min=1, max=30),
           stop=stop_after_attempt(5), reraise=True)
    def _get(self, url: str, params: dict[str, Any]) -> Any:
        self._throttle()
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise PolymarketError(f"network error on {url}: {exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise PolymarketRateLimited(f"{resp.status_code} on {url}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise PolymarketError(f"{resp.status_code} on {url}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise PolymarketError(f"non-JSON response on {url}: {resp.text[:200]}") from exc


# --------------------------------------------------------------------------- #
# Parsing helpers — Gamma encodes parallel arrays as JSON STRINGS                #
# --------------------------------------------------------------------------- #
def _as_list(v: Any) -> list[Any]:
    """Gamma returns `outcomes` / `clobTokenIds` / `outcomePrices` as JSON-encoded STRINGS
    (e.g. '["Yes","No"]'). Accept either a real list or that string form; [] on anything else."""
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def yes_token(market: dict[str, Any]) -> Optional[str]:
    """The CLOB token to BACK an outcome = the clobTokenId paired with the "Yes" outcome. None if the
    market has no Yes leg or isn't deployed to the CLOB yet."""
    labels = _as_list(market.get("outcomes"))
    tokens = _as_list(market.get("clobTokenIds"))
    for i, lab in enumerate(labels):
        if str(lab).strip().lower() == "yes" and i < len(tokens) and tokens[i]:
            return str(tokens[i])
    return None


_WIN_RE = re.compile(r"^\s*will\s+(.*?)\s+win\b", re.IGNORECASE)
_PAREN_SUFFIX_RE = re.compile(r"\s*\(.*\)\s*$")


def leg_role(market: dict[str, Any]) -> Optional[tuple[str, Optional[str]]]:
    """Classify one binary market into ("draw", None) or ("team", <raw team name>), or None.

    Prefers `groupItemTitle` (the clean identity: "Côte d'Ivoire" / "Draw (A vs. B)") and falls back
    to the market `question` ("Will <team> win on <date>?" / "...end in a draw?") when groupItemTitle
    is absent — Gamma's /public-search omits groupItemTitle but always carries `question`."""
    git = str(market.get("groupItemTitle") or "").strip()
    q = str(market.get("question") or "").strip()
    if git.lower().startswith("draw") or "end in a draw" in q.lower():
        return "draw", None
    if git:
        team = _PAREN_SUFFIX_RE.sub("", git).strip()   # drop any " (A vs. B)" suffix
        if team:
            return "team", team
    m = _WIN_RE.match(q)
    if m:
        return "team", m.group(1).strip()
    return None


# FULL-GAME total goals only: groupItemTitle "O/U 2.5". The half/team variants carry a prefix
# ("1st Half O/U 2.5", "2nd Half O/U 0.5", "Czechia O/U 1.5", "South Africa O/U 2.5") and are
# deliberately EXCLUDED — different scope from the canonical full-time totals.
_OU_FULL_RE = re.compile(r"^\s*O/U\s+(\d+(?:\.\d+)?)\s*$", re.IGNORECASE)


def _outcome_token(market: dict[str, Any], want_label: str) -> Optional[str]:
    """The clobTokenId paired with the outcome labelled `want_label` (e.g. 'over'/'under'/'yes'/'no').
    None if absent or the market isn't on the CLOB yet."""
    outs = _as_list(market.get("outcomes"))
    toks = _as_list(market.get("clobTokenIds"))
    for i, lab in enumerate(outs):
        if str(lab).strip().lower() == want_label and i < len(toks) and toks[i]:
            return str(toks[i])
    return None


def parse_more_markets_tokens(event: dict[str, Any]) -> tuple[dict[float, tuple[str, str]], Optional[tuple[str, str]]]:
    """From a '<game>-more-markets' Gamma event, extract the CLOB tokens for FULL-GAME total goals
    O/U and Both-Teams-To-Score only. Returns (totals, btts):
      totals = {line: (over_token, under_token)}
      btts   = (yes_token, no_token) | None
    Half/team O/U and half BTTS are excluded (different scope). Each is a binary Yes/No-style market
    with parallel outcomes/clobTokenIds arrays."""
    totals: dict[float, tuple[str, str]] = {}
    btts: Optional[tuple[str, str]] = None
    for m in (event.get("markets") or []):
        if not isinstance(m, dict):
            continue
        git = str(m.get("groupItemTitle") or "").strip()
        mo = _OU_FULL_RE.match(git)
        if mo:
            try:
                line = float(mo.group(1))
            except ValueError:
                continue
            over, under = _outcome_token(m, "over"), _outcome_token(m, "under")
            if over and under:
                totals[line] = (over, under)
        elif git.lower() == "both teams to score":     # exact -> full game (halves have a suffix)
            yes, no = _outcome_token(m, "yes"), _outcome_token(m, "no")
            if yes and no:
                btts = (yes, no)
    return totals, btts


# FULL-GAME spread/handicap: groupItemTitle "Czechia (-1.5)" / "South Africa (-2.5)". The two
# outcomes are the two TEAM names (named team covers its line; the other covers the negation). Half
# variants would carry a prefix and are excluded by the team-identity check in the merge.
_SPREAD_GIT_RE = re.compile(r"^\s*(?P<team>.+?)\s*\((?P<line>[+-]?\d+(?:\.\d+)?)\)\s*$")


def parse_spread_markets(event: dict[str, Any]) -> list[dict[str, Any]]:
    """From a '<game>-more-markets' event, extract full-game spread markets as
    [{named, line, opp, named_token, opp_token}]. `named` covers `line`; `opp` covers -line. The
    merge resolves named/opp to the canonical fixture's p1/p2 and refuses anything else."""
    out: list[dict[str, Any]] = []
    for m in (event.get("markets") or []):
        if not isinstance(m, dict):
            continue
        mo = _SPREAD_GIT_RE.match(str(m.get("groupItemTitle") or "").strip())
        if not mo:
            continue
        try:
            line = float(mo.group("line"))
        except ValueError:
            continue
        named = mo.group("team").strip()
        outs, toks = _as_list(m.get("outcomes")), _as_list(m.get("clobTokenIds"))
        if len(outs) != 2 or len(toks) < 2:
            continue
        named_i = next((i for i, o in enumerate(outs) if str(o).strip().lower() == named.lower()), None)
        if named_i is None:
            continue
        opp_i = 1 - named_i
        if not toks[named_i] or not toks[opp_i]:
            continue
        out.append({"named": named, "line": line, "opp": str(outs[opp_i]),
                    "named_token": str(toks[named_i]), "opp_token": str(toks[opp_i])})
    return out


_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _date_part(v: Any) -> Optional[str]:
    """Pull a YYYY-MM-DD date out of any ISO-ish string (handles 'T'/' ' separators)."""
    m = _DATE_RE.search(str(v or ""))
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _commence_iso(event: dict[str, Any], markets: list[dict[str, Any]]) -> Optional[str]:
    """The match's UTC calendar day anchored at 12:00:00Z (mirrors kalshi._event_commence_iso). We
    take the date from the event (eventDate/startTime) or, failing that, a market's gameStartTime or
    its "win on <date>" question — NEVER the event `startDate`, which is a creation artifact. None if
    no date is recoverable."""
    for v in (event.get("eventDate"), event.get("startTime"), event.get("endDate")):
        d = _date_part(v)
        if d:
            return d + "T12:00:00Z"
    for m in markets:
        d = _date_part(m.get("gameStartTime")) or _date_part(m.get("question"))
        if d:
            return d + "T12:00:00Z"
    return None


# --------------------------------------------------------------------------- #
# Price + liquidity helpers                                                      #
# --------------------------------------------------------------------------- #
# UNITS — get this exactly right or it mints phantom arbs. The CLOB best ask is a price in DOLLARS in
# (0,1) = implied probability. To BACK an outcome you buy Yes at that ask, so the decimal odds are
# 1 / ask  ($0.32 -> 3.125). Do NOT read Gamma's outcomePrices (last/mid) and do NOT scale by 100.
def decimal_from_ask(ask: Any) -> Optional[float]:
    """Decimal odds to back an outcome = 1 / float(ask). $0.32 -> 3.125. None unless the ask is a
    real two-sided price in (0,1) dollars."""
    try:
        price = float(ask)
    except (TypeError, ValueError):
        return None
    if not (0.0 < price < 1.0):
        return None
    return 1.0 / price


def leg_limit(ask_size: Any, ask: Any) -> float:
    """Real max stake at the best ask = shares available × price = size × ask (dollars). A genuine
    limit — these legs are NOT low_confidence (unlike the-odds-api). 0 on bad input."""
    try:
        return float(ask_size) * float(ask)
    except (TypeError, ValueError):
        return 0.0


def _num(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def best_ask(book: Any) -> Optional[tuple[float, float]]:
    """(price, size) at the best ask of a CLOB order book — the LOWEST ask price (the buy price) and
    the shares resting there. None if the book has no usable ask in (0,1). Computed explicitly so we
    never rely on the array's order."""
    best: Optional[tuple[float, float]] = None
    for lvl in (book or {}).get("asks") or []:
        if not isinstance(lvl, dict):
            continue
        p, s = _num(lvl.get("price")), _num(lvl.get("size"))
        if p is None or s is None or not (0.0 < p < 1.0):
            continue
        if best is None or p < best[0]:
            best = (p, s)
    return best


def _ask_levels(book: Any) -> list[tuple[Any, Any]]:
    """Raw (price, size) ask levels from a CLOB book for bookmath (validation lives in bookmath)."""
    return [(lvl.get("price"), lvl.get("size"))
            for lvl in (book or {}).get("asks") or [] if isinstance(lvl, dict)]


def _price_from_book(book: Any, *, depth_pricing: bool = False,
                     slippage_pct: float = DEFAULT_DEPTH_SLIPPAGE_PCT,
                     reference_size: float = DEFAULT_WALK_STAKE) -> Optional[tuple[float, float]]:
    """(decimal_odds, limit_dollars) for one CLOB book, or None. THE single Poly leg-pricing point.

    ``depth_pricing`` False -> legacy single best-ask tick: decimal = 1/ask, limit = ask_size × ask.
    ``depth_pricing`` True  -> SIZE-AWARE walk-to-stake fill (the honest number):
      * decimal = 1 / (avg fill price walking the ask ladder cheapest-first to ``reference_size``
        shares via bookmath.walk_book) — the stake the arb intends for this leg. A deep favorite walks
        ~entirely at its best ask (≈ unchanged); a thin leg blends into worse levels (worse odds). This
        is the "REAL book: avg fill" the probe prints, matching Polymarket's "Avg. Price". When the
        ladder holds fewer than ``reference_size`` shares, walk_book averages over all that fills.
      * limit = the fillable DOLLAR depth within ``slippage_pct`` of the best ask (Σ price×size of the
        band) — UNCHANGED units (dollars), so arbitrage.py is untouched. The sizer caps stake at this
        limit, so the realized fill is no worse than the (deeper-walked) quoted decimal: conservative.
    ``reference_size`` <= 0 -> walk to the leg's own fillable band instead of a fixed reference."""
    if not depth_pricing:
        ask = best_ask(book)
        if ask is None:
            return None
        price, size = ask
        dec = decimal_from_ask(price)
        return None if dec is None else (dec, leg_limit(size, price))
    band = bookmath.vwap_within_band(_ask_levels(book), slippage_pct)
    if band is None:
        return None
    _band_vwap, band_shares = band
    limit = _band_vwap * band_shares                     # fillable dollar depth (the leg's stake cap)
    ladder = bookmath.valid_asks(_ask_levels(book))      # ascending, validated — walk cheapest-first
    stake = reference_size if reference_size and reference_size > 0 else band_shares
    walk = bookmath.walk_book(ladder, stake)
    dec = decimal_from_ask(walk.avg_price)
    return None if dec is None else (dec, limit)


# --------------------------------------------------------------------------- #
# Event model (priced legs handed to merge_into)                                #
# --------------------------------------------------------------------------- #
@dataclass
class Leg:
    """One binary Yes leg of a WC event. `role`/`team`/`yes_token` come from parsing; `decimal`/
    `limit` are filled by CLOB pricing (None until priced)."""
    role: str                         # "draw" | "team" (+ "over"/"under"/"yes"/"no"/"spread" extras)
    team: Optional[str] = None        # outcome label: raw team name for "team", DRAW_LABEL for "draw"
    yes_token: Optional[str] = None
    decimal: Optional[float] = None
    limit: Optional[float] = None
    # Execution metadata (additive): the CLOB token is `yes_token`; these are the order params the
    # executor needs. Populated best-effort at parse/price time; None where Gamma did not supply them
    # (the executor re-fetches tick_size/neg_risk at call time as a fallback).
    neg_risk: Optional[bool] = None
    tick_size: Optional[float] = None

    def __post_init__(self) -> None:
        # Guard: a leg must NEVER carry team=None. A null draw label is why draw legs were dropped or
        # mispaired downstream (leg matching is by name) and why probe scripts crashed formatting None.
        # Draw -> the canonical DRAW_LABEL that aligns with the other books' tie leg; any other label
        # that couldn't be derived falls back to the outcome string (the role), never None.
        if self.team is None:
            self.team = DRAW_LABEL if self.role == "draw" else self.role


@dataclass
class PolyEvent:
    event_id: str
    title: str
    commence_iso: Optional[str]
    legs: list[Leg]                   # the Yes legs (a valid WC event has exactly 1 draw + 2 team)
    slug: str = ""                    # game slug, used to reach the '<slug>-more-markets' siblings
    # SAFE-TIER extras priced from the sibling 'Game Lines' event (full-game, reg-time):
    totals: dict[float, tuple[Leg, Leg]] = field(default_factory=dict)  # line -> (over_leg, under_leg)
    btts: Optional[tuple[Leg, Leg]] = None                              # (yes_leg, no_leg)
    # Each: {named, line, opp, named_leg, opp_leg} — named covers line, opp covers -line (priced).
    spreads: list[dict[str, Any]] = field(default_factory=list)


def parse_event_legs(event: dict[str, Any]) -> Optional[PolyEvent]:
    """Pure parse of a Gamma event dict -> PolyEvent with unpriced legs, or None unless it is a
    well-formed WC match: EXACTLY one draw market and two team-win markets, each binary with a Yes
    token. (This shape check also drops cross-sport search noise before any CLOB call.)"""
    markets = [m for m in (event.get("markets") or []) if isinstance(m, dict)]
    event_neg = event.get("negRisk")
    legs: list[Leg] = []
    draws = teams = 0
    for m in markets:
        role = leg_role(m)
        if role is None:
            continue
        kind, team = role
        tok = yes_token(m)
        if tok is None:
            continue
        neg = m.get("negRisk")
        neg = bool(neg) if neg is not None else (bool(event_neg) if event_neg is not None else None)
        tick = _num(m.get("orderPriceMinTickSize"))
        if kind == "draw":
            # Populate the previously-None draw label with the canonical tie label so it aligns with
            # the other books' draw leg (pricing/decimal/limit untouched — only the label).
            legs.append(Leg(role="draw", team=DRAW_LABEL, yes_token=tok, neg_risk=neg, tick_size=tick))
            draws += 1
        else:
            legs.append(Leg(role="team", team=team, yes_token=tok, neg_risk=neg, tick_size=tick))
            teams += 1
    if draws != 1 or teams != 2:
        return None
    return PolyEvent(
        event_id=str(event.get("id") or event.get("slug") or ""),
        title=str(event.get("title") or ""),
        commence_iso=_commence_iso(event, markets),
        legs=legs,
        slug=str(event.get("slug") or ""),
    )


def _has_counterparty(raw_by_fixture: Optional[dict], fid: Optional[str], market_id: Any) -> bool:
    """True if some OTHER book already priced this canonical marketId on this fixture — so a Poly leg
    here has something to pair against. No counterparty -> don't waste a CLOB call (it can't arb)."""
    if not raw_by_fixture or fid is None:
        return False
    fx = raw_by_fixture.get(fid) or {}
    mid = str(market_id)
    return any(mid in (book.get("markets") or {}) for book in (fx.get("bookmakerOdds") or {}).values())


def _match_fid(parsed: PolyEvent, by_fixture: dict[str, dict[str, Any]]) -> Optional[str]:
    """The canonical fixtureId this event maps to (team identity + date), or None — computed BEFORE
    any pricing so we never CLOB-price an event that won't merge."""
    teams = [l.team for l in parsed.legs if l.role == "team" and l.team]
    if len(teams) != 2 or not parsed.commence_iso:
        return None
    fm, _ = match_event_to_fixture(teams[0], teams[1], parsed.commence_iso, by_fixture,
                                   _DAY_MATCH_TOLERANCE_MIN)
    return fm.fixture_id if fm is not None else None


def _spread_canonical(line: float, market_index) -> Optional[dict]:
    """The canonical spreads entry for a Poly handicap line (either sign), or None."""
    if abs(line * 2 - round(line * 2)) > 1e-9 or round(line * 2) % 2 == 0:
        return None                                    # quarter / whole-number -> push risk, skip
    return market_index.spreads.get(round(line, 2)) or market_index.spreads.get(round(-line, 2))


def _plan_extra_markets(client, parsed, market_index, fid, raw_by_fixture, log) -> dict[str, Any]:
    """Fetch the '<slug>-more-markets' sibling and return UNPRICED token plans for the totals / BTTS /
    spreads lines that (a) map to a canonical marketId AND (b) have a live counterparty on this
    fixture. No CLOB calls here — pricing happens later in one concurrent batch."""
    plan: dict[str, Any] = {"totals": {}, "btts": None, "spreads": []}
    if not parsed.slug:
        return plan
    try:
        sib = client.events_by_slug(f"{parsed.slug}-more-markets")
    except PolymarketError as exc:
        log.warning("[POLYMARKET] %s-more-markets fetch failed (%s) — 1x2 only.", parsed.slug, exc)
        return plan
    sib = (sib[0] if isinstance(sib, list) and sib else sib)
    if not isinstance(sib, dict):
        return plan
    tot_tokens, btts_tokens = parse_more_markets_tokens(sib)
    for line, toks in tot_tokens.items():
        spec = market_index.totals.get(line)
        if spec and _has_counterparty(raw_by_fixture, fid, spec["marketId"]):
            plan["totals"][line] = toks
    if btts_tokens and market_index.btts and _has_counterparty(raw_by_fixture, fid, market_index.btts["marketId"]):
        plan["btts"] = btts_tokens
    for sp in parse_spread_markets(sib):
        spec = _spread_canonical(sp["line"], market_index)
        if spec and _has_counterparty(raw_by_fixture, fid, spec["marketId"]):
            plan["spreads"].append(sp)
    return plan


def price_leg(client: PolymarketClient, token: str, *, depth_pricing: bool = False,
              slippage_pct: float = DEFAULT_DEPTH_SLIPPAGE_PCT,
              reference_size: float = DEFAULT_WALK_STAKE) -> Optional[tuple[float, float]]:
    """(decimal_odds, limit) for one Yes token, or None if it has no live ask (undeployed / empty
    book / 4xx). Network — kept tiny so the rest of the parse stays testable. ``depth_pricing`` walks
    the ask ladder to ``reference_size`` (avg fill, within ``slippage_pct`` for the limit) instead of
    the single best-ask tick; default off."""
    try:
        book = client.book(token)
    except PolymarketError:
        return None
    return _price_from_book(book, depth_pricing=depth_pricing, slippage_pct=slippage_pct,
                            reference_size=reference_size)


# --------------------------------------------------------------------------- #
# Discovery + fetch (network)                                                   #
# --------------------------------------------------------------------------- #
# FIFA 3-letter nation codes -> the deterministic WC match-slug key. Polymarket's WC match markets
# are slugged fifwc-<team3>-<team3>-<YYYY-MM-DD> (e.g. fifwc-bra-jpn-2026-06-29), so we can fetch a
# fixture's event DIRECTLY by slug instead of relying on /public-search (which buries the match
# market under unrelated noise — "Brazil" surfaces the presidential-election market, never the game).
# Keys are written human-readable and normalized through normalize_team at load, so every provider
# spelling (and the _TEAM_EQUIV collapses: "Côte d'Ivoire"/"Ivory Coast", "USA"/"United States",
# "IR Iran"/"Iran", "South Korea"/"Korea Republic") resolves to the same code.
_FIFA_CODES_RAW: dict[str, str] = {
    "Argentina": "arg", "Australia": "aus", "Austria": "aut", "Belgium": "bel", "Bolivia": "bol",
    "Bosnia and Herzegovina": "bih", "Brazil": "bra", "Burkina Faso": "bfa", "Cameroon": "cmr",
    "Canada": "can", "Cape Verde": "cpv", "Chile": "chi", "China PR": "chn", "Colombia": "col",
    "Costa Rica": "crc", "Côte d'Ivoire": "civ", "Croatia": "cro", "Curaçao": "cuw", "Czechia": "cze",
    "Denmark": "den", "DR Congo": "cod", "Ecuador": "ecu", "Egypt": "egy", "England": "eng",
    "Finland": "fin", "France": "fra", "Gabon": "gab", "Georgia": "geo", "Germany": "ger",
    "Ghana": "gha", "Greece": "gre", "Guinea": "gui", "Haiti": "hai", "Honduras": "hon",
    "Hungary": "hun", "Iceland": "isl", "Indonesia": "idn", "Iran": "irn", "Iraq": "irq",
    "Republic of Ireland": "irl", "Israel": "isr", "Italy": "ita", "Jamaica": "jam", "Japan": "jpn",
    "Jordan": "jor", "Kazakhstan": "kaz", "Korea DPR": "prk", "Korea Republic": "kor", "Kosovo": "kvx",
    "Kuwait": "kuw", "Mali": "mli", "Mauritania": "mtn", "Mexico": "mex", "Montenegro": "mne",
    "Morocco": "mar", "Mozambique": "moz", "Namibia": "nam", "Netherlands": "ned", "New Zealand": "nzl",
    "Nigeria": "nga", "North Macedonia": "mkd", "Northern Ireland": "nir", "Norway": "nor",
    "Oman": "oma", "Panama": "pan", "Paraguay": "par", "Peru": "per", "Poland": "pol",
    "Portugal": "por", "Qatar": "qat", "Romania": "rou", "Russia": "rus", "Saudi Arabia": "ksa",
    "Senegal": "sen", "Serbia": "srb", "Scotland": "sco", "Sierra Leone": "sle", "Slovakia": "svk",
    "Slovenia": "svn", "South Africa": "rsa", "Spain": "esp", "Sweden": "swe", "Switzerland": "sui",
    "Syria": "syr", "Thailand": "tha", "Togo": "tog", "Tunisia": "tun", "Türkiye": "tur",
    "Uganda": "uga", "Ukraine": "ukr", "United Arab Emirates": "uae", "Uruguay": "uru", "USA": "usa",
    "Uzbekistan": "uzb", "Venezuela": "ven", "Vietnam": "vie", "Wales": "wal", "Zambia": "zam",
}
_FIFA_CODES: dict[str, str] = {normalize_team(name): code for name, code in _FIFA_CODES_RAW.items()}


def _team_code(name: Any) -> Optional[str]:
    """FIFA 3-letter code for a team name (any provider spelling), or None if it isn't a coded WC
    nation (so we never build a slug for a club / non-WC fixture)."""
    return _FIFA_CODES.get(normalize_team(name))


def _wc_slug_candidates(info: dict[str, Any]) -> list[str]:
    """Deterministic Polymarket WC match slugs for one by_fixture entry, or [] if either team has no
    FIFA code (non-WC / unknown nation) or the start date is unrecoverable. Tries BOTH team orderings
    (Polymarket's home/away order is not guaranteed to match ours) and the kickoff's UTC day plus the
    prior day (a small-hours UTC kickoff can be listed under the previous US-local calendar date) —
    the discovery loop fetches them in order and stops at the first hit."""
    c1, c2 = _team_code(info.get("p1")), _team_code(info.get("p2"))
    d = _date_part(info.get("start_time"))
    if not c1 or not c2 or not d:
        return []
    dates = [d]
    try:
        dates.append((datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d"))
    except ValueError:
        pass
    slugs: list[str] = []
    for dd in dates:
        for a, b in ((c1, c2), (c2, c1)):
            s = f"fifwc-{a}-{b}-{dd}"
            if s not in slugs:
                slugs.append(s)
    return slugs


def _events_from_slug(data: Any) -> list[dict]:
    """The event dicts in a Gamma /events?slug= response (a bare JSON array, or a {events:[...]} dict
    on older shapes) — open events only."""
    evs = data if isinstance(data, list) else (data.get("events") if isinstance(data, dict) else None)
    return [e for e in (evs or []) if isinstance(e, dict) and not e.get("closed")]


def _search_terms(by_fixture: dict[str, dict[str, Any]]) -> list[str]:
    """One broad term plus each distinct in-window fixture team name — targeted, like kalshi querying
    its one series. De-duplicated and capped."""
    terms: list[str] = []
    seen: set[str] = set()
    for t in (*_BROAD_TERMS, *(
        str(info.get(k)) for info in by_fixture.values() for k in ("p1", "p2") if info.get(k)
    )):
        key = t.strip().lower()
        if key and key not in seen:
            seen.add(key)
            terms.append(t)
    return terms[:MAX_SEARCH_TERMS]


def _discover_events(client: PolymarketClient, by_fixture: dict[str, dict[str, Any]], log) -> dict[str, dict]:
    """Find each fixture's WC event and return {event_id: event} de-duplicated, skipping closed events.

    PRIMARY: fetch every fixture DIRECTLY by its deterministic match slug (events_by_slug) — this is
    how a game is reliably found, since keyword search buries the match market under unrelated noise.
    FALLBACK: keyword /public-search, but ONLY for fixtures the slug path did not resolve (non-coded
    nations, or a slug Polymarket spells differently). Every failure is logged and skipped — the feed
    must never break the run."""
    found: dict[str, dict] = {}
    slug_hit_fids: set[str] = set()
    for fid, info in by_fixture.items():
        for slug in _wc_slug_candidates(info):
            try:
                evs = _events_from_slug(client.events_by_slug(slug))
            except PolymarketError as exc:
                log.warning("[POLYMARKET] slug fetch %r failed (%s) — trying next.", slug, exc)
                continue
            for ev in evs:
                eid = str(ev.get("id") or ev.get("slug") or "")
                if eid:
                    found.setdefault(eid, ev)
                    slug_hit_fids.add(fid)
            if fid in slug_hit_fids:
                break                                  # first ordering/date that hits wins

    remaining = {fid: info for fid, info in by_fixture.items() if fid not in slug_hit_fids}
    for term in _search_terms(remaining) if remaining else []:
        try:
            data = client.search(term)
        except PolymarketError as exc:
            log.warning("[POLYMARKET] search %r failed (%s) — skipping that term.", term, exc)
            continue
        for ev in (data or {}).get("events") or []:
            if not isinstance(ev, dict) or ev.get("closed"):
                continue
            eid = str(ev.get("id") or ev.get("slug") or "")
            if eid:
                found.setdefault(eid, ev)
    return found


def fetch_wc_events(client: PolymarketClient, by_fixture: dict[str, dict[str, Any]], log,
                    market_index=None, raw_by_fixture: Optional[dict] = None,
                    max_workers: int = 8, *, depth_pricing: bool = False,
                    slippage_pct: float = DEFAULT_DEPTH_SLIPPAGE_PCT,
                    reference_size: float = DEFAULT_WALK_STAKE) -> list[PolyEvent]:
    """Discover WC match events and return priced PolyEvents the merge maps to fixtures. FAST PATH:
      1. parse + MATCH each event to a fixture FIRST (no network) — skip events that won't merge;
      2. plan which extra (totals/BTTS/spreads) lines to price — only canonical lines that have a
         live counterparty on this fixture (no counterparty -> can't arb -> don't price it);
      3. price EVERY needed CLOB token in ONE bounded-concurrency batch (the big speedup vs the old
         per-leg sequential throttle);
      4. assemble. An event with any unpriceable 1x2 leg is dropped (drop-safe)."""
    candidates = _discover_events(client, by_fixture, log)
    plans: list[dict[str, Any]] = []
    skipped_unmatched = 0
    matched_fids: set[str] = set()
    for ev in candidates.values():
        if len(plans) >= MAX_EVENTS:
            log.warning("[POLYMARKET] hit MAX_EVENTS=%s — not planning further candidates.", MAX_EVENTS)
            break
        parsed = parse_event_legs(ev)
        if parsed is None:
            continue
        fid = _match_fid(parsed, by_fixture)
        if fid is None:                                # won't merge -> never price it
            skipped_unmatched += 1
            continue
        matched_fids.add(fid)
        extra = (_plan_extra_markets(client, parsed, market_index, fid, raw_by_fixture, log)
                 if market_index is not None else {"totals": {}, "btts": None, "spreads": []})
        plans.append({"parsed": parsed, "extra": extra})

    # Surface every WC fixture discovery could NOT find an event for (instead of silently dropping it):
    # only fixtures we could address by slug — a coded WC nation pair — count as a miss worth flagging.
    unmatched_fixtures = 0
    for fid, info in by_fixture.items():
        if fid in matched_fids:
            continue
        slugs = _wc_slug_candidates(info)
        if not slugs:
            continue
        unmatched_fixtures += 1
        log.warning("[POLYMARKET] UNMATCHED fixture %s vs %s (tried slug=%s)",
                    info.get("p1"), info.get("p2"), slugs[0])

    # Collect every token to price, then fetch them all concurrently in one batch.
    tokens: list[str] = []
    for pl in plans:
        tokens += [leg.yes_token for leg in pl["parsed"].legs if leg.yes_token]
        for over_tok, under_tok in pl["extra"]["totals"].values():
            tokens += [over_tok, under_tok]
        if pl["extra"]["btts"]:
            tokens += list(pl["extra"]["btts"])
        for sp in pl["extra"]["spreads"]:
            tokens += [sp["named_token"], sp["opp_token"]]
    priced = client.price_tokens(tokens, max_workers=max_workers,
                                 depth_pricing=depth_pricing, slippage_pct=slippage_pct,
                                 reference_size=reference_size)

    out: list[PolyEvent] = []
    tot_lines = btts_n = spread_lines = 0
    for pl in plans:
        parsed, extra = pl["parsed"], pl["extra"]
        ok = True
        for leg in parsed.legs:
            v = priced.get(leg.yes_token)
            if v is None:
                ok = False
                break
            leg.decimal, leg.limit = v
        if not ok:
            continue
        for line, (over_tok, under_tok) in extra["totals"].items():
            vo, vu = priced.get(over_tok), priced.get(under_tok)
            if vo and vu:
                parsed.totals[line] = (Leg(role="over", yes_token=over_tok, decimal=vo[0], limit=vo[1]),
                                       Leg(role="under", yes_token=under_tok, decimal=vu[0], limit=vu[1]))
                tot_lines += 1
        if extra["btts"]:
            vy, vn = priced.get(extra["btts"][0]), priced.get(extra["btts"][1])
            if vy and vn:
                parsed.btts = (Leg(role="yes", yes_token=extra["btts"][0], decimal=vy[0], limit=vy[1]),
                               Leg(role="no", yes_token=extra["btts"][1], decimal=vn[0], limit=vn[1]))
                btts_n += 1
        for sp in extra["spreads"]:
            vn_, vo_ = priced.get(sp["named_token"]), priced.get(sp["opp_token"])
            if vn_ and vo_:
                parsed.spreads.append({
                    "named": sp["named"], "line": sp["line"], "opp": sp["opp"],
                    "named_leg": Leg(role="spread", yes_token=sp["named_token"], decimal=vn_[0], limit=vn_[1]),
                    "opp_leg": Leg(role="spread", yes_token=sp["opp_token"], decimal=vo_[0], limit=vo_[1])})
                spread_lines += 1
        out.append(parsed)
    log.info("[POLYMARKET] discovered %s candidate(s); %s matched & priced (%s candidate(s) unmatched, "
             "%s WC fixture(s) UNMATCHED) | %s CLOB token(s) priced concurrently "
             "(+%s total, %s BTTS, %s spread line(s)).",
             len(candidates), len(out), skipped_unmatched, unmatched_fixtures,
             len(priced), tot_lines, btts_n, spread_lines)
    return out


# --------------------------------------------------------------------------- #
# Leg construction (canonical bookmakerOdds fragment)                            #
# --------------------------------------------------------------------------- #
def _venue(leg: "Leg") -> dict[str, Any]:
    """Execution metadata for a Polymarket leg: the exact CLOB token we priced (BACK = BUY its Yes
    token), plus neg_risk / tick_size when Gamma supplied them. Persisted so the executor re-pulls
    the SAME token's book rather than re-discovering a possibly-different token."""
    return {"venue": "polymarket", "venueId": leg.yes_token, "venueSide": "BUY",
            "negRisk": leg.neg_risk, "tickSize": leg.tick_size}


def _player_line(price: float, limit: float, changed_at: str,
                 venue: dict[str, Any] | None = None) -> dict[str, Any]:
    """One canonical priced outcome carrying a REAL limit (size×price) so the engine does NOT mark it
    low_confidence. changedAt = scan time: a best-ask is a live resting order, not a stale line.

    ``venue`` (additive, optional) records the execution identifiers (token + side + neg_risk/
    tick_size) for the automated executor; it never affects the arb math or the manual pipeline."""
    line = {"price": price, "priceAmerican": None, "limit": limit,
            "changedAt": changed_at, "mainLine": True, "active": True}
    if venue:
        line.update(venue)
    return line


def _add_leg(entry: dict[str, Any], mid: int, oid: int, price: float, limit: float, changed_at: str,
             venue: dict[str, Any] | None = None) -> None:
    mkt = entry["markets"].setdefault(str(mid), {"marketActive": True, "outcomes": {}})
    mkt["outcomes"][str(oid)] = {"players": {"0": _player_line(price, limit, changed_at, venue)}}


def _is_no_push_half_line(line: float) -> bool:
    """True only for half-lines (±0.5, ±1.5, ±2.5 …): line*2 is an ODD integer. Whole-number lines
    (push on exact margin) and quarter-lines are rejected."""
    return abs(line * 2 - round(line * 2)) < 1e-9 and round(line * 2) % 2 != 0


def _inject_spreads(entry: dict[str, Any], spreads: list[dict[str, Any]], fm: FixtureMatch,
                    market_index: MarketIndex, changed_at: str) -> int:
    """Map Poly handicap markets to canonical spreads[line-on-p1], home=p1-covers / away=p2-covers.
    Returns #lines injected. SIGN GATE: the canonical line is derived from the fixture's p1/p2
    IDENTITY, and we require the market's two outcomes to be EXACTLY this fixture's two teams — so a
    mislabelled/flipped pair can never land on a complementary canonical outcome (it is skipped, or
    lands on a different line that the counterparty does not share)."""
    n = 0
    pair = {fm.p1_norm, fm.p2_norm}
    for sp in spreads:
        line = sp["line"]
        if not _is_no_push_half_line(line):
            continue
        named, opp = normalize_team(sp["named"]), normalize_team(sp["opp"])
        if named == opp or {named, opp} != pair:        # must be exactly this fixture's two teams
            continue
        if named == fm.p1_norm:                          # p1 covers `line`
            p1_pt, p1_leg, p2_leg = line, sp["named_leg"], sp["opp_leg"]
        else:                                            # named is p2 -> p1 covers the negation
            p1_pt, p1_leg, p2_leg = -line, sp["opp_leg"], sp["named_leg"]
        spec = market_index.spreads.get(round(p1_pt, 2))
        if not spec or spec.get("scope") != SCOPE_PER_GAME:
            continue
        _add_leg(entry, spec["marketId"], spec["home_oid"], p1_leg.decimal, p1_leg.limit, changed_at,
                 _venue(p1_leg))
        _add_leg(entry, spec["marketId"], spec["away_oid"], p2_leg.decimal, p2_leg.limit, changed_at,
                 _venue(p2_leg))
        n += 1
    return n


# --------------------------------------------------------------------------- #
# Coverage report (mirrors kalshi.Coverage)                                     #
# --------------------------------------------------------------------------- #
@dataclass
class Coverage:
    events_total: int = 0
    matched: int = 0
    unmatched_name: list[str] = field(default_factory=list)
    time_mismatch: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    recovered: int = 0                                       # #fixtures where polymarket was injected
    deferred: int = 0                                        # #fixtures OddsPapi already had active
    incomplete: list[str] = field(default_factory=list)      # matched but not exactly 1 draw + 2 teams
    totals_fixtures: int = 0                                  # #fixtures with >=1 total-goals line injected
    totals_lines: int = 0                                     # total O/U lines injected across fixtures
    btts_fixtures: int = 0                                    # #fixtures with a BTTS leg injected
    spreads_fixtures: int = 0                                 # #fixtures with >=1 spread line injected
    spreads_lines: int = 0                                    # spread (handicap) lines injected across fixtures

    def lines(self) -> list[str]:
        out = [
            f"POLYMARKET: {self.matched}/{self.events_total} events mapped "
            f"(unmatched-name {len(self.unmatched_name)}, time-mismatch {len(self.time_mismatch)}, "
            f"ambiguous {len(self.ambiguous)}) | recovered {self.recovered} fixture(s), "
            f"deferred {self.deferred} | SAFE-TIER: totals {self.totals_lines} line(s) on "
            f"{self.totals_fixtures} fixture(s), BTTS on {self.btts_fixtures} fixture(s), "
            f"spreads {self.spreads_lines} line(s) on {self.spreads_fixtures} fixture(s)",
        ]
        for label, names in (("unmatched-name", self.unmatched_name),
                             ("time-mismatch", self.time_mismatch), ("ambiguous", self.ambiguous),
                             ("incomplete (!= 1 draw + 2 teams)", self.incomplete)):
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
    events: list[PolyEvent],
    *,
    now,
    log,
) -> tuple[Coverage, dict[str, set[str]]]:
    """Merge priced Polymarket-direct events into raw_by_fixture (fill the `polymarket` slug) and
    return (coverage, poly_books) where poly_books maps canonical fixtureId -> {"polymarket"} for
    every fixture this source injected — so run.py keeps those legs SHADOW on first rollout, exactly
    like the kalshi kalshi_books contract.

    Each event has 3 priced Yes legs — home win / Draw / away win. Outcomes are keyed by team
    IDENTITY (the "draw" leg -> draw; the two team legs matched to the canonical fixture's p1/p2 via
    normalize_team), never by market order — and home/away come from the fixture's p1/p2 identity,
    never a Polymarket tag (the anti-phantom rule). An event is injected only if it matches exactly
    one in-window fixture AND all three legs are priced. Defers to an already-active OddsPapi
    polymarket book."""
    cov = Coverage()
    poly_books: dict[str, set[str]] = {}
    h2h = market_index.h2h
    if not h2h:
        log.warning("[POLYMARKET] no canonical Full Time Result (1x2) market in the index — cannot map; skipping.")
        return cov, poly_books

    cov.events_total = len(events)
    changed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    for ev in events:
        label = ev.title or ev.event_id or "(no title)"

        # Split the priced legs into the draw leg and the two team legs, by team identity.
        draw_leg: Optional[Leg] = None
        team_legs: dict[str, Leg] = {}
        team_raw_names: list[str] = []
        for leg in ev.legs:
            if leg.decimal is None or leg.limit is None:
                continue                                   # unpriced -> ignore
            if leg.role == "draw":
                draw_leg = leg
            elif leg.role == "team" and leg.team:
                team_legs[normalize_team(leg.team)] = leg
                team_raw_names.append(leg.team)

        if draw_leg is None or len(team_legs) != 2:
            cov.incomplete.append(label)
            log.warning("[POLYMARKET] %s: need 1 priced draw + 2 priced team legs (got draw=%s, teams=%s) — skipping.",
                        label, draw_leg is not None, len(team_legs))
            continue

        if ev.commence_iso is None:
            cov.unmatched_name.append(label)
            log.warning("[POLYMARKET] %s: no recoverable match date — skipping.", label)
            continue

        fm, reason = match_event_to_fixture(team_raw_names[0], team_raw_names[1], ev.commence_iso,
                                            by_fixture, _DAY_MATCH_TOLERANCE_MIN)
        if fm is None:
            bucket = getattr(cov, reason, None)
            if isinstance(bucket, list):
                bucket.append(label)
            log.info("[POLYMARKET] %s (%s): no unique fixture match — skipping.", label, reason)
            continue

        home_leg = team_legs.get(fm.p1_norm)
        away_leg = team_legs.get(fm.p2_norm)
        if home_leg is None or away_leg is None:
            cov.incomplete.append(label)
            log.warning("[POLYMARKET] %s: team labels %s do not both map to fixture p1/p2 (%s/%s) — skipping.",
                        label, team_raw_names, fm.p1_norm, fm.p2_norm)
            continue
        cov.matched += 1
        fid = fm.fixture_id

        # Ensure a fixture envelope exists, then apply the defer gate: if OddsPapi already supplied an
        # ACTIVE polymarket book (more market types), defer to it; otherwise fill the slug.
        fx_raw = raw_by_fixture.get(fid)
        if fx_raw is None:
            info = by_fixture.get(fid, {})
            fx_raw = {"fixtureId": fid, "startTime": info.get("start_time"),
                      "statusId": info.get("status_id"), "hasOdds": True, "bookmakerOdds": {}}
            raw_by_fixture[fid] = fx_raw
        book_odds = fx_raw.setdefault("bookmakerOdds", {})
        if _oddspapi_has_active(book_odds.get("polymarket")):
            cov.deferred += 1
            log.info("[POLYMARKET] %s: OddsPapi already supplies an active polymarket book — deferring.", label)
            continue

        entry = {"bookmakerIsActive": True, "suspended": False, "markets": {}}
        _add_leg(entry, h2h["marketId"], h2h["home_oid"], home_leg.decimal, home_leg.limit, changed_at,
                 _venue(home_leg))
        _add_leg(entry, h2h["marketId"], h2h["draw_oid"], draw_leg.decimal, draw_leg.limit, changed_at,
                 _venue(draw_leg))
        _add_leg(entry, h2h["marketId"], h2h["away_oid"], away_leg.decimal, away_leg.limit, changed_at,
                 _venue(away_leg))

        # SAFE-TIER: full-game total-goals O/U + BTTS (reg-time, same basis as the 1x2) onto this fixture.
        nt = 0
        for line, (over_leg, under_leg) in ev.totals.items():
            tidx = market_index.totals.get(line)
            if not tidx:
                continue
            _add_leg(entry, tidx["marketId"], tidx["over_oid"], over_leg.decimal, over_leg.limit, changed_at,
                     _venue(over_leg))
            _add_leg(entry, tidx["marketId"], tidx["under_oid"], under_leg.decimal, under_leg.limit, changed_at,
                     _venue(under_leg))
            nt += 1
        if nt:
            cov.totals_fixtures += 1
            cov.totals_lines += nt
        nb = 0
        if ev.btts and market_index.btts:
            yes_leg, no_leg = ev.btts
            b = market_index.btts
            _add_leg(entry, b["marketId"], b["yes_oid"], yes_leg.decimal, yes_leg.limit, changed_at,
                     _venue(yes_leg))
            _add_leg(entry, b["marketId"], b["no_oid"], no_leg.decimal, no_leg.limit, changed_at,
                     _venue(no_leg))
            cov.btts_fixtures += 1
            nb = 1

        # SAFE-TIER spreads (reg-time): map each Poly handicap line-for-line to the canonical
        # marketId (keyed by the line on p1/home). Sign is resolved from the fixture's p1/p2 IDENTITY
        # (never a Poly tag): named==p1 -> p1_pt = line; named==p2 -> p1_pt = -line. Only no-push
        # half-lines, only this fixture's exact two teams, only per_game-scoped canonical markets.
        ns = _inject_spreads(entry, ev.spreads, fm, market_index, changed_at)
        if ns:
            cov.spreads_fixtures += 1
            cov.spreads_lines += ns

        book_odds["polymarket"] = entry      # fill the (missing/stale) OddsPapi polymarket slug
        poly_books.setdefault(fid, set()).add("polymarket")
        cov.recovered += 1

        info = by_fixture.get(fid, {})
        log.info("[POLYMARKET] %s -> %s vs %s | home %.3f ($%.0f) / draw %.3f ($%.0f) / away %.3f ($%.0f)"
                 " | +%d total line(s)%s + %d spread line(s)",
                 label, info.get("p1"), info.get("p2"),
                 home_leg.decimal, home_leg.limit, draw_leg.decimal, draw_leg.limit,
                 away_leg.decimal, away_leg.limit, nt, " + BTTS" if nb else "", ns)

    return cov, poly_books
