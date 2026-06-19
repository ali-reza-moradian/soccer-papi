"""Entry point for one scan cycle: budget guard -> odds -> arbs -> CSV -> Telegram.

Run with:  python -m src.run
Designed to be frugal: ~1 billable request per cycle (plus an occasional names refresh).
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from . import (catalog, excel_log, formatting as fmt, group_markets, kalshi, normalize, player_props,
               polymarket, scoreboard, theoddsapi)
from .arbitrage import (ArbResult, Candidate, cap_total_investment, compute_arb, make_arb_id,
                        make_signature, select_legs)
from .config import Config, load_config
from .csv_store import append_opportunities
from .logsetup import setup_logging
from .normalize import FixtureFeed, RawCandidate, exchange_liquidity
from .oddspapi import OddsPapiClient, OddsPapiError, QuotaExceeded, check_budget, log_key_exhausted
from .telegram import build_message, send_message


# --------------------------------------------------------------------------- #
# Small helpers                                                                 #
# --------------------------------------------------------------------------- #
def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _leg_age_minutes(changed_at: Optional[str], now: datetime) -> Optional[float]:
    dt = _parse_iso(changed_at)
    if dt is None:
        return None
    return (now - dt).total_seconds() / 60.0


@dataclass
class EngineCtx:
    actionable: set[str]
    tracked: set[str]
    exchanges: set[str]
    commission: dict[str, float]
    clone_group_of: Any
    # UNKNOWN-LIMIT REALISM: a leg with no real reported limit is capped at this assumed executable
    # size in the T_max/binding calc (per-book override map first), not treated as bottomless.
    assumed_unknown_limit: float = 1000.0
    assumed_unknown_limit_by_book: dict[str, float] = field(default_factory=dict)
    # Hard cap on total money staked across all legs of one arb (0 => no cap). Applied AFTER sizing.
    bankroll_total: float = 0.0
    # Time-to-kickoff-aware staleness (replaces a single flat max age): soft books hold steady
    # pre-match lines for hours, so the max allowed leg age scales with how far off kickoff is.
    # See max_leg_age_for(). All ages are minutes; horizons are hours.
    max_leg_age_far: float = 360.0           # kickoff > stale_far_horizon_hours away
    max_leg_age_mid: float = 60.0            # between the near and far horizons
    max_leg_age_near: float = 20.0           # within stale_near_horizon_hours of kickoff
    stale_far_horizon_hours: float = 6.0
    stale_near_horizon_hours: float = 1.0
    low_confidence_limit_floor: float = 10.0
    # Outcome-mapping guard: trusted reference books (first present wins), the heavy-favourite
    # gate, and an optional book whose raw per-outcome odds we dump for debugging. Empty
    # reference_books => guard disabled (no-op).
    reference_books: list[str] = field(default_factory=list)
    min_favorite_ratio: float = 1.5
    dump_book: str = ""

    def max_leg_age_for(self, kickoff: Optional[datetime], now: datetime) -> float:
        """Max allowed price age (minutes) for legs on a fixture kicking off at ``kickoff``.

        Soft books hold steady lines hours before kickoff, so we relax the staleness cut the
        further off kickoff is, and tighten it inside the final hour when lines move fast. With no
        kickoff known we fall back to the tightest (near) limit rather than trust an old price.
        """
        if kickoff is None:
            return self.max_leg_age_near
        hours_to_ko = (kickoff - now).total_seconds() / 3600.0
        if hours_to_ko > self.stale_far_horizon_hours:
            return self.max_leg_age_far
        if hours_to_ko > self.stale_near_horizon_hours:
            return self.max_leg_age_mid
        return self.max_leg_age_near


@dataclass
class Opportunity:
    fixture_id: str
    match: str
    home_team: str
    away_team: str
    tournament: str
    kickoff_utc: Optional[str]
    spec: catalog.MarketSpec
    res: ArbResult
    actionable: bool
    shadow_books: list[str]
    suspicious: bool
    bet_links: dict[str, str]
    signature: str
    # SCOPE_GROUP arbs only: days the stake is frozen until the group stage resolves (~10d). None for
    # per-game arbs. Surfaced in the alert + log because group markets lock capital for a long time.
    capital_lockup_days: Optional[int] = None

    @property
    def is_group_scope(self) -> bool:
        return self.spec.period == "group"

    @property
    def rank_profit(self) -> float:
        return self.res.max_profit

    @property
    def rank_roi(self) -> float:
        return self.res.roi_decimal


# --------------------------------------------------------------------------- #
# Candidate construction                                                        #
# --------------------------------------------------------------------------- #
def _to_candidates(
    raws: list[RawCandidate],
    spec: catalog.MarketSpec,
    universe: set[str],
    ctx: EngineCtx,
    now: datetime,
    max_leg_age: float,
    exclude_books: frozenset[str] = frozenset(),
) -> list[Candidate]:
    out: list[Candidate] = []
    for rc in raws:
        if rc.book not in universe:
            continue
        if rc.book in exclude_books:   # outcome-mapping-suspect for this market
            continue
        age = _leg_age_minutes(rc.changed_at, now)
        if age is not None and age > max_leg_age:   # kickoff-aware staleness limit
            continue
        is_exch = rc.book in ctx.exchanges or bool(rc.exchange_meta)
        limit = exchange_liquidity(rc.exchange_meta, rc.limit) if is_exch else rc.limit
        out.append(
            Candidate(
                outcome_id=rc.outcome_id,
                outcome_name=spec.outcome_names.get(rc.outcome_id, str(rc.outcome_id)),
                book=rc.book,
                clone_group=ctx.clone_group_of(rc.book),
                decimal_odds=rc.price,
                american_odds=rc.price_american,
                limit=limit,
                changed_at=rc.changed_at,
                main_line=rc.main_line,
                is_exchange=is_exch,
                commission=ctx.commission.get(rc.book, 0.0),
            )
        )
    return out


def _arb_for_universe(
    raw_market: dict[int, list[RawCandidate]],
    spec: catalog.MarketSpec,
    universe: set[str],
    ctx: EngineCtx,
    now: datetime,
    max_leg_age: float,
    exclude_books: frozenset[str] = frozenset(),
) -> Optional[ArbResult]:
    cands_by_outcome: dict[int, list[Candidate]] = {}
    for oid in spec.outcome_ids:
        cl = _to_candidates(raw_market.get(oid, []), spec, universe, ctx, now, max_leg_age, exclude_books)
        if not cl:
            return None  # market incomplete for this universe
        cands_by_outcome[oid] = cl
    chosen = select_legs(cands_by_outcome)
    if not chosen:
        return None
    res = compute_arb(chosen, ctx.assumed_unknown_limit, ctx.assumed_unknown_limit_by_book,
                      ctx.low_confidence_limit_floor)
    return cap_total_investment(res, ctx.bankroll_total)


# --------------------------------------------------------------------------- #
# Outcome-mapping guard                                                         #
# --------------------------------------------------------------------------- #
# Some books occasionally arrive with their two extreme outcomes (home/away, or the two sides
# of a 2-way market) swapped relative to the canonical outcomeId — an upstream data fault that
# would otherwise mint phantom arbs (e.g. a heavy favourite priced as the underdog). We never
# remap odds ourselves: the canonical outcomeId key is authoritative. Instead we sanity-check
# each book against a trusted reference on markets that HAVE a clear favourite, and drop any
# book whose favourite/underdog ranking is reversed (outcome_mapping_suspect) for that market.
def _book_prices_by_outcome(raw_market: dict[int, list[RawCandidate]]) -> dict[str, dict[int, float]]:
    """Per-book best decimal price for each outcomeId in one market: book -> {oid: price}."""
    out: dict[str, dict[int, float]] = {}
    for oid, raws in raw_market.items():
        for rc in raws:
            cur = out.setdefault(rc.book, {})
            if oid not in cur or rc.price > cur[oid]:
                cur[oid] = rc.price
    return out


def _detect_mapping_suspects(
    book_prices: dict[str, dict[int, float]],
    reference_books: list[str],
    min_favorite_ratio: float,
) -> tuple[frozenset[str], Optional[str], Optional[tuple[int, int]]]:
    """Flag books that rank the favourite vs the underdog OPPOSITELY to the consensus.

    Returns ``(suspect_books, reference_book, (fav_oid, dog_oid))``. The reference (first
    configured book present, pricing >=2 outcomes) picks which outcomes are the favourite
    (cheapest) and underdog (dearest) and gates on a clear gap. Every book that priced BOTH
    extremes then "votes" on the ordering; the minority orientation is suspect. The reference's
    own orientation only breaks an exact tie — so a (hypothetically) flipped reference standing
    against a clear majority is itself flagged, not the correct books.
    """
    if not reference_books:
        return frozenset(), None, None
    ref = next((b for b in reference_books if b in book_prices and len(book_prices[b]) >= 2), None)
    if ref is None:
        return frozenset(), None, None
    ref_p = book_prices[ref]
    fav_oid = min(ref_p, key=lambda o: ref_p[o])   # lowest odds  -> favourite
    dog_oid = max(ref_p, key=lambda o: ref_p[o])   # highest odds -> underdog
    if fav_oid == dog_oid or ref_p[dog_oid] < ref_p[fav_oid] * min_favorite_ratio:
        return frozenset(), ref, None              # no clear favourite -> ordering is noise

    agree, reverse = [], []                        # books pricing fav cheaper vs dearer than dog
    for book, p in book_prices.items():
        if fav_oid not in p or dog_oid not in p:
            continue
        if p[fav_oid] < p[dog_oid]:
            agree.append(book)
        elif p[fav_oid] > p[dog_oid]:
            reverse.append(book)
    # Minority orientation is suspect; ties defer to the trusted reference (always in `agree`).
    minority = reverse if len(agree) >= len(reverse) else agree
    return frozenset(minority), ref, (fav_oid, dog_oid)


def _outcome_team(spec: catalog.MarketSpec, oid: int, home: str, away: str) -> str:
    return fmt.outcome_label(spec.outcome_names.get(oid, str(oid)), home, away, spec.family, spec.line)


def _log_mapping_suspects(log, match, home, away, spec, book_prices, suspects, ref, extremes) -> None:
    fav_oid, dog_oid = extremes
    fav, dog = _outcome_team(spec, fav_oid, home, away), _outcome_team(spec, dog_oid, home, away)
    market = fmt.market_label(spec.label, spec.family, spec.line)
    rp = book_prices.get(ref, {})
    for book in sorted(suspects):
        bp = book_prices.get(book, {})
        log.warning("[MAPPING SUSPECT] %s | %s: %s looks OUTCOME-FLIPPED vs %s — skipping %s for this market.",
                    match, market, book, ref, book)
        log.warning("    %-10s %s @ %s | %s @ %s   (favourite cheaper — correct)",
                    ref + ":", fav, fmt.num2(rp.get(fav_oid)), dog, fmt.num2(rp.get(dog_oid)))
        log.warning("    %-10s %s @ %s | %s @ %s   (favourite DEARER — outcomes swapped)",
                    book + ":", fav, fmt.num2(bp.get(fav_oid)), dog, fmt.num2(bp.get(dog_oid)))


def _dump_book_outcomes(log, match, spec, home, away, book, prices) -> None:
    """Raw per-outcome odds for one book on one market (set DEBUG_DUMP_BOOK / mapping_guard.dump_book)."""
    market = fmt.market_label(spec.label, spec.family, spec.line)
    parts = [f"oid {oid} [{spec.outcome_names.get(oid, oid)}={_outcome_team(spec, oid, home, away)}] @ {fmt.num2(prices[oid])}"
             for oid in spec.outcome_ids if oid in prices]
    log.info("[DUMP %s] %s | %s: %s", book, match, market, "; ".join(parts) or "(no priced outcomes)")


# --------------------------------------------------------------------------- #
# Per-book coverage diagnostic (WHY is a book missing for a fixture?)            #
# --------------------------------------------------------------------------- #
def _book_coverage_status(
    fx: FixtureFeed,
    book: str,
    specs: dict[int, catalog.MarketSpec],
    allow_quarter_lines: bool,
    now: datetime,
    max_leg_age: float,
) -> str:
    """Exactly one coverage status for ``book`` on fixture ``fx`` — see _coverage_line.

      ABSENT     — slug not in this fixture's raw OddsPapi feed at all (missing upstream)
      SUSPENDED  — returned upstream but offered no active priced markets (suspended / inactive)
      STALE      — has in-scope priced markets, but EVERY one is older than the staleness limit
      OK:N       — contributes N in-scope markets that have at least one fresh (non-stale) leg
    """
    if book not in fx.books_in_feed:
        return "ABSENT"
    if book not in fx.books_present:          # in feed, but normalize dropped it (suspended/no markets)
        return "SUSPENDED"
    priced = 0   # in-scope scannable markets where this book has any priced leg
    fresh = 0    # ...of those, how many have at least one leg within the staleness limit
    for mid, raw_market in fx.markets.items():
        spec = specs.get(mid)
        if spec is None:                      # out-of-scope future / non-MECE / player-prop — not scanned
            continue
        if spec.has_quarter_line and not allow_quarter_lines:
            continue
        has_priced = has_fresh = False
        for raws in raw_market.values():
            for rc in raws:
                if rc.book != book:
                    continue
                has_priced = True
                age = _leg_age_minutes(rc.changed_at, now)
                if age is None or age <= max_leg_age:
                    has_fresh = True
                    break
            if has_fresh:
                break
        if has_priced:
            priced += 1
            fresh += 1 if has_fresh else 0
    if priced and not fresh:
        return "STALE"
    return f"OK:{fresh}"


def _coverage_line(
    fx: FixtureFeed,
    match: str,
    books: list[str],
    specs: dict[int, catalog.MarketSpec],
    allow_quarter_lines: bool,
    now: datetime,
    max_leg_age: float,
) -> str:
    """One COVERAGE line per in-window fixture: a status for every configured book, so it is
    obvious which of the books supplied odds and — when one didn't — exactly why (absent upstream
    vs suspended vs filtered out as stale). The trailing age-limit shows which kickoff-aware
    staleness bucket applied, which explains any STALE verdicts."""
    parts = " | ".join(
        f"{b} {_book_coverage_status(fx, b, specs, allow_quarter_lines, now, max_leg_age)}"
        for b in books
    )
    return f"COVERAGE {match}: {parts} | age-limit {max_leg_age:.0f}m"


# --------------------------------------------------------------------------- #
# Logging the full calculation                                                  #
# --------------------------------------------------------------------------- #
def _outcome(opp: Opportunity, name: str) -> str:
    return fmt.outcome_label(name, opp.home_team, opp.away_team, opp.spec.family, opp.spec.line)


def _log_arb_calc(log, opp: Opportunity, now: datetime) -> None:
    res = opp.res
    market = fmt.market_label(opp.spec.label, opp.spec.family, opp.spec.line)
    log.info("[ARB] %s | %s (%s, %s)", opp.match, market, opp.spec.family, opp.spec.period)
    for leg in res.legs:
        age = _leg_age_minutes(leg.changed_at, now)
        age_s = f"changed {age:.0f}m ago" if age is not None else "age n/a"
        # Whole-dollar money everywhere; a leg with no real limit shows UNVERIFIED (assumed cap used).
        lim_s = "limit UNVERIFIED" if leg.unverified else f"limit {fmt.money0(leg.limit)}"
        log.info("    %-14s: %s @ %-12s (%s, %s)",
                 _outcome(opp, leg.outcome_name), fmt.num2(leg.decimal_odds), leg.book, lim_s, age_s)
    terms = " + ".join(f"1/{leg.eff_odds:.3f}" for leg in res.legs)
    vals = " + ".join(f"{1.0/leg.eff_odds:.4f}" for leg in res.legs)
    verdict = "ARB (S<1)" if res.is_arb else "no arb (S>=1)"
    log.info("    S = %s = %s = %.4f  -> %s", terms, vals, res.arb_sum_S, verdict)
    log.info("    ROI = 1/S - 1 = %s%%", fmt.num2(res.roi_pct))
    tmax_terms = ", ".join(
        f"{fmt.money0(leg.effective_limit)}{'(assumed)' if leg.unverified else ''}*{leg.eff_odds:.3f}*{res.arb_sum_S:.4f}"
        for leg in res.legs
    )
    log.info("    T_max = min(%s) = %s  (binding: %s)", tmax_terms or "n/a", fmt.money0(res.t_max), res.binding_book)
    stakes = " | ".join(f"{_outcome(opp, leg.outcome_name)} {fmt.money0(leg.stake)} @ {leg.book}" for leg in res.legs)
    log.info("    Stakes @ T_max: %s", stakes)
    total_inv = sum(leg.stake for leg in res.legs)
    log.info("    Total Investment = %s", fmt.money0(total_inv))
    if res.unverified_books:
        log.info("    UNVERIFIED limit on: %s (sizes assumed, not from the book)",
                 ", ".join(res.unverified_books))
    log.info("    Guaranteed profit @ T_max = %s (%s%%)  [actionable=%s, suspicious=%s, low_conf=%s]",
             fmt.money0(res.max_profit), fmt.num2(res.roi_pct), opp.actionable, opp.suspicious, res.low_confidence)


# --------------------------------------------------------------------------- #
# Record building (CSV + Telegram share these dicts)                            #
# --------------------------------------------------------------------------- #
def _stake_per_book(legs) -> dict[str, float]:
    """Total stake to place at each book — summed, since one account can back >1 leg."""
    out: dict[str, float] = {}
    for leg in legs:
        out[leg.book] = round(out.get(leg.book, 0.0) + leg.stake, 2)
    return out


def _legs_payload(opp: Opportunity) -> list[dict[str, Any]]:
    return [
        {
            "outcome": _outcome(opp, leg.outcome_name),
            "book": leg.book,
            "decimal_odds": round(leg.decimal_odds, 2),
            "american_odds": leg.american_odds,
            "limit": None if leg.limit is None else round(leg.limit, 2),
            "unverified": leg.unverified,
            "effective_limit": round(leg.effective_limit, 2),
            "stake": round(leg.stake, 2),
            "changed_at": fmt.iso_local(leg.changed_at),
        }
        for leg in opp.res.legs
    ]


def _csv_row(opp: Opportunity, now: datetime) -> dict[str, Any]:
    res = opp.res
    legs = _legs_payload(opp)
    return {
        "detected_at_et": fmt.iso_local(now),
        "signature": opp.signature,
        "actionable": opp.actionable,
        "bookmakers": ", ".join(leg.book for leg in res.legs),
        "market": fmt.market_label(opp.spec.label, opp.spec.family, opp.spec.line),
        "event_date": fmt.date_local(opp.kickoff_utc),
        "roi_pct": round(res.roi_pct, 2),
        "max_liquidity": round(res.t_max, 2),
        "match": opp.match,
        "fixture_id": opp.fixture_id,
        "tournament": opp.tournament,
        "kickoff_et": fmt.iso_local(opp.kickoff_utc),
        "market_id": opp.spec.market_id,
        "market_type": opp.spec.family,
        "period": opp.spec.period,
        "line": "" if opp.spec.line is None else f"{opp.spec.line:g}",
        "legs_json": legs,
        "arb_sum_S": round(res.arb_sum_S, 6),
        "roi_decimal": round(res.roi_decimal, 6),
        "total_stake_max": round(res.t_max, 2),
        "stake_split_json": _stake_per_book(opp.res.legs),
        "max_profit": round(res.max_profit, 2),
        "binding_book": res.binding_book,
        "min_leg_limit": res.min_leg_limit,
        "shadow_books": opp.shadow_books,
        "involves_exchange": res.involves_exchange,
        "low_confidence": res.low_confidence,
        "suspicious": opp.suspicious,
        "bet_links_json": opp.bet_links,
    }


def _telegram_item(opp: Opportunity) -> dict[str, Any]:
    res = opp.res
    return {
        "match": opp.match,
        "home_team": opp.home_team,
        "away_team": opp.away_team,
        "tournament": opp.tournament,
        "kickoff_utc": opp.kickoff_utc,
        "market": opp.spec.label,
        "market_family": opp.spec.family,
        "market_line": opp.spec.line,
        "roi_pct": res.roi_pct,
        "max_liquidity": res.t_max,
        "total_investment": sum(leg.stake for leg in res.legs),
        "max_profit": res.max_profit,
        "actionable": opp.actionable,
        "suspicious": opp.suspicious,
        "low_confidence": res.low_confidence,
        "involves_exchange": res.involves_exchange,
        "capital_lockup_days": opp.capital_lockup_days,   # group-scope arbs only (None otherwise)
        "legs": [
            {"book": leg.book, "outcome": leg.outcome_name,
             "decimal_odds": leg.decimal_odds, "limit": leg.limit,
             "unverified": leg.unverified, "stake": leg.stake}
            for leg in res.legs
        ],
        "bet_links": opp.bet_links,
    }


def _xlsx_row(opp: Opportunity, now: datetime) -> dict[str, Any]:
    """Structured row for the Excel running log (one per arb per scan). Whole-dollar money."""
    res = opp.res
    market = fmt.market_label(opp.spec.label, opp.spec.family, opp.spec.line)
    legs = []
    for leg in res.legs:
        legs.append({
            "book": leg.book,
            "outcome": _outcome(opp, leg.outcome_name),
            "odds": round(leg.decimal_odds, 2),
            "limit": "UNVERIFIED" if leg.unverified else fmt.round_dollars(leg.limit),
            "stake": fmt.round_dollars(leg.stake),
        })
    total_inv = fmt.round_dollars(sum(leg.stake for leg in res.legs))
    return {
        "scan_time_local": fmt.iso_local(now),
        "scan_time_utc": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "game": opp.match,
        "kickoff_time": fmt.iso_local(opp.kickoff_utc),
        "tournament": opp.tournament,
        "market": market,
        "legs": legs,
        "S": round(res.arb_sum_S, 4),
        "ROI_pct": round(res.roi_pct, 2),
        "T_max": fmt.round_dollars(res.t_max),
        "total_investment": total_inv,
        "guaranteed_profit": fmt.round_dollars(res.max_profit),
        "type": "REAL" if opp.actionable else "SHADOW",
        "low_confidence": "Y" if res.low_confidence else "N",
        "unverified_limit_books": ", ".join(res.unverified_books),
        "capital_lockup_days": "" if opp.capital_lockup_days is None else opp.capital_lockup_days,
        "arb_id": make_arb_id(opp.match, market, res.legs),
    }


# --------------------------------------------------------------------------- #
# Catalog loading / ensuring                                                    #
# --------------------------------------------------------------------------- #
def _ensure_catalogs(client, cfg: Config, acct: dict[str, Any], now_epoch: float, log) -> Optional[dict[str, Any]]:
    """Load cached catalogs; auto-refresh when MISSING or older than ``budget.catalog_cache_hours``.

    The catalog (sports/bookmakers/markets/tournaments) is near-static for a fixed tournament, so its
    TTL is long (default 14 days) and a refresh (~4 billable requests) is rare — the work that keeps
    team names current as the window rolls forward is the SEPARATE names-map refresh in run_cycle. A
    stale-but-present catalog is never discarded: if the budget is too low to refresh we keep using it
    rather than fail the scan. Only a MISSING catalog with no budget is fatal."""
    markets = catalog.load_json(cfg.cache_dir, catalog.MARKETS_FILE)
    books = catalog.load_json(cfg.cache_dir, catalog.BOOKMAKERS_FILE)
    tours = catalog.load_json(cfg.cache_dir, catalog.TOURNAMENTS_FILE)
    have_all = bool(markets) and bool(books) and tours is not None
    cached = {"markets": markets, "bookmakers": books, "tournaments": tours}

    catalog_ttl = float(cfg.budget_opt("catalog_cache_hours", 336))
    ages = [catalog.file_age_hours(cfg.cache_dir, f, now_epoch)
            for f in (catalog.MARKETS_FILE, catalog.BOOKMAKERS_FILE, catalog.TOURNAMENTS_FILE)]
    oldest = max((a for a in ages if a is not None), default=None)
    stale = have_all and catalog_ttl > 0 and oldest is not None and oldest > catalog_ttl

    if have_all and not stale:
        return cached

    remaining = acct.get("remaining")
    min_remaining = int(cfg.budget_opt("refresh_min_remaining", 24))
    if remaining is not None and remaining < min_remaining:
        if have_all:  # stale but usable — never throw away a working catalog to save names
            log.warning("Catalog stale (oldest %.1fh > %.0fh) but only %s requests remain (< %s) — "
                        "keeping the cached catalog this cycle.", oldest, catalog_ttl, remaining, min_remaining)
            return cached
        log.error("Catalogs missing and only %s requests remain (< %s) — run refresh-catalog "
                  "workflow first. Exiting.", remaining, min_remaining)
        return None

    why = "missing" if not have_all else f"stale (oldest {oldest:.1f}h > {catalog_ttl:.0f}h TTL)"
    log.warning("Refreshing catalogs inline — %s (~4 billable requests).", why)
    catalog.refresh_catalogs(client, cfg.cache_dir, cfg.sport_id, log)
    return {
        "markets": catalog.load_json(cfg.cache_dir, catalog.MARKETS_FILE),
        "bookmakers": catalog.load_json(cfg.cache_dir, catalog.BOOKMAKERS_FILE),
        "tournaments": catalog.load_json(cfg.cache_dir, catalog.TOURNAMENTS_FILE),
    }


def _resolve_tournaments(cfg: Config, tournaments_json, log) -> tuple[list[int], list[dict]]:
    if cfg.pinned_tournament_ids:
        log.info("Using pinned tournament IDs: %s", cfg.pinned_tournament_ids)
        return cfg.pinned_tournament_ids, []
    matched = catalog.resolve_tournament_ids(tournaments_json, cfg.tournament_regex, cfg.national_teams_only)
    ids = [int(t["tournamentId"]) for t in matched if t.get("tournamentId") is not None]
    for t in matched:
        log.info("Friendlies match: id=%s name=%r category=%r upcoming=%s future=%s",
                 t.get("tournamentId"), t.get("tournamentName"), t.get("categoryName"),
                 t.get("upcomingFixtures"), t.get("futureFixtures"))
    if not ids:
        log.error("No friendlies tournaments matched regex %r. Pin IDs in config or check the catalog.",
                  cfg.tournament_regex)
    return ids, matched


# --------------------------------------------------------------------------- #
# Per-book odds fetch (free tier returns one bookmaker per call)                #
# --------------------------------------------------------------------------- #
def _fixture_list(payload: Any) -> list[dict[str, Any]]:
    """Coerce an odds payload into a list of fixture dicts (handles list / wrapped / keyed)."""
    if isinstance(payload, list):
        return [f for f in payload if isinstance(f, dict)]
    if isinstance(payload, dict):
        inner = payload.get("fixtures") or payload.get("data")
        if isinstance(inner, list):
            return [f for f in inner if isinstance(f, dict)]
        vals = list(payload.values())
        if vals and all(isinstance(v, dict) for v in vals) and any(
            ("bookmakerOdds" in v or "fixtureId" in v) for v in vals
        ):
            return vals
    return []


def _merge_theoddsapi(cfg, cats, by_fixture, raw_by_fixture, log) -> dict[str, set[str]]:
    """Gap-fill raw_by_fixture with the-odds-api odds. Returns toa_books_by_fixture (fid -> slugs
    this source injected) so the scan can keep those legs shadow-only. Any failure is swallowed
    with a warning — the supplemental feed must never break the OddsPapi run."""
    if not cfg.theoddsapi_enabled:
        return {}
    if not cfg.secrets.odds_api_key:
        log.warning("theoddsapi.enabled but ODDS_API_KEY is not set — skipping supplemental feed.")
        return {}
    try:
        wc_key = str(cfg.theoddsapi_opt("wc_key", "soccer_fifa_world_cup"))
        regions = str(cfg.theoddsapi_opt("regions", "eu"))
        markets = str(cfg.theoddsapi_opt("markets", "h2h,spreads,totals"))
        odds_format = str(cfg.api_opt("odds_format", "decimal"))
        tol = float(cfg.theoddsapi_opt("commence_tolerance_minutes", 120))
        allow = cfg.theoddsapi_opt("books", None)
        allow_books = set(allow) if allow else None
        cost = len([m for m in markets.split(",") if m]) * len([r for r in regions.split(",") if r])

        catalog_slugs = {b.get("slug") for b in (cats.get("bookmakers") or []) if b.get("slug")}
        index = theoddsapi.build_market_index(cats.get("markets") or [], cfg.sport_id)
        if index.ambiguous:
            log.warning("[THE-ODDS-API] ambiguous market indices skipped: %s", "; ".join(index.ambiguous))

        client = theoddsapi.TheOddsApiClient(cfg.secrets.odds_api_key)
        payload = client.wc_odds(wc_key, regions, markets, odds_format)

        cov, toa_books = theoddsapi.merge_into(
            raw_by_fixture, by_fixture, index, catalog_slugs, payload,
            tolerance_minutes=tol, allow_books=allow_books, cost_credits=cost, log=log)
        for line in cov.lines():
            log.info("%s", line)
        log.info("[THE-ODDS-API] quota: %s requests remaining (used %s) | actionable=%s",
                 client.requests_remaining, client.requests_used, cfg.theoddsapi_actionable)
        return toa_books
    except theoddsapi.TheOddsApiError as exc:
        log.warning("the-odds-api feed unavailable (%s) — continuing with OddsPapi only.", exc)
        return {}
    except Exception as exc:  # noqa: BLE001 - supplemental feed must never break the run
        log.warning("the-odds-api merge failed (%s: %s) — continuing with OddsPapi only.",
                    type(exc).__name__, exc)
        return {}


def _merge_kalshi(cfg, cats, by_fixture, raw_by_fixture, now, log) -> dict[str, set[str]]:
    """Gap-fill raw_by_fixture with Kalshi-direct per-match World Cup result odds, overriding the
    suspended OddsPapi `kalshi` book. Returns kalshi_books_by_fixture (fid -> {"kalshi"}) so the
    scan can keep those legs shadow-only while kalshi.actionable is false. Any failure is swallowed
    with a warning — the supplemental feed must never break the OddsPapi run."""
    if not cfg.kalshi_enabled:
        return {}
    try:
        base_url = str(cfg.kalshi_opt("base_url", kalshi.DEFAULT_BASE_URL))
        series = str(cfg.kalshi_opt("series_ticker", "") or "")
        if not series:
            log.warning("kalshi.enabled but series_ticker is empty — skipping kalshi feed.")
            return {}
        index = theoddsapi.build_market_index(cats.get("markets") or [], cfg.sport_id)
        client = kalshi.KalshiClient(base_url=base_url)
        markets = client.iter_markets(series_ticker=series, status="open")
        # SAFE-TIER extras: also pull total-goals O/U (KXWCTOTAL) and BTTS (KXWCBTTS) — same
        # regulation settlement as the 1x2 result, so they are settlement-compatible (see audit).
        # Empty/blank config disables a tier. merge_into groups all three series by game key.
        for extra in (str(cfg.kalshi_opt("totals_series", "") or ""),
                      str(cfg.kalshi_opt("btts_series", "") or "")):
            if extra:
                markets += client.iter_markets(series_ticker=extra, status="open")
        cov, kalshi_books = kalshi.merge_into(raw_by_fixture, by_fixture, index, markets, now=now, log=log)
        for line in cov.lines():
            log.info("%s", line)
        log.info("[KALSHI] actionable=%s (shadow while false)", cfg.kalshi_actionable)
        return kalshi_books
    except kalshi.KalshiError as exc:
        log.warning("kalshi feed unavailable (%s) — continuing without it.", exc)
        return {}
    except Exception as exc:  # noqa: BLE001 - supplemental feed must never break the run
        log.warning("kalshi merge failed (%s: %s) — continuing without it.", type(exc).__name__, exc)
        return {}


def _merge_polymarket(cfg, cats, by_fixture, raw_by_fixture, now, log) -> dict[str, set[str]]:
    """Gap-fill raw_by_fixture with Polymarket-direct per-match World Cup result odds (CLOB best-ask
    prices), filling fixtures where OddsPapi has no active polymarket book. Returns
    poly_books_by_fixture (fid -> {"polymarket"}) so the scan keeps those legs shadow-only while
    polymarket.actionable is false. Any failure is swallowed with a warning — the supplemental feed
    must never break the OddsPapi run."""
    if not cfg.polymarket_enabled:
        return {}
    try:
        index = theoddsapi.build_market_index(cats.get("markets") or [], cfg.sport_id)
        client = polymarket.PolymarketClient(
            gamma_base=str(cfg.polymarket_opt("gamma_base", polymarket.GAMMA_BASE)),
            clob_base=str(cfg.polymarket_opt("clob_base", polymarket.CLOB_BASE)))
        # Pass the index so fetch also pulls each game's '-more-markets' sibling and prices the
        # SAFE-TIER full-game totals O/U + BTTS (reg-time) — the priority Kalshi<->Polymarket surface.
        events = polymarket.fetch_wc_events(client, by_fixture, log, market_index=index)
        cov, poly_books = polymarket.merge_into(raw_by_fixture, by_fixture, index, events, now=now, log=log)
        for line in cov.lines():
            log.info("%s", line)
        log.info("[POLYMARKET] actionable=%s (shadow while false)", cfg.polymarket_actionable)
        return poly_books
    except polymarket.PolymarketError as exc:
        log.warning("polymarket feed unavailable (%s) — continuing without it.", exc)
        return {}
    except Exception as exc:  # noqa: BLE001 - supplemental feed must never break the run
        log.warning("polymarket merge failed (%s: %s) — continuing without it.", type(exc).__name__, exc)
        return {}


def _fetch_odds_per_book(client, cfg, tournament_ids, books, log):
    """One odds-by-tournaments call per book; merge each book's odds onto the shared fixture.

    Returns (raw_by_fixture, fetched_books, returning_books) — the raw per-fixture dicts BEFORE
    normalization, so a supplemental source (the-odds-api) can gap-fill them before parsing. Fetches
    EVERY requested book — there is no per-cycle cap, so any pair of books can form an arb. A
    quota/rate-limit error (HTTP 429/403) raises QuotaExceeded, left to bubble up to main() for a
    clean "replace the key" exit.
    """
    verbosity = int(cfg.api_opt("odds_verbosity", 3))
    odds_format = str(cfg.api_opt("odds_format", "decimal"))
    raw_by_fixture: dict[str, dict[str, Any]] = {}
    fetched: list[str] = []
    returning: list[str] = []

    for book in books:
        try:
            payload = client.odds_by_tournaments(tournament_ids, bookmaker=book,
                                                 verbosity=verbosity, odds_format=odds_format)
        except OddsPapiError as exc:
            # e.g. a book outside the plan (400); count it as attempted and move on.
            log.warning("Skipping %s: %s", book, exc)
            fetched.append(book)
            continue

        fetched.append(book)
        fixtures = _fixture_list(payload)
        had_data = False
        for fx in fixtures:
            fid = fx.get("fixtureId")
            if not fid:
                continue
            fid = str(fid)
            book_odds = fx.get("bookmakerOdds") or {}
            if fid not in raw_by_fixture:
                merged = dict(fx)
                merged["bookmakerOdds"] = dict(book_odds)
                raw_by_fixture[fid] = merged
            else:
                raw_by_fixture[fid]["bookmakerOdds"].update(book_odds)
            if book_odds:
                had_data = True
        if had_data:
            returning.append(book)
        log.info("  fetched %-14s -> %s fixture(s)%s", book, len(fixtures),
                 "" if had_data else " (no odds for this book in window)")

    return raw_by_fixture, fetched, returning


# --------------------------------------------------------------------------- #
# Main cycle                                                                     #
# --------------------------------------------------------------------------- #
def run_cycle(cfg: Config, log) -> int:
    now = datetime.now(timezone.utc)
    # The scan window — a rolling 2-day UTC range, or a FROM_DATE/TO_DATE workflow_dispatch
    # override — is resolved in load_config() and lives on cfg.from_utc / cfg.to_utc, so every
    # downstream consumer (names refresh, _scan, Telegram header) sees the same range.
    from_utc, to_utc = cfg.from_utc, cfg.to_utc
    log.info("=" * 78)
    log.info("SCAN @ %s | window %s -> %s UTC", fmt.fmt_dt(now), from_utc, to_utc)
    mode = []
    if not cfg.oddspapi_fetch_odds:
        mode.append("FREE-PROFILE (no OddsPapi odds; odds from the-odds-api/kalshi/polymarket)")
    if cfg.local_run:
        mode.append("LOCAL_RUN (Telegram on, git skipped by runner)")
    if cfg.dry_run:
        mode.append("DRY_RUN (Telegram + CSV suppressed)")
    if mode:
        log.info("MODE: %s", " | ".join(mode))

    if not cfg.secrets.odds_papi_key:
        log.error("ODDS_PAPI_KEY not set — cannot run.")
        return 0

    client = OddsPapiClient(cfg.secrets.odds_papi_key, logger=log)

    # 1) Budget guard (free call) -------------------------------------------------
    # A 429/403 here (dead or invalid key) raises QuotaExceeded, handled cleanly in main().
    safety = int(cfg.budget_opt("safety_margin", 15))
    acct = check_budget(client, safety, log)
    if not acct.get("safe_to_run", True):
        log.warning("Request budget nearly gone (remaining=%s <= margin=%s). Skipping scan.",
                    acct.get("remaining"), safety)
        if cfg.secrets.telegram_ready and not cfg.dry_run:
            send_message(cfg.secrets.telegram_bot_key, cfg.secrets.telegram_group_id,
                         f"⚠️ Arb bot paused: only {acct.get('remaining')} API requests left this month.", log)
        return 0

    # 2) Catalogs ----------------------------------------------------------------
    cats = _ensure_catalogs(client, cfg, acct, now.timestamp(), log)
    if not cats or not cats.get("markets") or not cats.get("bookmakers"):
        log.error("Required catalogs unavailable. Exiting.")
        return 0

    specs, skipped = catalog.build_market_specs(
        cats["markets"], cfg.sport_id, cfg.exclude_market_names, cfg.exclude_future_names)
    clone_group_of = catalog.build_clone_group_fn(cats["bookmakers"])
    n_futures = sum(1 for s in skipped if s.get("reason") == "out_of_scope_future")
    log.info("Market catalog: %s MECE markets accepted, %s skipped "
             "(player-prop/excluded/non-MECE; %s out-of-scope futures).",
             len(specs), len(skipped), n_futures)

    # 3) Tournaments -------------------------------------------------------------
    tournament_ids, _matched = _resolve_tournaments(cfg, cats.get("tournaments") or [], log)
    if not tournament_ids:
        return 0

    # 3b) Which books can we query, and afford this cycle? -----------------------
    # Free/standard plans return ONE bookmaker per odds call, so each book = 1 request. In the FREE
    # PROFILE (oddspapi.fetch_odds=false) we spend ZERO OddsPapi odds requests — every book's prices
    # come from the supplemental feeds instead — so this whole section is skipped (to_fetch empty).
    to_fetch: list[str] = []
    if cfg.oddspapi_fetch_odds:
        granted = acct.get("bookmakers")
        if granted:
            log.info("Plan grants %s bookmaker(s): %s", len(granted), ", ".join(granted))
        else:
            log.info("Plan does not enumerate bookmakers; will try the configured ones.")
        # Actionable books first (they drive real arbs), then any extra tracked books.
        fetch_order = cfg.actionable_books + [b for b in cfg.tracked_books if b not in cfg.actionable_books]
        if granted:
            grant_set = set(granted)
            usable = [b for b in fetch_order if b in grant_set]
            blocked = [b for b in fetch_order if b not in grant_set]
            if blocked:
                log.info("Configured books NOT in your plan (skipped): %s", blocked)
        else:
            usable = list(fetch_order)
        if len(usable) < 2:
            log.warning("Only %s usable bookmaker(s): %s. Arbitrage needs >=2 distinct books, so there is "
                        "nothing to compute. Upgrade the plan / enable more books, then re-run. "
                        "Exiting now (0 billable odds requests spent).", len(usable), usable or "none")
            return 0
        # No per-cycle cap: fetch EVERY usable book so any pair can form an arb (one request each).
        to_fetch = usable
        log.info("Fetching all %s usable book(s) this cycle: %s", len(to_fetch), to_fetch)
    else:
        log.info("FREE PROFILE: skipping OddsPapi odds fetch (0 billable odds requests). Odds will come "
                 "from the-odds-api (Pinnacle + 1xBet), Kalshi-direct, and Polymarket-direct.")

    # 4) Names map — refresh when the cache is older than names_cache_hours OR its fingerprint
    #    (tournament set / window) no longer matches this scan. The fingerprint check is the guard
    #    against a stale-content-but-fresh-mtime cache (e.g. a committed map for an old scope)
    #    silently matching nothing — the mtime TTL alone never fires in that case.
    names = catalog.load_json(cfg.cache_dir, catalog.NAMES_FILE) or {}
    names_age = catalog.file_age_hours(cfg.cache_dir, catalog.NAMES_FILE, now.timestamp())
    names_ttl = float(cfg.budget_opt("names_cache_hours", 12))
    fp_reason = (catalog.names_fingerprint_stale(names, tournament_ids, cfg.from_utc, cfg.to_utc)
                 if names else None)
    ttl_stale = names_age is None or names_age > names_ttl
    remaining = acct.get("remaining")
    if ttl_stale or fp_reason:
        if remaining is not None and (remaining - client.billable_count) <= safety + len(to_fetch):
            log.warning("Skipping names refresh to preserve odds budget (remaining=%s).", remaining)
        else:
            # A quota/rate-limit error here means the key is dead; let it bubble up to main().
            reason = fp_reason or (f"cache age {names_age:.1f}h > {names_ttl}h ttl"
                                   if names_age is not None else "missing")
            log.info("Refreshing fixtures name map (%s).", reason)
            names = catalog.refresh_names(client, cfg.cache_dir, cfg.sport_id, tournament_ids,
                                          cfg.from_utc, cfg.to_utc, now.timestamp())
    by_fixture = names.get("by_fixture", {})
    by_participant = names.get("by_participant", {})

    # 5) Odds — ONE billable call per book, merged across books (skipped in the FREE PROFILE) ------
    if to_fetch:
        raw_by_fixture, fetched_books, returning_books = _fetch_odds_per_book(
            client, cfg, tournament_ids, to_fetch, log)
        log.info("Odds fetch: %s book-call(s); %s returned odds (%s).",
                 len(fetched_books), len(returning_books), ", ".join(returning_books) or "none")
    else:
        raw_by_fixture, fetched_books, returning_books = {}, [], []

    # 5b) Supplemental: the-odds-api gap-fill (shadow-only until theoddsapi.actionable is true) ----
    toa_books_by_fixture = _merge_theoddsapi(cfg, cats, by_fixture, raw_by_fixture, log)

    # 5c) Supplemental: Kalshi-direct gap-fill (overrides suspended kalshi; shadow until kalshi.actionable) -
    kalshi_books_by_fixture = _merge_kalshi(cfg, cats, by_fixture, raw_by_fixture, now, log)

    # 5d) Supplemental: Polymarket-direct gap-fill (CLOB best-ask; shadow until polymarket.actionable) -
    poly_books_by_fixture = _merge_polymarket(cfg, cats, by_fixture, raw_by_fixture, now, log)

    feeds = normalize.parse_odds_payload(list(raw_by_fixture.values()))
    # Count books across the MERGED feed (OddsPapi + supplemental), not just OddsPapi's returning_books,
    # so the warning is correct in the FREE PROFILE where all odds arrive via the supplemental sources.
    feed_books = set().union(*(fx.books_present for fx in feeds)) if feeds else set()
    if len(feed_books) < 2:
        log.warning("Fewer than 2 books priced this cycle (%s) -> no cross-book arbitrage possible.",
                    ", ".join(sorted(feed_books)) or "none")

    seen_mids = normalize.seen_market_ids(feeds)
    log.info("Feed: %s fixtures with odds. Distinct marketIds seen: %s", len(feeds), sorted(seen_mids))
    missing = [mid for mid in seen_mids if mid not in specs]
    if missing:
        log.info("marketIds seen but NOT scanned (player-prop/excluded/unclassified): %s", sorted(missing))

    # the-odds-api books join the TRACKED (shadow) universe at runtime — so shadow arbs can use
    # them — without being added to config bookmakers.tracked (which would make OddsPapi fetch &
    # bill them). They are deliberately NOT added to actionable.
    toa_all_books: set[str] = set().union(*toa_books_by_fixture.values()) if toa_books_by_fixture else set()
    if toa_all_books:
        log.info("[THE-ODDS-API] added to TRACKED (shadow) universe only: %s", ", ".join(sorted(toa_all_books)))

    guard_on = bool(cfg.mapping_guard_opt("enabled", True))
    reference_books = [str(b) for b in (cfg.mapping_guard_opt("reference_books", ["pinnacle", "1xbet"]) or [])]
    ctx = EngineCtx(
        actionable=set(cfg.actionable_books),
        tracked=set(cfg.tracked_books) | toa_all_books,
        exchanges=cfg.exchanges,
        commission=cfg.commission,
        clone_group_of=clone_group_of,
        assumed_unknown_limit=cfg.assumed_unknown_limit,
        assumed_unknown_limit_by_book=cfg.assumed_unknown_limit_by_book,
        bankroll_total=cfg.bankroll_total,
        # Time-to-kickoff-aware staleness tiers (relaxed pre-match; tight inside the final hour).
        max_leg_age_far=float(cfg.threshold("max_leg_age_far_minutes", 360)),
        max_leg_age_mid=float(cfg.threshold("max_leg_age_mid_minutes", 60)),
        max_leg_age_near=float(cfg.threshold("max_leg_age_near_minutes", 20)),
        stale_far_horizon_hours=float(cfg.threshold("stale_far_horizon_hours", 6)),
        stale_near_horizon_hours=float(cfg.threshold("stale_near_horizon_hours", 1)),
        low_confidence_limit_floor=float(cfg.threshold("low_confidence_limit_floor", 10)),
        reference_books=reference_books if guard_on else [],
        min_favorite_ratio=float(cfg.mapping_guard_opt("min_favorite_ratio", 1.5)),
        dump_book=os.environ.get("DEBUG_DUMP_BOOK") or str(cfg.mapping_guard_opt("dump_book", "") or ""),
    )

    # 6) Scan --------------------------------------------------------------------
    opportunities, stats = _scan(feeds, specs, ctx, cfg, by_fixture, by_participant, now, log,
                                 toa_books_by_fixture, kalshi_books_by_fixture, poly_books_by_fixture)

    # 6b) Player-goals tier (STEP 5, default OFF): Poly -player-props <-> Kalshi KXWCGOAL, group-stage
    # only (settlement gate). Detected separately (synthetic per-player markets), appended to the
    # opportunities so it flows through CSV/xlsx/alerts unchanged. Drop-safe — never breaks the run.
    if cfg.player_props_enabled:
        try:
            pp = player_props.detect(cfg, by_fixture, now, log)
            for opp in pp:
                if opp.suspicious:                       # log-only, never alerted (mirrors _scan)
                    stats["suspicious_arbs"] += 1
                elif opp.actionable:
                    stats["real_arbs"] += 1
                else:
                    stats["shadow_arbs"] += 1
            opportunities.extend(pp)
        except Exception as exc:  # noqa: BLE001 - supplemental tier must never break the run
            log.warning("player-props tier failed (%s: %s) — continuing without it.",
                        type(exc).__name__, exc)

    # 6c) Group-stage outcome tier (SCOPE_GROUP, default OFF): cross-exchange group winner/bottom
    # (SAFE) + qualify (DANGEROUS, logged-only). Appended like player props. Drop-safe.
    if cfg.group_markets_enabled:
        try:
            gm = group_markets.detect(cfg, now, log)
            for opp in gm:
                if opp.suspicious:
                    stats["suspicious_arbs"] += 1
                elif opp.actionable:
                    stats["real_arbs"] += 1
                else:
                    stats["shadow_arbs"] += 1
            opportunities.extend(gm)
        except Exception as exc:  # noqa: BLE001 - supplemental tier must never break the run
            log.warning("group-markets tier failed (%s: %s) — continuing without it.",
                        type(exc).__name__, exc)

    # 7) Output ------------------------------------------------------------------
    _emit(opportunities, stats, cfg, now, client, log)
    return 0


def _scan(feeds, specs, ctx, cfg, by_fixture, by_participant, now, log, toa_books_by_fixture=None,
          kalshi_books_by_fixture=None, poly_books_by_fixture=None):
    toa_books_by_fixture = toa_books_by_fixture or {}
    kalshi_books_by_fixture = kalshi_books_by_fixture or {}
    poly_books_by_fixture = poly_books_by_fixture or {}
    theoddsapi_actionable = cfg.theoddsapi_actionable
    theoddsapi_actionable_books = cfg.theoddsapi_actionable_books  # None => no per-book restriction
    kalshi_actionable = cfg.kalshi_actionable
    poly_actionable = cfg.polymarket_actionable
    from_dt = _parse_iso(cfg.from_utc)
    to_dt = _parse_iso(cfg.to_utc)
    min_roi = float(cfg.threshold("min_roi_pct", 0.5))
    min_stake = float(cfg.threshold("min_total_stake", 20))
    susp_pct = float(cfg.threshold("roi_suspicious_pct", 8.0))
    near_ceiling = float(cfg.threshold("near_miss_ceiling_S", 1.02))
    # Every book in play, in priority order (actionable first), for the COVERAGE diagnostic. Uses
    # ctx.tracked so runtime-added the-odds-api shadow books show their per-fixture status too.
    actionable_first = list(cfg.actionable_books)
    coverage_books = actionable_first + [b for b in sorted(ctx.tracked) if b not in set(actionable_first)]
    allow_quarter = cfg.allow_quarter_lines

    opportunities: dict[str, Opportunity] = {}
    closest: list[tuple] = []  # (S, roi_pct, match, label, leg_summary, t_max, is_arb)
    stats = {
        "fixtures_in_window": 0,
        "fixtures_skipped_status": 0,
        "markets_scanned": 0,
        "markets_complete": 0,
        "real_arbs": 0,
        "shadow_arbs": 0,
        "near_misses": 0,
        "arbs_below_threshold": 0,
        "shadow_book_counter": Counter(),
        "mapping_suspect_flags": Counter(),
        "funded_arbs": [],   # (roi_pct, S, match, market, books, t_max, above_floor) — funded-only
        "suspicious_arbs": 0,  # log-only: never alert, never count as real/shadow
    }

    for fx in feeds:
        if fx.status_id in (2, 3):
            stats["fixtures_skipped_status"] += 1
            continue
        start = _parse_iso(fx.start_time)
        if start is None or start <= now:
            continue
        if from_dt and start < from_dt:
            continue
        if to_dt and start > to_dt:
            continue

        home_team, away_team = _teams(fx, by_fixture, by_participant)
        match = f"{home_team} vs {away_team}"
        info = by_fixture.get(fx.fixture_id, {})
        if info.get("status_id") in (2, 3):  # cancelled per the name map (e.g. struck-through fixture)
            stats["fixtures_skipped_status"] += 1
            continue
        tournament = info.get("tournament") or ""
        stats["fixtures_in_window"] += 1
        leg_age_limit = ctx.max_leg_age_for(start, now)
        log.info("MATCH: %s | %s | books with odds: %s",
                 match, fx.start_time, ", ".join(sorted(fx.books_present)) or "none")
        log.info("  %s", _coverage_line(fx, match, coverage_books, specs, allow_quarter, now, leg_age_limit))

        for mid, raw_market in fx.markets.items():
            spec = specs.get(mid)
            if spec is None:
                continue
            if spec.has_quarter_line and not cfg.allow_quarter_lines:
                continue
            # No-push rule: a HANDICAP on a whole-number line refunds on an exact-margin result, so a
            # such a pair is not a riskless arb. Only no-push half-lines (±1.5, ±2.5, …) may pair.
            if spec.family in ("asian_handicap", "euro_handicap") and spec.is_whole_line:
                continue
            stats["markets_scanned"] += 1

            # Outcome-mapping guard: drop books whose favourite/underdog look swapped (see above).
            book_prices = _book_prices_by_outcome(raw_market)
            suspects, ref_book, extremes = _detect_mapping_suspects(
                book_prices, ctx.reference_books, ctx.min_favorite_ratio)
            if suspects and extremes:
                _log_mapping_suspects(log, match, home_team, away_team, spec,
                                      book_prices, suspects, ref_book, extremes)
                for b in suspects:
                    stats["mapping_suspect_flags"][b] += 1
            if ctx.dump_book and ctx.dump_book in book_prices:
                _dump_book_outcomes(log, match, spec, home_team, away_team, ctx.dump_book,
                                    book_prices[ctx.dump_book])

            # Per-book actionable gate for the-odds-api legs. A recovered slug may turn an arb
            # actionable ONLY if (a) the master switch theoddsapi.actionable is on, (b) the slug is
            # in theoddsapi.actionable_books (None => no per-book restriction), AND (c) — for spreads
            # — an OddsPapi (non-the-odds-api) book prices the same marketId+handicap, validating the
            # sign mapping. Any recovered book failing this is excluded from the actionable UNIVERSE
            # (so no pure-actionable arb selects it) AND forces `actionable` False on any result that
            # still contains it — the actionable-slug check alone is not enough, since a recovered
            # soft book may itself sit in bookmakers.actionable. The gate is PER-BOOK, so 1xbet can
            # be actionable on a fixture while a soft book recovered on the same fixture cannot.
            toa_here = toa_books_by_fixture.get(fx.fixture_id, frozenset())
            toa_blocked: set[str] = set()
            if toa_here:
                spread_unvalidated = False
                if spec.family == "asian_handicap":
                    nontoa = {rc.book for raws in raw_market.values() for rc in raws if rc.book not in toa_here}
                    spread_unvalidated = not nontoa                # no OddsPapi book validates the line
                for b in toa_here:
                    if (not theoddsapi_actionable
                            or (theoddsapi_actionable_books is not None and b not in theoddsapi_actionable_books)
                            or spread_unvalidated):
                        toa_blocked.add(b)

            # Kalshi-direct shadow gate: while kalshi.actionable is false, the recovered `kalshi` leg
            # is blocked from actionable (kalshi is already in bookmakers.actionable, so without this
            # it would go actionable on the first run). Same rollout posture as the-odds-api.
            kalshi_here = kalshi_books_by_fixture.get(fx.fixture_id, frozenset())
            kalshi_blocked = set(kalshi_here) if (kalshi_here and not kalshi_actionable) else set()

            # Polymarket-direct shadow gate: identical posture — while polymarket.actionable is false,
            # the injected `polymarket` leg (already in bookmakers.actionable) is forced non-actionable.
            poly_here = poly_books_by_fixture.get(fx.fixture_id, frozenset())
            poly_blocked = set(poly_here) if (poly_here and not poly_actionable) else set()

            blocked = toa_blocked | kalshi_blocked | poly_blocked
            real_exclude = suspects | blocked

            real = _arb_for_universe(raw_market, spec, ctx.actionable, ctx, now, leg_age_limit, real_exclude)
            shadow = _arb_for_universe(raw_market, spec, ctx.tracked, ctx, now, leg_age_limit, suspects)

            # FUNDED-ONLY transparency: record every funded (kalshi/polymarket/pinnacle/1xbet) arb that
            # clears S<1, EVEN below the ROI/stake floor, so a funded arb can never be silently hidden
            # behind a juicier shadow-book price. (Funded arbs above the floor also surface as real
            # opportunities below; this list is the complete funded picture for the end-of-run report.)
            if real is not None and real.is_arb:
                books = "+".join(sorted({lg.book for lg in real.legs}))
                above = real.roi_pct >= min_roi and (real.t_max >= min_stake or real.low_confidence)
                stats["funded_arbs"].append(
                    (real.roi_pct, real.arb_sum_S, match, spec.label, books, real.t_max, above))

            # The broadest complete result for this market — used for diagnostics so we can
            # SEE how close the market got, even when nothing clears the arb threshold.
            probe = shadow if shadow is not None else real
            if probe is not None:
                stats["markets_complete"] += 1
                leg_summary = " | ".join(f"{lg.outcome_name} {lg.decimal_odds:g}@{lg.book}"
                                         for lg in probe.legs)
                closest.append((probe.arb_sum_S, probe.roi_pct, match, spec.label,
                                leg_summary, probe.t_max, probe.is_arb))
                if not probe.is_arb and probe.arb_sum_S <= near_ceiling:
                    stats["near_misses"] += 1
                if probe.is_arb and (probe.roi_pct < min_roi or probe.t_max < min_stake):
                    stats["arbs_below_threshold"] += 1

            for res in (real, shadow):
                if res is None or not res.is_arb:
                    continue
                if res.roi_pct < min_roi:
                    continue
                # Low-confidence arbs (null/tiny limit, e.g. thin Polymarket/Kalshi) are kept even
                # below the stake floor — the human judges them; the flag is carried to Telegram.
                if res.t_max < min_stake and not res.low_confidence:
                    continue

                # A blocked supplemental leg (the-odds-api: master off / not in actionable_books /
                # unvalidated spread; kalshi: shadow gate) forces the arb non-actionable, even though
                # the recovered slug (1xbet, kalshi) may itself sit in bookmakers.actionable.
                has_blocked = any(leg.book in blocked for leg in res.legs)
                actionable = (all(leg.book in ctx.actionable for leg in res.legs)
                              and not has_blocked)
                shadow_books = [leg.book for leg in res.legs
                                if leg.book not in ctx.actionable or leg.book in toa_here
                                or leg.book in kalshi_here or leg.book in poly_here]
                suspicious = res.roi_pct > susp_pct
                sig = make_signature(fx.fixture_id, mid, spec.line, res.legs)
                bet_links = {leg.book: fx.fixture_paths.get(leg.book, "") for leg in res.legs}

                opp = Opportunity(
                    fixture_id=fx.fixture_id, match=match, home_team=home_team, away_team=away_team,
                    tournament=tournament, kickoff_utc=fx.start_time, spec=spec, res=res,
                    actionable=actionable, shadow_books=shadow_books, suspicious=suspicious,
                    bet_links=bet_links, signature=sig,
                )
                prev = opportunities.get(sig)
                if prev is None or (actionable and not prev.actionable):
                    opportunities[sig] = opp

    # Tally after dedup so each opportunity counts once. SUSPICIOUS arbs (ROI above the suspicious
    # ceiling — almost always a thin longshot / stale-line artifact) are LOG-ONLY: they never count
    # as a real OR shadow arb and never reach Telegram (see _emit). They are still logged + written
    # to the CSV/xlsx for the record.
    for opp in opportunities.values():
        if opp.suspicious:
            stats["suspicious_arbs"] += 1
            continue
        if opp.actionable:
            stats["real_arbs"] += 1
        else:
            stats["shadow_arbs"] += 1
            for b in opp.shadow_books:
                stats["shadow_book_counter"][b] += 1

    # Log the full calc for each (deduped) opportunity.
    for opp in sorted(opportunities.values(), key=lambda o: o.res.max_profit, reverse=True):
        _log_arb_calc(log, opp, now)

    # Diagnostic: the markets that came CLOSEST to an arb (lowest S). This proves the engine
    # is computing on live odds even when zero arbs clear, and reveals near-fair markets.
    n_report = int(cfg.threshold("closest_report_count", 10))
    if closest:
        closest.sort(key=lambda c: c[0])  # ascending S — lowest first
        log.info("-" * 78)
        log.info("CLOSEST MARKETS to an arb (lowest implied-probability sum S; S<1 would be an arb):")
        for S, roi, match, label, legs, t_max, is_arb in closest[:n_report]:
            tag = ""
            if is_arb:
                tag = "  <-- ARB but below ROI/stake floor"
            log.info("  S=%.4f (ROI %+.2f%%)%s | %s | %s | %s",
                     S, roi, tag, match, label, legs)
        best_S = closest[0][0]
        log.info("Best market was S=%.4f -> overround %.2f%% (need S<1.0000 for a riskless arb).",
                 best_S, (best_S - 1.0) * 100.0)

    return list(opportunities.values()), stats


def _teams(fx: FixtureFeed, by_fixture: dict, by_participant: dict) -> tuple[str, str]:
    """Return (home, away) team names — home is participant1, away is participant2."""
    info = by_fixture.get(fx.fixture_id)
    if info and info.get("p1") and info.get("p2"):
        return str(info["p1"]), str(info["p2"])
    home = by_participant.get(str(fx.participant1_id), f"Team {fx.participant1_id}")
    away = by_participant.get(str(fx.participant2_id), f"Team {fx.participant2_id}")
    return str(home), str(away)


def _match_name(fx: FixtureFeed, by_fixture: dict, by_participant: dict) -> str:
    home, away = _teams(fx, by_fixture, by_participant)
    return f"{home} vs {away}"


def _rank_key(cfg: Config):
    if str(cfg.telegram_opt("rank_by", "profit")).lower() == "roi":
        return lambda o: o.rank_roi
    return lambda o: o.rank_profit


# --------------------------------------------------------------------------- #
# Telegram notify throttle (hourly "no real arbs" summary)                      #
# --------------------------------------------------------------------------- #
NOTIFY_STATE_FILE = "notify_state.json"


def _notify_state_path(cfg: Config) -> str:
    return os.path.join(cfg.cache_dir, NOTIFY_STATE_FILE)


def _load_notify_state(cfg: Config) -> dict[str, Any]:
    try:
        with open(_notify_state_path(cfg), "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def _save_notify_state(cfg: Config, state: dict[str, Any]) -> None:
    try:
        os.makedirs(cfg.cache_dir, exist_ok=True)
        with open(_notify_state_path(cfg), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError:  # pragma: no cover - disk error
        pass


def _send_heartbeat(opportunities, stats, cfg: Config, now: datetime, log, local_tz: str) -> tuple[int, str]:
    """On zero REAL arbs, send a short 'bot alive' ping at most once per heartbeat_min_interval_min.

    Returns (sent_count, reason) for the per-scan TELEGRAM status line. Real arbs are handled by the
    caller and always send immediately — this is only the no-real-arb path."""
    n_fix = stats["fixtures_in_window"]
    shadow = stats["shadow_arbs"]
    if not cfg.heartbeat_enabled:
        return 0, "heartbeat disabled"

    interval = cfg.heartbeat_min_interval_min
    state = _load_notify_state(cfg)
    last = _parse_iso(state.get("last_heartbeat_utc"))
    if interval > 0 and last is not None and (now - last).total_seconds() < interval * 60.0:
        mins = (now - last).total_seconds() / 60.0
        return 0, f"heartbeat throttled (last {mins:.0f}m ago < {interval:.0f}m); 0 real, {shadow} shadow"

    msg = (f"Scan @ {fmt.fmt_dt(now, local_tz)} — 0 arbs in {n_fix} fixtures. "
           f"Bot alive, next ~10 min.")
    if shadow:
        msg += f"\n({shadow} shadow-only tracked — not bettable, no account there)"

    if cfg.dry_run:
        log.info("[dry_run] Would send heartbeat:\n%s", msg)
        return 0, "dry-run (heartbeat not sent)"
    if not cfg.secrets.telegram_ready:
        return 0, "telegram not configured (no token/chat_id)"
    if send_message(cfg.secrets.telegram_bot_key, cfg.secrets.telegram_group_id, msg, log):
        state["last_heartbeat_utc"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        _save_notify_state(cfg, state)
        return 1, "heartbeat sent (0 real arbs)"
    return 0, "heartbeat send FAILED"


def _maybe_send_shadow_digest(sb_rows, cfg: Config, now: datetime, log) -> str:
    """Send the 'books to fund next' digest once per local day, at/after shadow_digest_hour.

    Independent of the real-arb alert and heartbeat. Throttled via notify_state
    last_shadow_digest_date (local calendar date). Returns a status string for the log."""
    local_tz = str(cfg.telegram_opt("local_tz", "America/Toronto"))
    now_local = fmt.to_local(now, local_tz)
    if now_local.hour < cfg.shadow_digest_hour:
        return f"before digest hour ({now_local.hour}h < {cfg.shadow_digest_hour}h)"
    today = now_local.strftime("%Y-%m-%d")
    state = _load_notify_state(cfg)
    if state.get("last_shadow_digest_date") == today:
        return "already sent today"
    msg = scoreboard.format_digest(sb_rows, cfg.shadow_window_hours)
    if msg is None:
        return "no unlock books to suggest yet"
    if cfg.dry_run:
        log.info("[dry_run] Would send shadow digest:\n%s", msg)
        return "dry-run (not sent)"
    if not cfg.secrets.telegram_ready:
        return "telegram not configured"
    if send_message(cfg.secrets.telegram_bot_key, cfg.secrets.telegram_group_id, msg, log):
        state["last_shadow_digest_date"] = today
        _save_notify_state(cfg, state)
        return "sent"
    return "send FAILED"


def _emit(opportunities, stats, cfg: Config, now, client, log):
    # --- CSV ---
    # dry_run is verification-only: never mutate the opportunities CSV (mirrors the Telegram guards).
    if cfg.dry_run:
        log.info("[dry_run] Would write %s opportunity row(s) to %s (suppressed).",
                 len(opportunities), cfg.csv_path)
    elif opportunities:
        rows = [_csv_row(o, now) for o in opportunities]
        counts = append_opportunities(cfg.csv_path, rows, now,
                                      float(cfg.threshold("csv_dedup_minutes", 90)))
        log.info("CSV: %s new, %s updated -> %s", counts["new"], counts["updated"], cfg.csv_path)
    else:
        log.info("CSV: no opportunities to write.")

    # --- Excel running log (every arb, REAL and SHADOW, one row per arb per scan) ---
    if cfg.dry_run:
        log.info("[dry_run] Would append %s arb row(s) to %s (suppressed).",
                 len(opportunities), cfg.xlsx_path)
    elif opportunities:
        n = excel_log.append_arbs(cfg.xlsx_path, [_xlsx_row(o, now) for o in opportunities], log)
        log.info("XLSX: appended %s arb row(s) -> %s", n, cfg.xlsx_path)

    # --- Shadow-book scoreboard (which non-funded book to open next; refreshed every scan) ---
    # Built from the CSV (it carries suspicious + per-leg books over history), written AFTER the CSV
    # write above so this scan's arbs are included.
    funded = set(cfg.actionable_books)
    sb_rows = scoreboard.build_scoreboard(cfg.csv_path, funded, cfg.shadow_window_hours, now)
    if cfg.dry_run:
        log.info("[dry_run] Would refresh shadow_scoreboard (%s book(s)) in %s.",
                 len(sb_rows), cfg.xlsx_path)
    else:
        excel_log.write_scoreboard(cfg.xlsx_path, sb_rows, cfg.shadow_window_hours,
                                   now.strftime("%Y-%m-%dT%H:%M:%SZ"), log)
        log.info("SCOREBOARD: %s non-funded book(s) over %.0fh -> %s sheet.",
                 len(sb_rows), cfg.shadow_window_hours, "shadow_scoreboard")
    top_unlock = [r for r in sb_rows if r["unlock_count"] > 0][:5]
    if top_unlock:
        log.info("         Top unlock books: " + " | ".join(
            f"{r['book']} ({r['unlock_count']} arbs, avg ROI {r['avg_roi_pct']:.2f}%)" for r in top_unlock))

    # --- Telegram ---
    # A window banner heads every message so the group always knows the range we covered.
    window_line = f"📅 Scanning: {fmt.window_label(cfg.from_utc, cfg.to_utc)}"
    rank = _rank_key(cfg)
    # SENDABLE = actionable AND NOT suspicious. Suspicious arbs (huge ROI from thin longshot
    # liquidity) are NEVER alerted. Shadow arbs are surfaced via the heartbeat / scoreboard.
    sendable = [o for o in opportunities if o.actionable and not o.suspicious]
    # ALERT FILTERS (gate ALERTS only — CSV/xlsx above already logged ALL opportunities):
    #   (1) fixture kicks off within alert_max_days_out days, (2) guaranteed profit >= alert_min_profit.
    max_days, min_profit = cfg.alert_max_days_out, cfg.alert_min_profit

    def _within_window(o) -> bool:
        if max_days <= 0 or o.is_group_scope:
            return True            # group-scope arbs resolve at group end (~10d) — window doesn't apply
        ko = fmt.parse_iso(o.kickoff_utc)
        return ko is not None and 0 <= (ko - now).total_seconds() <= max_days * 86400.0

    alertable = [o for o in sendable if o.res.max_profit >= min_profit and _within_window(o)]
    filtered = len(sendable) - len(alertable)
    flt = (f"; {filtered} filtered (>{max_days:g}d out / <${min_profit:g})" if filtered else "")
    top = sorted(alertable, key=rank, reverse=True)[:3]

    header = (f"{window_line}\n"
              f"⚽ <b>Arb scan</b> — {fmt.fmt_dt(now)}\n"
              f"{stats['real_arbs']} real · {stats['shadow_arbs']} shadow · "
              f"top {len(top)} below")
    local_tz = str(cfg.telegram_opt("local_tz", "America/Toronto"))

    sent = 0
    suppressed = 0
    if cfg.dry_run:
        # Verification: render exactly what WOULD be sent (post-filter), send nothing.
        if top:
            preview = build_message([_telegram_item(o) for o in top], header, local_tz)
            log.info("[dry_run] Telegram preview (%s alertable arb(s)%s):\n%s", len(top), flt, preview)
            reason = f"dry-run (would send {len(top)} alert(s){flt})"
        else:
            _send_heartbeat(opportunities, stats, cfg, now, log, local_tz)  # logs would-send heartbeat
            reason = f"dry-run (0 alertable arbs{flt}; heartbeat path)"
    elif top:
        # >=1 arb passes the filters -> ALWAYS send the top-3 alert immediately (no throttle).
        if not cfg.secrets.telegram_ready:
            reason = f"{len(top)} alertable arb(s) but TELEGRAM OFF (no token/chat_id)"
        else:
            msg = build_message([_telegram_item(o) for o in top], header, local_tz)
            if send_message(cfg.secrets.telegram_bot_key, cfg.secrets.telegram_group_id, msg, log):
                sent = len(top)
                reason = f"{sent} alert(s) sent immediately{flt}"
            else:
                reason = f"{len(top)} alertable arb(s) but Telegram send FAILED"
    else:
        # Nothing alertable (no real arbs, or all filtered out) -> a throttled heartbeat ping.
        sent, reason = _send_heartbeat(opportunities, stats, cfg, now, log, local_tz)
        reason += flt
        if sent == 0 and ("throttled" in reason or "disabled" in reason):
            suppressed = 1

    # One-line, EVERY scan: is Telegram working, did we send, and if not, why.
    if cfg.dry_run:
        tg_state = "OFF (dry-run)"
    elif cfg.secrets.telegram_ready:
        tg_state = "ON"
    else:
        tg_state = "OFF (no token)"
    log.info("TELEGRAM: %s | %s sent | %s suppressed | %s", tg_state, sent, suppressed, reason)

    # Once-daily shadow-book digest (independent of the arb alert / heartbeat).
    log.info("SHADOW DIGEST: %s", _maybe_send_shadow_digest(sb_rows, cfg, now, log))

    # --- Summary ---
    log.info("-" * 78)
    log.info("SUMMARY: %s fixtures in window | %s skipped(status) | %s markets scanned (%s complete)",
             stats["fixtures_in_window"], stats["fixtures_skipped_status"],
             stats["markets_scanned"], stats.get("markets_complete", 0))
    log.info("         %s real arbs | %s shadow arbs | %s suspicious (log-only, never alerted) | "
             "%s near-misses | %s arb(s) below ROI/stake floor | %s sent",
             stats["real_arbs"], stats["shadow_arbs"], stats.get("suspicious_arbs", 0),
             stats["near_misses"], stats.get("arbs_below_threshold", 0), sent)
    # FUNDED-ONLY arbs (kalshi/polymarket/pinnacle/1xbet only), incl. below floor — so a real
    # bettable arb is never invisible behind a better shadow-book leg.
    funded = sorted(stats.get("funded_arbs", []), key=lambda t: t[0], reverse=True)
    if funded:
        log.info("         FUNDED-ONLY arbs (kalshi/polymarket/pinnacle/1xbet; '*'=above ROI/stake floor):")
        for roi, S, match, label, books, t_max, above in funded[:10]:
            log.info("           %s S=%.4f ROI %+.2f%% T_max %s | %s | %s",
                     "*" if above else " ", S, roi, fmt.money0(t_max), match, f"{label} [{books}]")
    elif stats["real_arbs"] == 0:
        log.info("         FUNDED-ONLY arbs: none clear S<1 this cycle (shadow arbs use non-funded books).")
    if stats["shadow_book_counter"]:
        log.info("         Books by # of shadow arbs they appeared in (which to fund next):")
        for book, c in stats["shadow_book_counter"].most_common():
            log.info("           %-14s %s", book, c)
    if stats.get("mapping_suspect_flags"):
        log.info("         Books skipped as outcome-mapping-suspect (favourite/underdog flipped vs reference):")
        for book, c in stats["mapping_suspect_flags"].most_common():
            log.info("           %-14s %s market(s)", book, c)
    log.info("         Billable requests used this run: %s", client.billable_count)
    log.info("=" * 78)


def main() -> int:
    log = setup_logging()
    cfg = load_config()
    try:
        return run_cycle(cfg, log)
    except QuotaExceeded as exc:
        # Key out of credits / rate-limited (429) or forbidden (403): clean message, no traceback.
        log_key_exhausted(log, exc)
        return 0
    except Exception:  # noqa: BLE001 - report bugs loudly but don't mask the traceback
        log.exception("Unexpected error during scan cycle.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
