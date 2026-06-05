"""Entry point for one scan cycle: budget guard -> odds -> arbs -> CSV -> Telegram.

Run with:  python -m src.run
Designed to be frugal: ~1 billable request per cycle (plus an occasional names refresh).
"""
from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from . import catalog, normalize
from .arbitrage import ArbResult, Candidate, compute_arb, make_signature, select_legs
from .config import Config, load_config
from .csv_store import append_opportunities
from .logsetup import setup_logging
from .normalize import FixtureFeed, RawCandidate, exchange_liquidity
from .oddspapi import OddsPapiClient, OddsPapiError, QuotaExceeded, check_budget
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
    max_leg_age_minutes: float
    unknown_limit_fallback: float


@dataclass
class Opportunity:
    fixture_id: str
    match: str
    tournament: str
    kickoff_utc: Optional[str]
    spec: catalog.MarketSpec
    res: ArbResult
    actionable: bool
    shadow_books: list[str]
    suspicious: bool
    bet_links: dict[str, str]
    signature: str

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
) -> list[Candidate]:
    out: list[Candidate] = []
    for rc in raws:
        if rc.book not in universe:
            continue
        age = _leg_age_minutes(rc.changed_at, now)
        if age is not None and age > ctx.max_leg_age_minutes:
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
) -> Optional[ArbResult]:
    cands_by_outcome: dict[int, list[Candidate]] = {}
    for oid in spec.outcome_ids:
        cl = _to_candidates(raw_market.get(oid, []), spec, universe, ctx, now)
        if not cl:
            return None  # market incomplete for this universe
        cands_by_outcome[oid] = cl
    chosen = select_legs(cands_by_outcome)
    if not chosen:
        return None
    return compute_arb(chosen, ctx.unknown_limit_fallback)


# --------------------------------------------------------------------------- #
# Logging the full calculation                                                  #
# --------------------------------------------------------------------------- #
def _log_arb_calc(log, opp: Opportunity, now: datetime) -> None:
    res = opp.res
    line_lbl = f" ({opp.spec.family}, {opp.spec.period})"
    log.info("[ARB] %s | %s%s", opp.match, opp.spec.label, line_lbl)
    for leg in res.legs:
        age = _leg_age_minutes(leg.changed_at, now)
        age_s = f"changed {age:.0f}m ago" if age is not None else "age n/a"
        lim_s = f"limit {leg.limit:g}" if leg.limit else "limit n/a"
        log.info("    %-10s: %.3f @ %-12s (%s, %s)",
                 leg.outcome_name, leg.decimal_odds, leg.book, lim_s, age_s)
    terms = " + ".join(f"1/{leg.eff_odds:.3f}" for leg in res.legs)
    vals = " + ".join(f"{1.0/leg.eff_odds:.4f}" for leg in res.legs)
    verdict = "ARB (S<1)" if res.is_arb else "no arb (S>=1)"
    log.info("    S = %s = %s = %.4f  -> %s", terms, vals, res.arb_sum_S, verdict)
    log.info("    ROI = 1/S - 1 = %.2f%%", res.roi_pct)
    tmax_terms = ", ".join(
        f"{leg.limit:g}*{leg.eff_odds:.3f}*{res.arb_sum_S:.4f}" for leg in res.legs if leg.limit
    )
    log.info("    T_max = min(%s) = %.1f  (binding: %s)", tmax_terms or "n/a", res.t_max, res.binding_book)
    stakes = " | ".join(f"{leg.outcome_name} {leg.stake:g} @ {leg.book}" for leg in res.legs)
    log.info("    Stakes @ T_max: %s", stakes)
    log.info("    Guaranteed profit @ T_max = %.2f (%.2f%%)  [actionable=%s, suspicious=%s, low_conf=%s]",
             res.max_profit, res.roi_pct, opp.actionable, opp.suspicious, res.low_confidence)


# --------------------------------------------------------------------------- #
# Record building (CSV + Telegram share these dicts)                            #
# --------------------------------------------------------------------------- #
def _legs_payload(opp: Opportunity) -> list[dict[str, Any]]:
    return [
        {
            "outcome": leg.outcome_name,
            "book": leg.book,
            "decimal_odds": round(leg.decimal_odds, 4),
            "american_odds": leg.american_odds,
            "limit": leg.limit,
            "stake": leg.stake,
            "changed_at": leg.changed_at,
        }
        for leg in opp.res.legs
    ]


def _csv_row(opp: Opportunity, now: datetime) -> dict[str, Any]:
    res = opp.res
    kickoff_dt = _parse_iso(opp.kickoff_utc)
    legs = _legs_payload(opp)
    return {
        "detected_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "signature": opp.signature,
        "actionable": opp.actionable,
        "bookmakers": ", ".join(leg.book for leg in res.legs),
        "market": opp.spec.label,
        "event_date": kickoff_dt.strftime("%Y-%m-%d") if kickoff_dt else "",
        "roi_pct": round(res.roi_pct, 4),
        "max_liquidity": round(res.t_max, 2),
        "match": opp.match,
        "fixture_id": opp.fixture_id,
        "tournament": opp.tournament,
        "kickoff_utc": opp.kickoff_utc or "",
        "market_id": opp.spec.market_id,
        "market_type": opp.spec.family,
        "period": opp.spec.period,
        "line": "" if opp.spec.line is None else f"{opp.spec.line:g}",
        "legs_json": legs,
        "arb_sum_S": round(res.arb_sum_S, 6),
        "roi_decimal": round(res.roi_decimal, 6),
        "total_stake_max": round(res.t_max, 2),
        "stake_split_json": {leg.book: leg.stake for leg in res.legs},
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
        "tournament": opp.tournament,
        "kickoff_utc": opp.kickoff_utc,
        "market": opp.spec.label,
        "roi_pct": res.roi_pct,
        "max_liquidity": res.t_max,
        "max_profit": res.max_profit,
        "actionable": opp.actionable,
        "suspicious": opp.suspicious,
        "low_confidence": res.low_confidence,
        "involves_exchange": res.involves_exchange,
        "legs": [
            {"book": leg.book, "outcome": leg.outcome_name,
             "decimal_odds": leg.decimal_odds, "limit": leg.limit, "stake": leg.stake}
            for leg in res.legs
        ],
        "bet_links": opp.bet_links,
    }


# --------------------------------------------------------------------------- #
# Catalog loading / ensuring                                                    #
# --------------------------------------------------------------------------- #
def _ensure_catalogs(client, cfg: Config, acct: dict[str, Any], log) -> Optional[dict[str, Any]]:
    """Load cached catalogs; auto-refresh once if missing and budget allows."""
    markets = catalog.load_json(cfg.cache_dir, catalog.MARKETS_FILE)
    books = catalog.load_json(cfg.cache_dir, catalog.BOOKMAKERS_FILE)
    tours = catalog.load_json(cfg.cache_dir, catalog.TOURNAMENTS_FILE)

    if markets and books and tours is not None:
        return {"markets": markets, "bookmakers": books, "tournaments": tours}

    remaining = acct.get("remaining")
    min_remaining = int(cfg.budget_opt("refresh_min_remaining", 24))
    if remaining is not None and remaining < min_remaining:
        log.error("Catalogs missing and only %s requests remain (< %s) — run refresh-catalog "
                  "workflow first. Exiting.", remaining, min_remaining)
        return None

    log.warning("Catalogs missing — refreshing inline (run refresh-catalog workflow to avoid this).")
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


def _fetch_odds_per_book(client, cfg, tournament_ids, books, start_remaining, safety, log):
    """One odds-by-tournaments call per book; merge each book's odds onto the shared fixture.

    Returns (feeds, fetched_books, returning_books). Stops early if the per-run budget would
    dip to the safety margin, so a single run can never blow the monthly quota.
    """
    verbosity = int(cfg.api_opt("odds_verbosity", 3))
    odds_format = str(cfg.api_opt("odds_format", "decimal"))
    raw_by_fixture: dict[str, dict[str, Any]] = {}
    fetched: list[str] = []
    returning: list[str] = []

    for book in books:
        if start_remaining is not None and (start_remaining - client.billable_count) <= safety:
            log.warning("Budget safety margin reached after %s book(s); stopping odds fetch.", len(fetched))
            break
        try:
            payload = client.odds_by_tournaments(tournament_ids, bookmaker=book,
                                                 verbosity=verbosity, odds_format=odds_format)
        except QuotaExceeded:
            log.warning("Quota hit fetching %s — stopping odds fetch.", book)
            break
        except OddsPapiError as exc:
            # e.g. a book outside the plan; count it as attempted and move on.
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

    feeds = normalize.parse_odds_payload(list(raw_by_fixture.values()))
    return feeds, fetched, returning


# --------------------------------------------------------------------------- #
# Main cycle                                                                     #
# --------------------------------------------------------------------------- #
def run_cycle(cfg: Config, log) -> int:
    now = datetime.now(timezone.utc)
    log.info("=" * 78)
    log.info("SCAN @ %s | window %s -> %s", now.strftime("%Y-%m-%dT%H:%M:%SZ"), cfg.from_utc, cfg.to_utc)

    if not cfg.secrets.odds_papi_key:
        log.error("ODDS_PAPI_KEY not set — cannot run.")
        return 0

    client = OddsPapiClient(cfg.secrets.odds_papi_key, logger=log)

    # 1) Budget guard (free call) -------------------------------------------------
    safety = int(cfg.budget_opt("safety_margin", 15))
    try:
        acct = check_budget(client, safety, log)
    except QuotaExceeded:
        log.warning("Quota already exhausted (429 on a billable elsewhere). Exiting cleanly.")
        return 0
    if not acct.get("safe_to_run", True):
        log.warning("Request budget nearly gone (remaining=%s <= margin=%s). Skipping scan.",
                    acct.get("remaining"), safety)
        if cfg.secrets.telegram_ready and not cfg.dry_run:
            send_message(cfg.secrets.telegram_bot_key, cfg.secrets.telegram_group_id,
                         f"⚠️ Arb bot paused: only {acct.get('remaining')} API requests left this month.", log)
        return 0

    # 2) Catalogs ----------------------------------------------------------------
    cats = _ensure_catalogs(client, cfg, acct, log)
    if not cats or not cats.get("markets") or not cats.get("bookmakers"):
        log.error("Required catalogs unavailable. Exiting.")
        return 0

    specs, skipped = catalog.build_market_specs(cats["markets"], cfg.sport_id, cfg.exclude_market_names)
    clone_group_of = catalog.build_clone_group_fn(cats["bookmakers"])
    log.info("Market catalog: %s MECE markets accepted, %s skipped (player-prop/excluded/non-MECE).",
             len(specs), len(skipped))

    # 3) Tournaments -------------------------------------------------------------
    tournament_ids, _matched = _resolve_tournaments(cfg, cats.get("tournaments") or [], log)
    if not tournament_ids:
        return 0

    # 3b) Which books can we query, and afford this cycle? -----------------------
    # Free/standard plans return ONE bookmaker per odds call, so each book = 1 request.
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
    max_books = int(cfg.budget_opt("max_books_per_cycle", 4))
    to_fetch = usable[:max_books]
    if len(usable) > len(to_fetch):
        log.info("Budget cap max_books_per_cycle=%s -> fetching %s of %s usable books this cycle: %s",
                 max_books, len(to_fetch), len(usable), to_fetch)

    # 4) Names map (cached; refresh at most every names_cache_hours, budget permitting) --
    names = catalog.load_json(cfg.cache_dir, catalog.NAMES_FILE) or {}
    names_age = catalog.file_age_hours(cfg.cache_dir, catalog.NAMES_FILE, now.timestamp())
    names_ttl = float(cfg.budget_opt("names_cache_hours", 6))
    remaining = acct.get("remaining")
    if names_age is None or names_age > names_ttl:
        if remaining is not None and (remaining - client.billable_count) <= safety + len(to_fetch):
            log.warning("Skipping names refresh to preserve odds budget (remaining=%s).", remaining)
        else:
            try:
                log.info("Refreshing fixtures name map (cache age=%s, ttl=%sh).",
                         f"{names_age:.1f}h" if names_age is not None else "missing", names_ttl)
                names = catalog.refresh_names(client, cfg.cache_dir, cfg.sport_id, tournament_ids,
                                              cfg.from_utc, cfg.to_utc, now.timestamp())
            except QuotaExceeded:
                log.warning("Quota hit while refreshing names — continuing with cached/empty names.")
    by_fixture = names.get("by_fixture", {})
    by_participant = names.get("by_participant", {})

    # 5) Odds — ONE billable call per book, merged across books ------------------
    start_remaining = acct.get("remaining")
    feeds, fetched_books, returning_books = _fetch_odds_per_book(
        client, cfg, tournament_ids, to_fetch, start_remaining, safety, log)
    log.info("Odds fetch: %s book-call(s); %s returned odds (%s).",
             len(fetched_books), len(returning_books), ", ".join(returning_books) or "none")
    if len(returning_books) < 2:
        log.warning("Fewer than 2 books returned odds this cycle (%s) -> no cross-book arbitrage possible.",
                    returning_books or "none")

    seen_mids = normalize.seen_market_ids(feeds)
    log.info("Feed: %s fixtures with odds. Distinct marketIds seen: %s", len(feeds), sorted(seen_mids))
    missing = [mid for mid in seen_mids if mid not in specs]
    if missing:
        log.info("marketIds seen but NOT scanned (player-prop/excluded/unclassified): %s", sorted(missing))

    ctx = EngineCtx(
        actionable=set(cfg.actionable_books),
        tracked=set(cfg.tracked_books),
        exchanges=cfg.exchanges,
        commission=cfg.commission,
        clone_group_of=clone_group_of,
        max_leg_age_minutes=float(cfg.threshold("max_leg_age_minutes", 20)),
        unknown_limit_fallback=float(cfg.threshold("unknown_limit_fallback", 100)),
    )

    # 6) Scan --------------------------------------------------------------------
    opportunities, stats = _scan(feeds, specs, ctx, cfg, by_fixture, by_participant, now, log)

    # 7) Output ------------------------------------------------------------------
    _emit(opportunities, stats, cfg, now, client, log)
    return 0


def _scan(feeds, specs, ctx, cfg, by_fixture, by_participant, now, log):
    from_dt = _parse_iso(cfg.from_utc)
    to_dt = _parse_iso(cfg.to_utc)
    min_roi = float(cfg.threshold("min_roi_pct", 0.5))
    min_stake = float(cfg.threshold("min_total_stake", 20))
    susp_pct = float(cfg.threshold("roi_suspicious_pct", 8.0))
    near_ceiling = float(cfg.threshold("near_miss_ceiling_S", 1.02))

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

        match = _match_name(fx, by_fixture, by_participant)
        info = by_fixture.get(fx.fixture_id, {})
        if info.get("status_id") in (2, 3):  # cancelled per the name map (e.g. struck-through fixture)
            stats["fixtures_skipped_status"] += 1
            continue
        tournament = info.get("tournament") or ""
        stats["fixtures_in_window"] += 1
        log.info("MATCH: %s | %s | books with odds: %s",
                 match, fx.start_time, ", ".join(sorted(fx.books_present)) or "none")

        for mid, raw_market in fx.markets.items():
            spec = specs.get(mid)
            if spec is None:
                continue
            if spec.has_quarter_line and not cfg.allow_quarter_lines:
                continue
            stats["markets_scanned"] += 1

            real = _arb_for_universe(raw_market, spec, ctx.actionable, ctx, now)
            shadow = _arb_for_universe(raw_market, spec, ctx.tracked, ctx, now)

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
                if res.roi_pct < min_roi or res.t_max < min_stake:
                    continue

                actionable = all(leg.book in ctx.actionable for leg in res.legs)
                shadow_books = [leg.book for leg in res.legs if leg.book not in ctx.actionable]
                suspicious = res.roi_pct > susp_pct
                sig = make_signature(fx.fixture_id, mid, spec.line, res.legs)
                bet_links = {leg.book: fx.fixture_paths.get(leg.book, "") for leg in res.legs}

                opp = Opportunity(
                    fixture_id=fx.fixture_id, match=match, tournament=tournament,
                    kickoff_utc=fx.start_time, spec=spec, res=res, actionable=actionable,
                    shadow_books=shadow_books, suspicious=suspicious, bet_links=bet_links, signature=sig,
                )
                prev = opportunities.get(sig)
                if prev is None or (actionable and not prev.actionable):
                    opportunities[sig] = opp

    # Tally after dedup so each opportunity counts once.
    for opp in opportunities.values():
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


def _match_name(fx: FixtureFeed, by_fixture: dict, by_participant: dict) -> str:
    info = by_fixture.get(fx.fixture_id)
    if info and info.get("p1") and info.get("p2"):
        return f"{info['p1']} vs {info['p2']}"
    p1 = by_participant.get(str(fx.participant1_id), f"Team {fx.participant1_id}")
    p2 = by_participant.get(str(fx.participant2_id), f"Team {fx.participant2_id}")
    return f"{p1} vs {p2}"


def _rank_key(cfg: Config):
    if str(cfg.telegram_opt("rank_by", "profit")).lower() == "roi":
        return lambda o: o.rank_roi
    return lambda o: o.rank_profit


def _emit(opportunities, stats, cfg: Config, now, client, log):
    # --- CSV ---
    if opportunities:
        rows = [_csv_row(o, now) for o in opportunities]
        counts = append_opportunities(cfg.csv_path, rows, now,
                                      float(cfg.threshold("csv_dedup_minutes", 90)))
        log.info("CSV: %s new, %s updated -> %s", counts["new"], counts["updated"], cfg.csv_path)
    else:
        log.info("CSV: no opportunities to write.")

    # --- Telegram (top 3) ---
    rank = _rank_key(cfg)
    # Prefer actionable, non-suspicious arbs for the headline three.
    preferred = [o for o in opportunities if o.actionable and not o.suspicious]
    rest = [o for o in opportunities if o not in preferred]
    ordered = sorted(preferred, key=rank, reverse=True) + sorted(rest, key=rank, reverse=True)
    top = ordered[:3]

    sent = 0
    if top and not cfg.dry_run:
        if cfg.secrets.telegram_ready:
            header = (f"⚽ <b>Arb scan</b> — {now.strftime('%Y-%m-%d %H:%M UTC')}\n"
                      f"{stats['real_arbs']} real · {stats['shadow_arbs']} shadow · "
                      f"top {len(top)} below")
            msg = build_message([_telegram_item(o) for o in top], header,
                                str(cfg.telegram_opt("local_tz", "America/Toronto")))
            if send_message(cfg.secrets.telegram_bot_key, cfg.secrets.telegram_group_id, msg, log):
                sent = len(top)
        else:
            log.warning("Telegram not configured — would have sent %s opportunities.", len(top))
    elif top and cfg.dry_run:
        log.info("[dry_run] Would send %s opportunities to Telegram.", len(top))
    elif not top:
        log.info("No opportunities to send to Telegram this cycle.")
        if cfg.telegram_opt("send_when_empty", False) and cfg.secrets.telegram_ready and not cfg.dry_run:
            send_message(cfg.secrets.telegram_bot_key, cfg.secrets.telegram_group_id,
                         f"⚽ Arb scan {now.strftime('%H:%M UTC')}: no opportunities this cycle.", log)

    # --- Summary ---
    log.info("-" * 78)
    log.info("SUMMARY: %s fixtures in window | %s skipped(status) | %s markets scanned (%s complete)",
             stats["fixtures_in_window"], stats["fixtures_skipped_status"],
             stats["markets_scanned"], stats.get("markets_complete", 0))
    log.info("         %s real arbs | %s shadow arbs | %s near-misses | %s arb(s) below ROI/stake floor | %s sent",
             stats["real_arbs"], stats["shadow_arbs"], stats["near_misses"],
             stats.get("arbs_below_threshold", 0), sent)
    if stats["shadow_book_counter"]:
        log.info("         Books by # of shadow arbs they appeared in (which to fund next):")
        for book, c in stats["shadow_book_counter"].most_common():
            log.info("           %-14s %s", book, c)
    log.info("         Billable requests used this run: %s", client.billable_count)
    log.info("=" * 78)


def main() -> int:
    log = setup_logging()
    cfg = load_config()
    try:
        return run_cycle(cfg, log)
    except QuotaExceeded:
        log.warning("Quota exhausted mid-run. Exiting cleanly (exit 0).")
        return 0
    except Exception:  # noqa: BLE001 - report bugs loudly but don't mask the traceback
        log.exception("Unexpected error during scan cycle.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
