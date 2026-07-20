"""One multi-sport OG scan cycle — the orchestrator (spec section 0).

``python -m src.og_multi`` calls :func:`run_cycle`. For each enabled sport that is DUE (per-sport
cadence via ``state.is_due`` — one process serves all sports) it:
  1. loads the GenZ tree (``data/genz/<sport>_tree.json``);
  2. fetches the-odds-api odds (toa.run_toa — self-learning, quota-accounted);
  3. matches events to tree games (match.match_events);
  4. fetches the live exchange ask ladders Tier A needs, CONCURRENTLY (resolve.MarketData);
  5. assembles Tier A + Tier B rows (tiers.build_rows);
  6. writes ``data/og_current_<sport>.json`` (soccer panel shape + tier/settlement/inventory fields);
  7. stamps the sport's last-run.

ALERT-ONLY: nothing here places, or can place, an order. A per-sport failure is logged and skipped so
one bad sport never sinks the cycle.
"""
from __future__ import annotations

import concurrent.futures as cf
from datetime import datetime, timezone
from typing import Any, Optional

from ..config import Config, load_config
from ..executor.resolve import MarketData
from ..genz.config import load_genz_config, paths_for_sport
from ..kalshi import KalshiClient
from ..logsetup import get_logger, setup_logging
from ..polymarket import PolymarketClient
from ..theoddsapi import TheOddsApiClient
from . import match, state, tiers, toa
from .tiers import TierCfg


def _load_tree(sport: str) -> dict:
    return state.read_json(paths_for_sport(sport).tree_path, {"games": {}})


def _market_data(gz) -> MarketData:
    """A live read-only book source with the sport's configured Kalshi throttle + timeouts."""
    kc = KalshiClient(base_url=gz.kalshi_base_url, timeout=gz.http_timeout_seconds,
                      min_interval=(gz.kalshi_min_interval if gz.kalshi_min_interval is not None else 0.5))
    pc = PolymarketClient(gamma_base=gz.gamma_base, clob_base=gz.clob_base, timeout=gz.http_timeout_seconds)
    return MarketData(kalshi_client=kc, poly_client=pc)


def _fetch_ladders(md: MarketData, jobs: set, max_workers: int, log) -> dict:
    """Fetch every (venue, identifier, side) ask ladder concurrently. A failed fetch -> [] (that leg
    simply drops out of best-of-book); a scan is never blocked by one hung call."""
    if not jobs:
        return {}

    def one(job: tuple[str, str, str]):
        venue, ident, side = job
        try:
            asks = md.kalshi_ask_ladder(ident, side) if venue == "kalshi" else md.poly_ask_ladder(ident)
            return job, asks
        except Exception as exc:                              # noqa: BLE001 — never let one leg sink the cycle
            log.debug("[OGM] ladder fetch failed %s: %s", job, exc)
            return job, []

    ladders: dict = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as ex:
        for job, asks in ex.map(one, list(jobs)):
            ladders[job] = asks
    return ladders


def scan_sport(sport: str, *, cfg: Config, now: datetime, log,
               base: str = state.OG_MULTI_DIR) -> dict[str, Any]:
    """Run one sport's cycle and write its panel file. Returns a summary dict."""
    gz = load_genz_config(sport=sport)
    games = _load_tree(sport).get("games", {}) or {}
    interval_s = float((cfg.og_multi_opt("interval_s", {}) or {}).get(sport, 300))
    region = str(cfg.og_multi_opt("region", "eu"))
    toa_key = str((cfg.og_multi_opt("toa_sports", {}) or {}).get(sport, ""))
    warn_pct = float(cfg.og_multi_opt("quota_warn_pct", 60) or 60)

    fetch = toa.ToaFetch(sport=sport)
    api_key = cfg.secrets.odds_api_key
    if api_key and toa_key:
        try:
            fetch = toa.run_toa(sport, client=TheOddsApiClient(api_key), region=region,
                                toa_sport_key=toa_key, now=now, log=log, warn_pct=warn_pct, base=base)
        except Exception as exc:                              # noqa: BLE001
            log.warning("[OGM] %s the-odds-api fetch failed: %s", sport, exc)
    elif not api_key:
        log.warning("[OGM] %s: ODDS_API_KEY unset - no book legs this cycle (exchange-only)", sport)

    matched = match.match_events(sport, fetch.events, games, now=now, log=log)

    tcfg = TierCfg(pinnacle_limit=float(cfg.og_multi_opt("pinnacle_limit", 5000) or 5000),
                   age_limit_min=float(cfg.og_multi_opt("age_limit_min", 30) or 30),
                   poly_fee_rate=float(gz.poly_fee_rate), bankroll_cap=float(cfg.bankroll_total),
                   min_total_stake=float(cfg.threshold("min_total_stake", 20) or 20))

    jobs = tiers.exchange_jobs(sport, matched.by_game, age_limit_min=tcfg.age_limit_min, now=now)
    ladders = _fetch_ladders(_market_data(gz), jobs, gz.max_workers, log) if jobs else {}
    rows, inv = tiers.build_rows(sport, matched.by_game, ladders, cfg=tcfg, now=now, log=log)

    payload = _write_sport(sport, rows, inv, fetch, interval_s, now, base)
    state.mark_ran(sport, now, base)

    summary = {"sport": sport, "games": len(games), "events": len(fetch.events),
               "matched": len(matched.by_game), "unmatched": len(matched.unmatched),
               "ladder_jobs": len(jobs), "rows": len(payload["arbs"]),
               "arbs": inv.get("arbs", 0), "near_misses": inv.get("near_misses", 0),
               "credits": fetch.credits, "markets_served": fetch.markets_served,
               "sport_keys": fetch.sport_keys}
    log.info("[OGM] %s: %d games, %d toa events, %d matched, %d ladder fetches -> %d placeable / "
             "%d near-miss (tierA %d, tierB %d)", sport, len(games), len(fetch.events),
             len(matched.by_game), len(jobs), inv.get("arbs", 0), inv.get("near_misses", 0),
             inv.get("tier_a_families", 0), inv.get("tier_b_families", 0))
    return summary


def _write_sport(sport: str, rows: list[dict], inv: dict, fetch: toa.ToaFetch,
                 interval_s: float, now: datetime, base: str) -> dict[str, Any]:
    """Write ``data/og_current_<sport>.json``. Placeable rows first, near-misses capped at 20 (the
    spec's near_misses cap). The panel does its own funded-first sort + place/near split."""
    placeable = [r for r in rows if not (r.get("fee_trap") or r.get("below_floor"))]
    near = [r for r in rows if r.get("fee_trap") or r.get("below_floor")][:20]
    payload = {
        "cycle_utc": state.iso_utc(now), "sport": sport, "scan_interval_s": int(interval_s),
        "toa_capabilities": {"sport_keys": fetch.sport_keys, "markets_served": fetch.markets_served,
                             "credits": fetch.credits, "daily_total": fetch.daily_total},
        "inventory_counts": inv, "arbs": placeable + near,
    }
    state.write_json(state.og_current_path(sport), payload)
    return payload


def run_cycle(*, now: Optional[datetime] = None, log=None, config_path: Optional[str] = None,
              base: str = state.OG_MULTI_DIR) -> list[dict[str, Any]]:
    """One multi-sport cycle: scan every enabled sport that is due. Returns the per-sport summaries."""
    now = now or datetime.now(timezone.utc)
    log = log or get_logger("og_multi")
    cfg = load_config(config_path)
    enabled = cfg.og_multi_enabled_sports
    interval_map = cfg.og_multi_opt("interval_s", {}) or {}
    state.ensure_dir(base)

    summaries: list[dict[str, Any]] = []
    for sport in enabled:
        interval = float(interval_map.get(sport, 300))
        if not state.is_due(sport, interval, now, base):
            log.info("[OGM] %s: not due (interval %ss) - skipping this cycle", sport, interval)
            continue
        try:
            summaries.append(scan_sport(sport, cfg=cfg, now=now, log=log, base=base))
        except Exception as exc:                              # noqa: BLE001 — isolate per-sport failures
            log.exception("[OGM] %s scan failed: %s", sport, exc)
    if not enabled:
        log.info("[OGM] og_multi.enabled_sports is empty - nothing to scan")
    return summaries


def main() -> None:
    log = setup_logging()
    run_cycle(log=log)


if __name__ == "__main__":
    main()
