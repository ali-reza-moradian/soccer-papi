"""One-off discovery probe for a FUTURE Polymarket-direct source (mirror of scripts/probe_kalshi.py).

NO auth, NO trading calls — read-only public market data only. Like the Kalshi probe this DOES NOT
build the source; it prints everything a later mapping step (the equivalent of B2 for Kalshi) needs to
key Polymarket prices off our canonical OddsPapi fixtureId / marketId / outcomeId.

Usage:
    python -m scripts.probe_polymarket                 # default in-window team search terms
    python -m scripts.probe_polymarket "Spain" "Cape Verde" "Ecuador"

Two PUBLIC, unauthenticated APIs (no key, no headers beyond a polite User-Agent):
  * Gamma (https://gamma-api.polymarket.com) — market/event DISCOVERY. /public-search?q= for title/
    slug/team search, /events and /events/{id} for the event→markets grouping, /markets/{id} detail.
  * CLOB  (https://clob.polymarket.com) — PRICES/DEPTH per outcome token. /price?token_id=&side=,
    /midpoint?token_id=, /book?token_id= (full resting order book). Prices are DOLLARS in (0,1) =
    implied probability, so the decimal odds to BACK an outcome are 1 / ask (same shape as Kalshi).

The client throttles to >= 0.5s between requests and retries 429/5xx with exponential backoff, exactly
like src/kalshi.KalshiClient — Polymarket's public read limits are generous but we stay polite.

Steps:
  1. Gamma discovery — /public-search for "World Cup" + each in-window team term; collect candidate
     events and print how they're GROUPED (one event vs many markets) and what identifies a single
     match (title / slug / startDate / tags / series / negRisk / per-market questions).
  2. For up to 3 in-window match events, pull full event detail and print each market + every outcome
     with its CLOB token_id and human label (team / "Draw"), then print a VERDICT: is a match ONE
     market with 3 outcomes (home/draw/away) or SEPARATE binary Yes/No markets (negRisk grouping)?
  3. For each outcome token, GET CLOB /price (buy=ask), /midpoint, and /book — print best bid/ask +
     size (real depth), the implied prob, and the decimal odds 1/ask so the prices are legible.
  4. Dump the raw JSON of one event, one market, and one orderbook so the mapping step can pin exact
     field names + units. Then STOP — paste the output; nothing here touches arbitrage.py or sources.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Teams in our current in-window WC fixtures — used as title/slug search terms. Override via argv.
DEFAULT_TERMS = ["World Cup", "Ivory Coast", "Ecuador", "Spain", "Cape Verde"]

MAX_DRILL_EVENTS = 3      # how many matched events to fully drill (markets + prices + books)
MAX_BOOK_TOKENS = 12      # global cap on CLOB /price+/midpoint+/book calls (politeness)
USER_AGENT = "soccer-papi-probe/1.0 (read-only WC discovery)"


# --------------------------------------------------------------------------- #
# HTTP client (public Gamma + CLOB read endpoints; NO auth)                     #
# --------------------------------------------------------------------------- #
class PolyError(Exception):
    """Any failure talking to Polymarket. Self-contained to this probe (no src/ dependency)."""


class PolyRateLimited(PolyError):
    """Transient HTTP 429 / 5xx — retried with exponential backoff."""


class PolyClient:
    """Thin client over Polymarket's public Gamma + CLOB read endpoints. Unauthenticated GETs only:
    discovery via Gamma, prices/depth via CLOB. Calls are (a) throttled >= min_interval apart and
    (b) retried on 429/5xx with exponential backoff — mirrors src/kalshi.KalshiClient exactly."""

    def __init__(self, *, timeout: float = 30.0, session: requests.Session | None = None,
                 min_interval: float = 0.5) -> None:
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.min_interval = min_interval
        self._last_request_ts = 0.0

    # -- Gamma (discovery) ---------------------------------------------------
    def gamma_search(self, q: str) -> Any:
        """GET /public-search?q= — fuzzy search over events/tags/profiles by title/slug/team name."""
        return self._get(GAMMA_BASE + "/public-search", {"q": q})

    def events(self, **params: Any) -> Any:
        """GET /events — list events (each with nested markets). Filter via closed/active/limit/…"""
        return self._get(GAMMA_BASE + "/events", params)

    def event(self, event_id: Any) -> Any:
        """GET /events/{id} — one event with its FULL nested markets (incl. clobTokenIds)."""
        return self._get(GAMMA_BASE + f"/events/{event_id}", {})

    def market(self, market_id: Any) -> Any:
        """GET /markets/{id} — one market's full Gamma object."""
        return self._get(GAMMA_BASE + f"/markets/{market_id}", {})

    # -- CLOB (prices + depth) ----------------------------------------------
    def clob_price(self, token_id: str, side: str) -> Any:
        """GET /price?token_id=&side=buy|sell — best ask (buy) / best bid (sell), dollars in (0,1)."""
        return self._get(CLOB_BASE + "/price", {"token_id": token_id, "side": side})

    def clob_midpoint(self, token_id: str) -> Any:
        """GET /midpoint?token_id= — midpoint of best bid/ask, dollars in (0,1)."""
        return self._get(CLOB_BASE + "/midpoint", {"token_id": token_id})

    def clob_book(self, token_id: str) -> Any:
        """GET /book?token_id= — full resting order book (bids/asks each {price,size})."""
        return self._get(CLOB_BASE + "/book", {"token_id": token_id})

    # -- transport (throttle + retry/backoff, mirrors KalshiClient) ----------
    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        import time
        wait = self.min_interval - (time.monotonic() - self._last_request_ts)
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.monotonic()

    @retry(retry=retry_if_exception_type(PolyRateLimited),
           wait=wait_exponential(multiplier=1, min=1, max=30),
           stop=stop_after_attempt(5), reraise=True)
    def _get(self, url: str, params: dict[str, Any]) -> Any:
        self._throttle()
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise PolyError(f"network error on {url}: {exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise PolyRateLimited(f"{resp.status_code} on {url}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise PolyError(f"{resp.status_code} on {url}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise PolyError(f"non-JSON response on {url}: {resp.text[:200]}") from exc


# --------------------------------------------------------------------------- #
# Parsing helpers — Gamma encodes parallel arrays as JSON STRINGS               #
# --------------------------------------------------------------------------- #
def _as_list(v: Any) -> list[Any]:
    """Gamma returns `outcomes` / `outcomePrices` / `clobTokenIds` as JSON-encoded STRINGS
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


def _market_outcomes(m: dict) -> list[tuple[str, Optional[str], Optional[str]]]:
    """(label, token_id, gamma_price) per outcome, pairing the parallel outcomes/clobTokenIds/
    outcomePrices arrays by index. token_id None if the market isn't deployed to the CLOB yet."""
    labels = _as_list(m.get("outcomes"))
    tokens = _as_list(m.get("clobTokenIds"))
    prices = _as_list(m.get("outcomePrices"))
    n = max(len(labels), len(tokens))
    out: list[tuple[str, Optional[str], Optional[str]]] = []
    for i in range(n):
        label = str(labels[i]) if i < len(labels) else f"outcome[{i}]"
        token = str(tokens[i]) if i < len(tokens) and tokens[i] else None
        price = str(prices[i]) if i < len(prices) else None
        out.append((label, token, price))
    return out


def _tag_texts(ev: dict) -> list[str]:
    return [str(t.get("label") or t.get("slug") or "") for t in (ev.get("tags") or []) if isinstance(t, dict)]


def _looks_like_wc(ev: dict) -> bool:
    """A World Cup match event mentions the WC in its title/slug/tags/series."""
    text = " ".join([str(ev.get("title") or ""), str(ev.get("slug") or ""),
                     str((ev.get("series") or [{}])[0].get("slug", "") if isinstance(ev.get("series"), list) else ""),
                     *_tag_texts(ev)]).lower()
    return "world cup" in text or "fifa" in text


def _looks_like_match(ev: dict) -> bool:
    """A single match (not a futures/group event): title reads 'A vs B' / 'A-vs-B'."""
    t = (str(ev.get("title") or "") + " " + str(ev.get("slug") or "")).lower()
    return " vs" in t or "-vs-" in t or " v " in t


def _num(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _best_level(levels: Any, *, side: str) -> Optional[tuple[float, float]]:
    """(price, size) at the best level of a CLOB book side. Best bid = highest price you can sell
    into; best ask = lowest price you can buy at. Computed explicitly (don't trust array order)."""
    rows: list[tuple[float, float]] = []
    for lvl in levels or []:
        p, s = _num((lvl or {}).get("price")), _num((lvl or {}).get("size"))
        if p is not None and s is not None:
            rows.append((p, s))
    if not rows:
        return None
    return max(rows, key=lambda r: r[0]) if side == "bid" else min(rows, key=lambda r: r[0])


def _pp(label: str, obj: Any) -> None:
    print(f"\n----- {label} (raw JSON) -----")
    print(json.dumps(obj, ensure_ascii=False, indent=2)[:4000])


# --------------------------------------------------------------------------- #
# Discovery                                                                     #
# --------------------------------------------------------------------------- #
def _collect_search_events(client: PolyClient, terms: list[str]) -> dict[str, dict]:
    """Run /public-search for each term; return {event_id: event} de-duplicated across terms."""
    found: dict[str, dict] = {}
    for term in terms:
        print(f"\n=== GET /public-search?q={term!r} ===")
        try:
            data = client.gamma_search(term)
        except PolyError as exc:
            print(f"  ERROR: {exc}")
            continue
        evs = (data or {}).get("events") if isinstance(data, dict) else None
        evs = evs if isinstance(evs, list) else (data if isinstance(data, list) else [])
        tags = (data or {}).get("tags") if isinstance(data, dict) else None
        if isinstance(tags, list) and tags:
            print(f"  tags: {', '.join(str(t.get('label') or t.get('slug')) for t in tags[:8] if isinstance(t, dict))}")
        print(f"  {len(evs)} event(s) returned")
        for ev in evs:
            if not isinstance(ev, dict):
                continue
            eid = str(ev.get("id") or ev.get("slug") or "")
            if eid:
                found.setdefault(eid, ev)
    return found


def main(argv: list[str]) -> int:
    terms = argv or DEFAULT_TERMS
    client = PolyClient()
    print(f"Polymarket discovery probe — search terms: {terms}")
    print(f"Gamma={GAMMA_BASE}  CLOB={CLOB_BASE}  (read-only, throttled >=0.5s, retry 429/5xx)")

    # 1) Discovery: search, then describe grouping / single-match identity ---------------------
    candidates = _collect_search_events(client, terms)
    print(f"\n=== {len(candidates)} unique candidate event(s) across all terms ===")
    for eid, ev in candidates.items():
        wc = "WC" if _looks_like_wc(ev) else "  "
        mtch = "MATCH" if _looks_like_match(ev) else "     "
        mkts = ev.get("markets") if isinstance(ev.get("markets"), list) else []
        print(f"\n  [{wc} {mtch}] id={eid} closed={ev.get('closed')} start={ev.get('startDate')}")
        print(f"           title={ev.get('title')!r}")
        print(f"           slug={ev.get('slug')!r}  negRisk={ev.get('negRisk')}  #markets={len(mkts)}")
        tags = _tag_texts(ev)
        if tags:
            print(f"           tags={tags}")
        for m in mkts[:6]:
            if isinstance(m, dict):
                print(f"             - market {m.get('id')}: {str(m.get('question'))!r} "
                      f"groupItemTitle={m.get('groupItemTitle')!r}")

    # Pick in-window WC match events to drill: WC-tagged, not closed, looks like a single match,
    # soonest first. Fall back to any not-closed match-shaped event if none are WC-tagged.
    def _start(ev: dict) -> str:
        return str(ev.get("startDate") or "")
    wc_matches = [ev for ev in candidates.values()
                  if not ev.get("closed") and _looks_like_wc(ev) and _looks_like_match(ev)]
    if not wc_matches:
        wc_matches = [ev for ev in candidates.values() if not ev.get("closed") and _looks_like_match(ev)]
    drill = sorted(wc_matches, key=_start)[:MAX_DRILL_EVENTS]

    print(f"\n=== drilling {len(drill)} event(s) (full markets + CLOB prices/depth) ===")
    if not drill:
        print("  no in-window WC match events matched the search — adjust the team terms (argv) and re-run.")
        return 0

    # 2) + 3) Per event: full detail, every outcome's token + label, then CLOB price/midpoint/book
    book_calls = 0
    raw_event = raw_market = raw_book = None
    for ev in drill:
        eid = str(ev.get("id") or ev.get("slug"))
        print(f"\n────────────────────────────────────────────────────────────────────")
        print(f"EVENT id={eid}  {ev.get('title')!r}  start={ev.get('startDate')}")
        try:
            full = client.event(eid)
        except PolyError as exc:
            print(f"  /events/{eid} ERROR: {exc}")
            continue
        # /events/{id} may return the object directly or wrapped in a list.
        full = full[0] if isinstance(full, list) and full else full
        if not isinstance(full, dict):
            print(f"  unexpected /events/{eid} shape: {type(full)}")
            continue
        if raw_event is None:
            raw_event = full
        mkts = [m for m in (full.get("markets") or []) if isinstance(m, dict)]

        # Grouping VERDICT — the core question for mapping to our 1x2 home/draw/away outcomeIds.
        outcome_counts = [len(_as_list(m.get("outcomes"))) for m in mkts]
        if len(mkts) == 1 and outcome_counts and outcome_counts[0] >= 3:
            verdict = f"SINGLE market, {outcome_counts[0]} outcomes in one book (home/draw/away together)"
        elif len(mkts) >= 2 and outcome_counts and all(c == 2 for c in outcome_counts):
            verdict = f"{len(mkts)} SEPARATE binary Yes/No markets (negRisk grouping — one per outcome)"
        elif len(mkts) == 1 and outcome_counts and outcome_counts[0] == 2:
            verdict = "SINGLE binary market (2-way — no draw leg?)"
        else:
            verdict = f"mixed/other ({len(mkts)} markets, outcome counts {outcome_counts}) — inspect dump"
        print(f"  GROUPING: {verdict}   (negRisk={full.get('negRisk')})")

        for m in mkts:
            print(f"\n  market id={m.get('id')} conditionId={m.get('conditionId')}")
            print(f"    question={str(m.get('question'))!r}  groupItemTitle={m.get('groupItemTitle')!r}  "
                  f"closed={m.get('closed')} active={m.get('active')}")
            if raw_market is None:
                raw_market = m
            for label, token, gprice in _market_outcomes(m):
                print(f"    • outcome={label!r}  gamma_price={gprice}  token_id={token}")
                if token is None:
                    print("        (no CLOB token — market not deployed to the order book yet)")
                    continue
                if book_calls >= MAX_BOOK_TOKENS:
                    print(f"        (skipped CLOB calls — hit MAX_BOOK_TOKENS={MAX_BOOK_TOKENS})")
                    continue
                book_calls += 1
                # /price (ask), /midpoint, /book — read-only.
                try:
                    ask = client.clob_price(token, "buy")
                    mid = client.clob_midpoint(token)
                    book = client.clob_book(token)
                except PolyError as exc:
                    print(f"        CLOB ERROR: {exc}")
                    continue
                if raw_book is None:
                    raw_book = book
                ask_px = (ask or {}).get("price")
                mid_px = (mid or {}).get("mid")
                bb = _best_level((book or {}).get("bids"), side="bid")
                ba = _best_level((book or {}).get("asks"), side="ask")
                ask_dec = _num(ask_px)
                dec = (1.0 / ask_dec) if (ask_dec and 0.0 < ask_dec < 1.0) else None
                print(f"        price(ask)={ask_px}  midpoint={mid_px}  "
                      f"decimal_odds(1/ask)={dec:.3f}" if dec else
                      f"        price(ask)={ask_px}  midpoint={mid_px}  decimal_odds(1/ask)=n/a")
                if bb:
                    print(f"        book best BID: price={bb[0]} size={bb[1]}  (~${bb[0]*bb[1]:.0f} depth)")
                else:
                    print("        book best BID: (none)")
                if ba:
                    print(f"        book best ASK: price={ba[0]} size={ba[1]}  (~${ba[0]*ba[1]:.0f} depth)")
                else:
                    print("        book best ASK: (none)")

    # 4) Raw shapes so the mapping step can pin exact field names + units ----------------------
    if raw_event is not None:
        _pp("one EVENT object (/events/{id})", raw_event)
    if raw_market is not None:
        _pp("one MARKET object (note: outcomes/clobTokenIds/outcomePrices are JSON STRINGS)", raw_market)
    if raw_book is not None:
        _pp("one CLOB ORDER BOOK (/book?token_id=) — bids/asks each {price,size} in dollars/shares", raw_book)

    print("\n=== DONE — paste everything above (esp. the GROUPING verdict, token→label mapping, "
          "and price/book units). No source built; arbitrage.py untouched. ===")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
