"""the-odds-api adapter for the OG multi-sport scanner — self-learning + 422-proof (spec section 1).

Generalises the soccer btts lesson (never hardcode market support). For each sport it:
  * resolves the the-odds-api sport-key(s): MLB/UFC are static (``baseball_mlb`` /
    ``mma_mixed_martial_arts``); tennis is AUTO — discovered from ``/v4/sports`` (group==Tennis &&
    active) once per scan-day, cached to ``data/og_multi/toa_tennis_keys.json`` (logging whether the
    call billed, via the x-requests-* header delta);
  * fetches the desired BULK markets (mlb [h2h,spreads,totals], ufc [h2h,totals], tennis
    [h2h,totals,spreads]) in ONE call. On a 422 INVALID_MARKET it parses the offending market key,
    drops it, and RETRIES the reduced set IN THE SAME CYCLE (a 422 never costs a cycle its data),
    persisting the learned valid set per sport-key to ``data/og_multi/toa_capabilities.json`` (loaded
    on startup; a dropped market is re-probed at most once a week). ``h2h`` is never dropped — it is
    the winner market Tier A always needs and does not 422 for these sports;
  * accounts credits per cycle from the response headers + a running UTC-day total, and WARNs when the
    projected month exceeds ``quota_warn_pct`` of the plan quota (remaining + used, from the headers).

Books are returned raw (every book present in the response). Which books may turn an arb ACTIONABLE
(pinnacle + 1xbet) vs stay shadow-only (the soft books) is decided downstream in tiers.py — this
module just reports ``ACTIONABLE_BOOKS`` (canonical slugs) for that gate. region is eu only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..theoddsapi import TheOddsApiClient, TheOddsApiError, canonical_book_slug
from . import state

# Desired bulk markets per sport (ordered; h2h first — the winner spine). Learned-down on 422.
DESIRED_MARKETS: dict[str, list[str]] = {
    "mlb": ["h2h", "spreads", "totals"],
    "ufc": ["h2h", "totals"],
    "tennis": ["h2h", "totals", "spreads"],
}
NEVER_DROP = frozenset({"h2h"})          # the winner market — Tier A always needs it; it never 422s
REPROBE_DAYS = 7                          # re-probe a 422-dropped market at most once a week

# the-odds-api book KEYS whose recovered legs may turn an arb ACTIONABLE (canonical post-alias slugs).
# 1xbet is the toa book we hold an account on; pinnacle is sourced here in the free profile. Every
# other book in the response (unibet, bet365, …) is recorded SHADOW-only, exactly like the soccer
# shadow doctrine. Enforced in tiers.py; toa.py returns all books untouched.
ACTIONABLE_BOOKS = frozenset({"pinnacle", "1xbet"})


@dataclass
class ToaFetch:
    """One sport's the-odds-api pull for a cycle."""
    sport: str
    events: list = field(default_factory=list)        # raw the-odds-api event dicts (all books)
    markets_served: list = field(default_factory=list)  # markets the plan actually served (panel footer)
    sport_keys: list = field(default_factory=list)      # sport-keys queried (tennis may be several)
    credits: int = 0                                    # credits spent this sport this cycle
    daily_total: int = 0                                # running UTC-day credit total after this sport


def _used(client: TheOddsApiClient) -> int:
    try:
        return int(client.requests_used) if client.requests_used is not None else 0
    except (TypeError, ValueError):
        return 0


def _remaining(client: TheOddsApiClient) -> int:
    try:
        return int(client.requests_remaining) if client.requests_remaining is not None else 0
    except (TypeError, ValueError):
        return 0


def _last(client: TheOddsApiClient) -> Optional[int]:
    """The credit cost of the client's LAST request, from the-odds-api's x-requests-last header (0 on
    the free /v4/sports endpoint). None when the client never called / the header was absent."""
    try:
        v = getattr(client, "requests_last", None)
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _call_credits(client: TheOddsApiClient, used_before: int) -> int:
    """Credits the most recent call cost. Prefers x-requests-last (the exact per-request cost); only
    when that header is absent does it fall back to the cumulative-used delta — and even then just
    when a REAL baseline existed (used_before > 0), so a fresh client (whose requests_used is still
    None -> 0 before its first response) never mis-bills the whole account lifetime to one cycle."""
    last = _last(client)
    if last is not None:
        return max(0, last)
    return max(0, _used(client) - used_before) if used_before > 0 else 0


def _now_utc(now: datetime) -> datetime:
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def _day(now: datetime) -> str:
    return _now_utc(now).strftime("%Y-%m-%d")


def _parse_iso(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Sport-key resolution                                                          #
# --------------------------------------------------------------------------- #
def discover_tennis_keys(client: TheOddsApiClient, now: datetime, log, *,
                         base: str = state.OG_MULTI_DIR) -> tuple[list[str], int]:
    """AUTO tennis keys: the live ATP/WTA tournament sport-keys (group==Tennis && active), discovered
    from /v4/sports once per scan-day and cached. Returns (keys, credits_billed_for_discovery)."""
    cache = state.load_tennis_keys(base)
    if state.tennis_keys_fresh(cache, now):
        return list(cache.get("keys") or []), 0
    used_before = _used(client)
    try:
        sports = client.sports()
    except TheOddsApiError as exc:
        log.warning("[TOA] tennis /v4/sports discovery failed: %s", exc)
        return list(cache.get("keys") or []), 0        # fall back to the last-known keys, if any
    billed = _call_credits(client, used_before)         # /v4/sports is free -> x-requests-last is 0
    keys = [str(s.get("key")) for s in sports
            if isinstance(s, dict) and str(s.get("group")) == "Tennis" and s.get("active")
            and s.get("key")]
    state.save_tennis_keys(_day(now), keys, billed=bool(billed), base=base)
    log.info("[TOA] tennis sport-keys discovered: %d active (%s) - /v4/sports billed %d credit(s)",
             len(keys), ", ".join(keys) if keys else "none", billed)
    return keys, billed


def resolve_sport_keys(sport: str, toa_sport_key: str, client: TheOddsApiClient, now: datetime, log, *,
                       base: str = state.OG_MULTI_DIR) -> tuple[list[str], int]:
    """Resolve the the-odds-api sport-key(s) for ``sport``. Static for mlb/ufc; AUTO discovery for
    tennis. Returns (keys, discovery_credits)."""
    if str(toa_sport_key).upper() == "AUTO":
        return discover_tennis_keys(client, now, log, base=base)
    return ([toa_sport_key], 0) if toa_sport_key else ([], 0)


# --------------------------------------------------------------------------- #
# Capability learning                                                           #
# --------------------------------------------------------------------------- #
def desired_markets(sport: str, sport_key: str, caps: dict[str, Any], now: datetime) -> list[str]:
    """The bulk markets to request for ``sport_key``: the sport's base set minus any market dropped
    within the last week (h2h is never dropped). Order preserved (h2h first)."""
    base = DESIRED_MARKETS.get(sport, ["h2h"])
    dropped = (caps.get(sport_key) or {}).get("dropped") or {}
    out: list[str] = []
    now = _now_utc(now)
    for m in base:
        if m in NEVER_DROP:
            out.append(m)
            continue
        d = _parse_iso(dropped.get(m))
        if d is None or (now - d).days >= REPROBE_DAYS:
            out.append(m)                               # never dropped, or the weekly re-probe is due
    return out or [base[0]]                              # never send an empty market list


def _parse_invalid_market(exc: TheOddsApiError, requested: list[str]) -> list[str]:
    """On a 422 INVALID_MARKET, the markets whose key the error names. Falls back to the last
    non-h2h requested market when the body names none explicitly (defensive — still 422-proof)."""
    if getattr(exc, "status_code", None) != 422:
        return []
    body = (getattr(exc, "body", None) or str(exc) or "").lower()
    if "invalid_market" not in body and "invalid market" not in body:
        return []
    named = [m for m in requested if m != "h2h" and m in body]
    if named:
        return named
    tail = [m for m in requested if m != "h2h"]
    return [tail[-1]] if tail else []                    # drop the last droppable market as a fallback


def fetch_events(sport: str, sport_key: str, client: TheOddsApiClient, region: str,
                 caps: dict[str, Any], now: datetime, log) -> tuple[list, list[str], int]:
    """Fetch one sport-key's bulk odds with 422 drop-retry-learn. Returns (events, markets_served,
    credits). Mutates ``caps`` in place (valid set + drop timestamps); the caller persists it."""
    attempt = desired_markets(sport, sport_key, caps, now)
    credits = 0
    while attempt:
        used_before = _used(client)
        try:
            events = client.odds(sport_key, region, ",".join(attempt))
        except TheOddsApiError as exc:
            credits += _call_credits(client, used_before)
            bad = _parse_invalid_market(exc, attempt)
            if bad:
                entry = caps.setdefault(sport_key, {})
                dropped = entry.setdefault("dropped", {})
                stamp = state.iso_utc(now)
                for m in bad:
                    if m in attempt:
                        attempt.remove(m)
                    dropped[m] = stamp
                log.warning("[TOA] %s/%s: INVALID_MARKET %s dropped (learned); retrying %s",
                            sport, sport_key, bad, attempt or "[]")
                continue                                 # retry the reduced set IN THE SAME CYCLE
            log.warning("[TOA] %s/%s odds fetch failed (status %s): %s",
                        sport, sport_key, getattr(exc, "status_code", None), exc)
            return [], [], credits
        credits += _call_credits(client, used_before)
        entry = caps.setdefault(sport_key, {})
        entry["valid"] = list(attempt)                   # remember what actually worked
        return (events if isinstance(events, list) else []), list(attempt), credits
    return [], [], credits


# --------------------------------------------------------------------------- #
# Public entry — one sport, one cycle                                           #
# --------------------------------------------------------------------------- #
def run_toa(sport: str, *, client: TheOddsApiClient, region: str, toa_sport_key: str,
            now: datetime, log, warn_pct: float = 60.0,
            base: str = state.OG_MULTI_DIR) -> ToaFetch:
    """Pull ``sport``'s the-odds-api odds for one cycle: resolve key(s), fetch with capability
    learning, account credits, and WARN on projected-month quota. Never raises — a feed failure
    yields an empty ToaFetch so the cycle continues."""
    caps = state.load_capabilities(base)
    keys, credits = resolve_sport_keys(sport, toa_sport_key, client, now, log, base=base)
    events: list = []
    served: set[str] = set()
    for key in keys:
        ev, markets, c = fetch_events(sport, key, client, region, caps, now, log)
        events.extend(ev)
        served.update(markets)
        credits += c
    state.save_capabilities(caps, base)

    daily_total = state.record_credits(credits, now, base)
    remaining, used = _remaining(client), _used(client)
    quota = used + remaining
    served_list = [m for m in DESIRED_MARKETS.get(sport, []) if m in served]  # ordered, deduped
    log.info("[TOA] %s: %d event(s) over key(s) %s | markets served %s | %d credit(s) this cycle, "
             "%d today, %d remaining", sport, len(events), keys or "none",
             served_list or "none", credits, daily_total, remaining)
    if quota > 0:
        projected_month = daily_total * 30
        if projected_month > (warn_pct / 100.0) * quota:
            log.warning("[TOA] QUOTA WATCH: projected month ~%d req > %.0f%% of plan quota %d "
                        "(used %d, remaining %d, today %d)", projected_month, warn_pct, quota,
                        used, remaining, daily_total)
    return ToaFetch(sport=sport, events=events, markets_served=served_list, sport_keys=list(keys),
                    credits=credits, daily_total=daily_total)
